package whatsapp

// Phone-offline detection.
//
// A linked device only keeps working while the primary phone comes online now
// and then: WhatsApp unlinks companions after roughly 14 days without it, and
// nothing on the bridge side announces that in advance. The bridge cannot ask
// "is the phone online", so it watches for traffic that only the phone itself
// produces, remembers the newest such timestamp in messages.db (phone_status),
// warns as the gap grows, and once the gap passes PHONE_OFFLINE_PING_AFTER
// sends one small request per day that only the phone answers. A phone that is
// merely idle (asleep, app backgrounded) wakes up for it and refreshes the
// timer; a phone that is truly gone stays silent and the logs escalate.
//
// The idea follows mautrix-whatsapp's phone ping; this is an independent
// implementation.

import (
	"context"
	"fmt"
	"sync"
	"time"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"

	"whatsapp-bridge/internal/database"
)

const (
	// Log thresholds. Fixed rather than configurable: they are measured
	// against WhatsApp's own ~14-day unlink window, which is not ours to tune.
	phoneOfflineWarnAfter  = 7 * 24 * time.Hour
	phoneOfflineErrorAfter = 12 * 24 * time.Hour

	// phoneAlertRepeat and phonePingMinInterval keep both the log and the
	// phone quiet: one line and at most one nudge per day.
	phoneAlertRepeat     = 24 * time.Hour
	phonePingMinInterval = 24 * time.Hour

	// phoneSeenGranularity throttles writes. The phone sends receipts in
	// bursts (reading a chat marks dozens of messages) and day-scale
	// thresholds gain nothing from minute-level precision.
	phoneSeenGranularity = time.Hour

	// phoneCheckInterval is how often the monitor re-evaluates thresholds
	// between the connect-time check.
	phoneCheckInterval = time.Hour

	phonePingTimeout = 30 * time.Second
)

// phoneAlertLevel is how loudly an outage of a given length is logged.
type phoneAlertLevel int

const (
	phoneAlertNone phoneAlertLevel = iota
	phoneAlertWarn
	phoneAlertError
)

// phoneStatusStore is the persistence the monitor needs; *database.MessageStore
// satisfies it, tests use an in-memory fake.
type phoneStatusStore interface {
	LoadPhoneStatus() (database.PhoneStatus, error)
	SavePhoneStatus(database.PhoneStatus) error
}

// PhoneMonitor tracks when the primary phone was last seen and nudges it when
// it has been silent too long. Create it with Client.StartPhoneMonitor.
type PhoneMonitor struct {
	store       phoneStatusStore
	logger      waLog.Logger
	pingEnabled bool
	pingAfter   time.Duration
	ping        func(context.Context) error
	// linked reports whether the session is connected and paired; checks are
	// skipped otherwise, since neither a ping nor an unlink warning means
	// anything without a session.
	linked func() bool
	now    func() time.Time
	ctx    context.Context

	mu             sync.Mutex
	state          database.PhoneStatus
	lastAlertAt    time.Time
	lastAlertLevel phoneAlertLevel
}

// newPhoneMonitor loads the persisted status. The first run on a store stamps
// TrackingSince so that a phone which never shows up is still measured from
// somewhere; without it an upgraded install would report "unknown" forever.
func newPhoneMonitor(ctx context.Context, store phoneStatusStore, logger waLog.Logger, pingEnabled bool, pingAfter time.Duration,
	ping func(context.Context) error, linked func() bool, now func() time.Time) (*PhoneMonitor, error) {
	st, err := store.LoadPhoneStatus()
	if err != nil {
		return nil, fmt.Errorf("load phone status: %w", err)
	}
	m := &PhoneMonitor{
		store:       store,
		logger:      logger,
		pingEnabled: pingEnabled,
		pingAfter:   pingAfter,
		ping:        ping,
		linked:      linked,
		now:         now,
		ctx:         ctx,
		state:       st,
	}
	if st.TrackingSince.IsZero() {
		m.state.TrackingSince = now().UTC()
		m.saveLocked()
	}
	return m, nil
}

// isFromPrimaryPhone reports whether an event was produced by our own primary
// phone. Our JID with device 0 is the phone; other companions (WhatsApp Web,
// desktop) are device > 0 and say nothing about the phone. Messages this
// bridge sends are not echoed back as events, so they cannot fake a sighting.
func isFromPrimaryPhone(src types.MessageSource) bool {
	return src.IsFromMe && src.Sender.Device == 0
}

// handleEvent is registered as a whatsmeow event handler. Signals of a live
// phone:
//   - receipts from device 0: the phone marking messages read/delivered;
//   - messages from device 0: anything typed on the phone, and also the
//     protocol message that answers our ping (an app-state key share);
//   - media retry answers: only the phone re-uploads media;
//   - pairing: pairing needs the phone in hand.
//
// History-sync blobs are deliberately not counted: they can be requested
// on demand and are downloaded from the CDN, so they prove little about now.
func (m *PhoneMonitor) handleEvent(evt interface{}) {
	switch v := evt.(type) {
	case *events.Message:
		if isFromPrimaryPhone(v.Info.MessageSource) {
			m.phoneSeen(v.Info.Timestamp)
		}
	case *events.Receipt:
		if isFromPrimaryPhone(v.MessageSource) {
			m.phoneSeen(v.Timestamp)
		}
	case *events.MediaRetry:
		m.phoneSeen(v.Timestamp)
	case *events.PairSuccess:
		m.phoneSeen(m.now())
	case *events.OfflineSyncCompleted:
		// Check only once the backlog that queued up while we were away has
		// been processed: it often holds receipts from the phone, and pinging
		// before reading them would nudge a phone that was online all along.
		go m.check(m.ctx)
	}
}

// phoneSeen records evidence that the phone was online at ts.
func (m *PhoneMonitor) phoneSeen(ts time.Time) {
	now := m.now()
	// A phone with a skewed clock must not push last-seen into the future,
	// where it would mask a real outage.
	if ts.IsZero() || ts.After(now) {
		ts = now
	}
	ts = ts.UTC()

	m.mu.Lock()
	if !phoneSeenWorthSaving(m.state.LastSeen, ts) {
		m.mu.Unlock()
		return
	}
	prevOffline := phoneOfflineFor(m.state, now)
	wasAlerting := m.lastAlertLevel != phoneAlertNone
	m.state.LastSeen = ts
	m.lastAlertLevel = phoneAlertNone
	m.lastAlertAt = time.Time{}
	m.saveLocked()
	m.mu.Unlock()

	if wasAlerting {
		m.logger.Infof("[PHONE] Phone is back online after %.1f days", prevOffline.Hours()/24)
	}
}

// Status returns when the phone was last seen (zero if never) and how long it
// has been unseen. The offline duration is measured from TrackingSince when
// the phone has never been seen, so it is a lower bound in that case. ok is
// false before tracking has started.
func (m *PhoneMonitor) Status() (lastSeen time.Time, offline time.Duration, ok bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.state.LastSeen.IsZero() && m.state.TrackingSince.IsZero() {
		return time.Time{}, 0, false
	}
	return m.state.LastSeen, phoneOfflineFor(m.state, m.now()), true
}

// run re-checks the thresholds periodically until ctx is cancelled.
func (m *PhoneMonitor) run(ctx context.Context) {
	ticker := time.NewTicker(phoneCheckInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			m.check(ctx)
		}
	}
}

// check logs the outage if it is due and sends a ping if one is due.
func (m *PhoneMonitor) check(ctx context.Context) {
	if !m.linked() {
		return
	}
	now := m.now()

	m.mu.Lock()
	offline := phoneOfflineFor(m.state, now)
	level := phoneAlertLevelFor(offline)
	alert := shouldLogPhoneAlert(level, m.lastAlertLevel, m.lastAlertAt, now)
	if alert {
		m.lastAlertLevel = level
		m.lastAlertAt = now
	}
	doPing := m.pingEnabled && shouldPingPhone(offline, m.state.LastPinged, now, m.pingAfter)
	if doPing {
		// Stamped before sending, so a failing send still counts as today's
		// attempt: "at most one per day" holds even when the socket is flaky.
		m.state.LastPinged = now.UTC()
		m.saveLocked()
	}
	st := m.state
	m.mu.Unlock()

	if alert {
		m.logPhoneAlert(level, st, offline)
	}
	if !doPing {
		return
	}
	pingCtx, cancel := context.WithTimeout(ctx, phonePingTimeout)
	defer cancel()
	if err := m.ping(pingCtx); err != nil {
		m.logger.Warnf("[PHONE] Phone ping failed: %v", err)
		return
	}
	m.logger.Infof("[PHONE] Phone silent for %.1f days, sent a ping to wake it", offline.Hours()/24)
}

func (m *PhoneMonitor) logPhoneAlert(level phoneAlertLevel, st database.PhoneStatus, offline time.Duration) {
	since := "last seen " + st.LastSeen.Format(time.RFC3339)
	if st.LastSeen.IsZero() {
		since = "never seen since tracking started " + st.TrackingSince.Format(time.RFC3339)
	}
	msg := fmt.Sprintf("[PHONE] Primary phone offline for %.1f days (%s). WhatsApp unlinks this device after ~14 days without the phone: open WhatsApp on the phone with internet access.",
		offline.Hours()/24, since)
	switch level {
	case phoneAlertError:
		m.logger.Errorf("%s", msg)
	case phoneAlertWarn:
		m.logger.Warnf("%s", msg)
	}
}

// saveLocked persists the current state; the caller holds m.mu (or owns m
// exclusively). Writing under the lock keeps two concurrent saves from landing
// out of order and regressing last-seen on disk. A failed write is logged, not
// fatal: the in-memory state stays correct and the next change writes again.
func (m *PhoneMonitor) saveLocked() {
	if err := m.store.SavePhoneStatus(m.state); err != nil {
		m.logger.Warnf("[PHONE] %v", err)
	}
}

// phoneSeenWorthSaving reports whether a sighting at ts should replace prev.
// Older sightings (offline backlog, out-of-order receipts) never move the
// clock back, and sightings within phoneSeenGranularity are not rewritten.
func phoneSeenWorthSaving(prev, ts time.Time) bool {
	if prev.IsZero() {
		return true
	}
	return ts.Sub(prev) >= phoneSeenGranularity
}

// phoneOfflineFor is how long the phone has been unseen at now, measured from
// the last sighting or, failing that, from when tracking started.
func phoneOfflineFor(st database.PhoneStatus, now time.Time) time.Duration {
	ref := st.LastSeen
	if ref.IsZero() {
		ref = st.TrackingSince
	}
	if ref.IsZero() || now.Before(ref) {
		return 0
	}
	return now.Sub(ref)
}

func phoneAlertLevelFor(offline time.Duration) phoneAlertLevel {
	switch {
	case offline > phoneOfflineErrorAfter:
		return phoneAlertError
	case offline > phoneOfflineWarnAfter:
		return phoneAlertWarn
	default:
		return phoneAlertNone
	}
}

// shouldLogPhoneAlert limits the log to one line per day, except that an
// escalation from warn to error is logged at once.
func shouldLogPhoneAlert(level, lastLevel phoneAlertLevel, lastAt, now time.Time) bool {
	if level == phoneAlertNone {
		return false
	}
	if level > lastLevel || lastAt.IsZero() {
		return true
	}
	return now.Sub(lastAt) >= phoneAlertRepeat
}

// shouldPingPhone reports whether a ping is due: the phone has been unseen
// longer than pingAfter and no ping went out in the last day.
func shouldPingPhone(offline time.Duration, lastPinged, now time.Time, pingAfter time.Duration) bool {
	if pingAfter <= 0 || offline <= pingAfter {
		return false
	}
	return lastPinged.IsZero() || now.Sub(lastPinged) >= phonePingMinInterval
}

// sendPhonePing asks the primary phone for an app-state sync key.
//
// Why this request: it is a peer message (to our own device 0 only, never to
// a contact), it has no visible effect on the phone (unlike a media retry,
// which shows "Finished syncing", or an on-demand history sync), and the phone
// answers it with an AppStateSyncKeyShare protocol message from device 0,
// which whatsmeow dispatches as an *events.Message that handleEvent counts as
// a sighting. Asking for the newest key we already hold makes the answer a
// harmless re-share. mautrix-whatsapp uses the same request for the same job.
func (c *Client) sendPhonePing(ctx context.Context) error {
	var keyIDs []*waE2E.AppStateSyncKeyId
	keyID, err := c.Store.AppStateKeys.GetLatestAppStateSyncKeyID(ctx)
	if err != nil {
		// An empty request still reaches the phone and still gets an answer.
		c.logger.Warnf("[PHONE] No app-state key id for the ping, sending an empty request: %v", err)
	} else if keyID != nil {
		keyIDs = append(keyIDs, &waE2E.AppStateSyncKeyId{KeyID: keyID})
	}
	_, err = c.Client.SendPeerMessage(ctx, &waE2E.Message{
		ProtocolMessage: &waE2E.ProtocolMessage{
			Type:                   waE2E.ProtocolMessage_APP_STATE_SYNC_KEY_REQUEST.Enum(),
			AppStateSyncKeyRequest: &waE2E.AppStateSyncKeyRequest{KeyIDs: keyIDs},
		},
	})
	if err != nil {
		return fmt.Errorf("send app-state key request to phone: %w", err)
	}
	return nil
}

// StartPhoneMonitor begins phone-offline tracking: it loads the persisted
// status, registers its own event handler and starts the periodic check,
// which stops when ctx is cancelled. Call it once, before the API server
// starts, so PhoneStatus never races with the assignment.
func (c *Client) StartPhoneMonitor(ctx context.Context, store *database.MessageStore) error {
	pingEnabled, pingAfter := true, 7*24*time.Hour
	if c.cfg != nil {
		pingEnabled, pingAfter = c.cfg.PhonePingEnabled, c.cfg.PhoneOfflinePingAfter
	}
	linked := func() bool { return c.IsConnected() && c.Store.ID != nil }
	m, err := newPhoneMonitor(ctx, store, c.logger, pingEnabled, pingAfter, c.sendPhonePing, linked, time.Now)
	if err != nil {
		return fmt.Errorf("start phone monitor: %w", err)
	}
	c.phoneMonitor = m
	c.AddEventHandler(m.handleEvent)
	go m.run(ctx)
	if pingEnabled {
		c.logger.Infof("[PHONE] Phone-offline monitor on (ping after %v of silence, max once a day)", pingAfter)
	} else {
		c.logger.Infof("[PHONE] Phone-offline monitor on, ping disabled (PHONE_PING_ENABLED=false)")
	}
	return nil
}

// PhoneStatus reports when the primary phone was last seen and how long it
// has been unseen; see PhoneMonitor.Status. ok is false when the monitor is
// not running.
func (c *Client) PhoneStatus() (lastSeen time.Time, offline time.Duration, ok bool) {
	if c.phoneMonitor == nil {
		return time.Time{}, 0, false
	}
	return c.phoneMonitor.Status()
}

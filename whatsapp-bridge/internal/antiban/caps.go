package antiban

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"sort"
	"sync"
	"time"
)

// Send caps.
//
// Warm-up only limits a fresh account for its first ANTIBAN_WARMUP_DAYS; after
// that nothing bounded send volume at all, so an agent stuck in a loop could
// message one chat (or many) as fast as pacing allowed. These caps are sliding
// windows that never expire with account age.
//
// They count attempts that got past every other check and were handed to
// WhatsApp, not successes: a send WhatsApp rejected still reached its servers,
// and counting before dispatch is what keeps two concurrent sends from both
// slipping under the last free slot.
//
// Only the new-recipient window is persisted. It spans 24h, so losing it on a
// restart would hand out a fresh budget of first contacts, the riskiest kind
// of send. The minute/hour windows are cheap to forget.

// ErrSendCapReached is matched (errors.Is) by every *CapError.
var ErrSendCapReached = errors.New("antiban: send cap reached")

// Cap names, also used in logs and error text.
const (
	CapRecipientPerMinute = "recipient_per_minute"
	CapRecipientPerHour   = "recipient_per_hour"
	CapGlobalPerHour      = "global_per_hour"
	CapNewRecipientsDay   = "new_recipients_per_day"
	CapFeedbackPerMinute  = "feedback_per_minute"
	CapFeedbackPerHour    = "feedback_per_hour"
)

const (
	newRecipientWindow = 24 * time.Hour
	// Past this many tracked chats, sweep idle ones so the maps don't grow
	// with every chat ever messaged.
	capSweepThreshold = 256
)

// CapError reports which cap refused a send and when it frees up.
type CapError struct {
	Cap        string
	Limit      int
	Window     time.Duration
	RetryAfter time.Duration
}

// Error implements error.
func (e *CapError) Error() string {
	return fmt.Sprintf("antiban: %s cap reached (%d per %s); retry in %s",
		e.Cap, e.Limit, e.Window, e.RetryAfter.Round(time.Second))
}

// Is lets errors.Is(err, ErrSendCapReached) match any cap.
func (e *CapError) Is(target error) bool { return target == ErrSendCapReached }

// SendRequest describes one outgoing message for pacing and caps.
type SendRequest struct {
	Type MessageType
	// Len is the text length, used for typing simulation.
	Len int
	// Recipient is the chat's non-AD JID. Empty skips the per-chat caps.
	Recipient string
	// NewRecipient marks a first contact: a one-to-one chat with no stored
	// messages. The caller decides, since only it has the message store.
	NewRecipient bool
}

type capsState struct {
	NewRecipients []time.Time `json:"newRecipients"`
}

// SendCaps enforces the sliding-window ceilings. Safe for concurrent use.
type SendCaps struct {
	cfg *Config
	now func() time.Time

	mu                sync.Mutex
	recipient         map[string][]time.Time // classMessage, per chat, last hour
	feedbackRecipient map[string][]time.Time // classFeedback, per chat, last minute
	global            []time.Time            // classMessage, last hour
	feedbackGlobal    []time.Time            // classFeedback, last hour
	newRecipients     []time.Time            // first contacts, last 24h
}

// NewSendCaps builds the caps and loads the persisted new-recipient window.
// It never fails: an unreadable state file is quarantined and treated as a
// spent first-contact budget (see loadNewRecipients).
func NewSendCaps(cfg *Config) *SendCaps {
	if cfg == nil {
		cfg = LoadConfig()
	}
	c := &SendCaps{
		cfg:               cfg,
		now:               time.Now,
		recipient:         make(map[string][]time.Time),
		feedbackRecipient: make(map[string][]time.Time),
	}
	c.newRecipients = c.loadNewRecipients()
	return c
}

// Check reports whether req would be refused, without recording anything.
// Called before the pacing delay so a capped send fails at once instead of
// waiting seconds only to be refused.
func (c *SendCaps) Check(req SendRequest) error {
	if c == nil {
		return nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.checkLocked(req, c.now())
}

// Reserve checks req and, if allowed, records it in every window it counts
// against, atomically. Called right before dispatch.
func (c *SendCaps) Reserve(req SendRequest) error {
	if c == nil {
		return nil
	}
	c.mu.Lock()
	now := c.now()
	if err := c.checkLocked(req, now); err != nil {
		c.mu.Unlock()
		return err
	}

	persist := false
	switch req.Type.class() {
	case classMessage:
		c.global = append(c.global, now)
		if req.Recipient != "" {
			c.recipient[req.Recipient] = append(c.recipient[req.Recipient], now)
		}
		if req.NewRecipient && c.cfg.NewRecipientsPerDay > 0 {
			c.newRecipients = append(c.newRecipients, now)
			persist = true
		}
	case classFeedback:
		c.feedbackGlobal = append(c.feedbackGlobal, now)
		if req.Recipient != "" {
			c.feedbackRecipient[req.Recipient] = append(c.feedbackRecipient[req.Recipient], now)
		}
	}
	if len(c.recipient)+len(c.feedbackRecipient) > capSweepThreshold {
		c.sweepLocked(now)
	}
	var snapshot capsState
	if persist {
		snapshot.NewRecipients = append([]time.Time(nil), c.newRecipients...)
	}
	c.mu.Unlock()

	if persist {
		if err := c.save(snapshot); err != nil {
			log.Printf("ANTIBAN WARN could not persist new-recipient window to %s: %v", c.cfg.CapsStatePath, err)
		}
	}
	return nil
}

func (c *SendCaps) checkLocked(req SendRequest, now time.Time) error {
	if !c.cfg.CapsEnabled {
		return nil
	}

	var worst *CapError
	consider := func(name string, ts []time.Time, limit int, window time.Duration) []time.Time {
		ts = pruneBefore(ts, now.Add(-window))
		if retry, full := windowFull(ts, limit, window, now); full {
			if worst == nil || retry > worst.RetryAfter {
				worst = &CapError{Cap: name, Limit: limit, Window: window, RetryAfter: retry}
			}
		}
		return ts
	}

	switch req.Type.class() {
	case classMessage:
		c.global = consider(CapGlobalPerHour, c.global, c.cfg.GlobalMaxPerHour, time.Hour)
		if req.Recipient != "" {
			// The hour window is the one kept; the minute cap reads its tail.
			ts := consider(CapRecipientPerHour, c.recipient[req.Recipient], c.cfg.RecipientMaxPerHour, time.Hour)
			consider(CapRecipientPerMinute, ts, c.cfg.RecipientMaxPerMinute, time.Minute)
			c.setOrDelete(c.recipient, req.Recipient, ts)
		}
		if req.NewRecipient {
			c.newRecipients = consider(CapNewRecipientsDay, c.newRecipients, c.cfg.NewRecipientsPerDay, newRecipientWindow)
		}
	case classFeedback:
		c.feedbackGlobal = consider(CapFeedbackPerHour, c.feedbackGlobal, c.cfg.FeedbackMaxPerHour, time.Hour)
		if req.Recipient != "" {
			ts := consider(CapFeedbackPerMinute, c.feedbackRecipient[req.Recipient], c.cfg.FeedbackMaxPerMinute, time.Minute)
			c.setOrDelete(c.feedbackRecipient, req.Recipient, ts)
		}
	}

	if worst != nil {
		log.Printf("ANTIBAN cap hit cap=%s limit=%d window=%s type=%s retry_after=%s",
			worst.Cap, worst.Limit, worst.Window, req.Type, worst.RetryAfter.Round(time.Second))
		return worst
	}
	return nil
}

func (c *SendCaps) setOrDelete(m map[string][]time.Time, key string, ts []time.Time) {
	if len(ts) == 0 {
		delete(m, key)
		return
	}
	m[key] = ts
}

func (c *SendCaps) sweepLocked(now time.Time) {
	for key, ts := range c.recipient {
		c.setOrDelete(c.recipient, key, pruneBefore(ts, now.Add(-time.Hour)))
	}
	for key, ts := range c.feedbackRecipient {
		c.setOrDelete(c.feedbackRecipient, key, pruneBefore(ts, now.Add(-time.Minute)))
	}
}

// loadNewRecipients reads the persisted first-contact window. A missing file
// is a clean start. Anything else unreadable fails safe: the file is moved
// aside and the whole daily budget is treated as spent now, so first contacts
// pause for 24h while replies in existing chats carry on. Guessing "none sent"
// instead would be failing open on exactly the riskiest kind of send.
func (c *SendCaps) loadNewRecipients() []time.Time {
	path := c.cfg.CapsStatePath
	if path == "" || !c.cfg.CapsEnabled || c.cfg.NewRecipientsPerDay <= 0 {
		return nil
	}
	now := c.now()

	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	var state capsState
	if err == nil {
		err = json.Unmarshal(data, &state)
	}
	if err != nil {
		moved := quarantineStateFile(path, now)
		log.Printf("ANTIBAN ERROR caps state %s is unreadable (%v); moved to %q. Failing safe: first contacts are blocked for %s, replies are unaffected",
			path, err, moved, newRecipientWindow)
		spent := make([]time.Time, c.cfg.NewRecipientsPerDay)
		for i := range spent {
			spent[i] = now
		}
		return spent
	}
	return pruneBefore(normalizeTimes(state.NewRecipients, now), now.Add(-newRecipientWindow))
}

func (c *SendCaps) save(state capsState) error {
	if c.cfg.CapsStatePath == "" {
		return nil
	}
	return writeJSONAtomic(c.cfg.CapsStatePath, state)
}

// normalizeTimes makes a loaded window safe to prune. A wall clock that steps
// back while the bridge runs leaves entries out of order or in the future;
// that is not corruption, so it must not quarantine the file and block first
// contacts for a day. Future entries are clamped to now (they keep counting
// for a full window, the safe side) and the slice is sorted, because
// pruneBefore treats it as append-ordered.
func normalizeTimes(ts []time.Time, now time.Time) []time.Time {
	out := make([]time.Time, len(ts))
	for i, t := range ts {
		if t.After(now) {
			t = now
		}
		out[i] = t
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Before(out[j]) })
	return out
}

// pruneBefore drops entries at or before cutoff. ts is append-ordered, so
// the expired ones are a prefix.
func pruneBefore(ts []time.Time, cutoff time.Time) []time.Time {
	i := 0
	for i < len(ts) && !ts[i].After(cutoff) {
		i++
	}
	if i == 0 {
		return ts
	}
	// Copy so the backing array of a long-lived slice doesn't pin old entries.
	return append([]time.Time(nil), ts[i:]...)
}

// windowFull reports whether ts (already pruned to the window) has reached
// limit, and if so how long until enough entries expire to admit one more.
func windowFull(ts []time.Time, limit int, window time.Duration, now time.Time) (time.Duration, bool) {
	if limit <= 0 || len(ts) < limit {
		return 0, false
	}
	// With n >= limit entries, one more fits once the (n-limit+1)th oldest is
	// out of the window.
	oldestBlocking := ts[len(ts)-limit]
	retry := oldestBlocking.Add(window).Sub(now)
	if retry < time.Second {
		retry = time.Second
	}
	return retry, true
}

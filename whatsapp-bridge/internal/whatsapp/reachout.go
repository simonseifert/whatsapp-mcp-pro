package whatsapp

import (
	"context"
	"fmt"
	"os"
	"sync"
	"time"

	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"

	"whatsapp-bridge/internal/antiban"
	"whatsapp-bridge/internal/database"
)

// Reachout timelock.
//
// When WhatsApp restricts an account from starting new conversations it acks
// sends to contacts the account has never talked to with error 463. Pushing
// more first-contact messages through while restricted is what turns a
// temporary lock into a ban, so after a 463 the bridge refuses sends to NEW
// contacts (no stored messages in that chat) until the lock should have lifted,
// while replies in existing chats keep working.
//
// whatsmeow exposes no query for the lock state. The only structured signal is
// the server-pushed MEX notification events.NotifyAccountReachoutTimelock,
// which carries is_active and time_enforcement_ends; when it arrives it is
// authoritative. The 463 ack itself carries no expiry, so a 463 seen without a
// notification locks for REACHOUT_TIMELOCK_DEFAULT (24h unless configured).
// The state is persisted in messages.db (bridge_state) so a restart does not
// forget it and resume cold outreach mid-lock.

const reachoutStateKey = "reachout_timelock_until"

const defaultReachoutTimelock = 24 * time.Hour

type reachoutTracker struct {
	mu    sync.RWMutex
	until time.Time
	store *database.MessageStore
}

// reachoutDefaultDuration reads REACHOUT_TIMELOCK_DEFAULT (a Go duration such
// as "24h" or "90m"); invalid or non-positive values fall back to 24h.
func reachoutDefaultDuration() time.Duration {
	raw := os.Getenv("REACHOUT_TIMELOCK_DEFAULT")
	if raw == "" {
		return defaultReachoutTimelock
	}
	d, err := time.ParseDuration(raw)
	if err != nil || d <= 0 {
		return defaultReachoutTimelock
	}
	return d
}

// EnableReachoutTracking loads a persisted reachout timelock from the store and
// subscribes to WhatsApp's timelock notifications. Call once at startup, before
// Connect, so a notification delivered on connect is not missed.
func (c *Client) EnableReachoutTracking(store *database.MessageStore) {
	c.reachout.mu.Lock()
	c.reachout.store = store
	if store != nil {
		until, err := store.GetBridgeStateTime(reachoutStateKey)
		if err != nil {
			c.logger.Warnf("Could not load persisted reachout timelock: %v", err)
		} else if until.After(time.Now()) {
			c.reachout.until = until
			c.logger.Warnf("Reachout timelock active until %s (persisted): sends to new contacts are refused", until.Format(time.RFC3339))
		}
	}
	c.reachout.mu.Unlock()

	if c.Client != nil {
		c.AddEventHandler(func(evt any) {
			if v, ok := evt.(*events.NotifyAccountReachoutTimelock); ok {
				c.handleReachoutNotification(v)
			}
		})
	}
}

// ReachoutTimelockUntil returns when the current reachout timelock ends, or the
// zero time when none is active.
func (c *Client) ReachoutTimelockUntil() time.Time {
	c.reachout.mu.RLock()
	defer c.reachout.mu.RUnlock()
	if c.reachout.until.After(time.Now()) {
		return c.reachout.until
	}
	return time.Time{}
}

// setReachoutTimelock replaces the lock expiry (zero clears it) and persists it.
func (c *Client) setReachoutTimelock(until time.Time) {
	c.reachout.mu.Lock()
	c.reachout.until = until
	store := c.reachout.store
	c.reachout.mu.Unlock()
	if store != nil {
		if err := store.SetBridgeStateTime(reachoutStateKey, until); err != nil {
			c.logger.Warnf("Could not persist reachout timelock: %v", err)
		}
	}
}

// recordReachoutTimelock is called when a send was answered with error 463.
// It extends (never shortens) the lock, logs, and feeds the antiban health
// score. Returns the resulting expiry.
func (c *Client) recordReachoutTimelock(recipient types.JID) time.Time {
	until := time.Now().Add(reachoutDefaultDuration())
	if current := c.ReachoutTimelockUntil(); current.After(until) {
		until = current
	}
	c.setReachoutTimelock(until)
	c.logger.Warnf("WhatsApp answered a send to %s with error 463 (reachout timelock): refusing sends to new contacts until %s; replies to existing chats still allowed",
		recipient, until.Format(time.RFC3339))
	c.antiban.RecordEvent(antiban.EventReachoutTimelock)
	return until
}

func (c *Client) handleReachoutNotification(v *events.NotifyAccountReachoutTimelock) {
	if !v.IsActive {
		if !c.ReachoutTimelockUntil().IsZero() {
			c.logger.Infof("WhatsApp lifted the reachout timelock")
		}
		c.setReachoutTimelock(time.Time{})
		return
	}
	until := v.TimeEnforcementEnds.Time
	if until.IsZero() {
		until = time.Now().Add(reachoutDefaultDuration())
	}
	c.setReachoutTimelock(until)
	c.logger.Warnf("WhatsApp reachout timelock active (enforcement %q) until %s: sends to new contacts are refused",
		v.EnforcementType, until.Format(time.RFC3339))
	c.antiban.RecordEvent(antiban.EventReachoutTimelock)
}

// isUserChat reports whether a JID addresses one person (the only kind of chat
// the reachout lock applies to; groups, newsletters and broadcasts are not
// "starting a conversation with a stranger").
func isUserChat(jid types.JID) bool {
	return jid.Server == types.DefaultUserServer || jid.Server == types.HiddenUserServer
}

// checkReachoutGate refuses a send to a contact with no stored history while a
// reachout timelock is active. aliases are other identifiers of the same chat
// (the raw recipient as given, its LID/phone twin) so a chat filed under either
// form counts as existing. A store error fails closed: during a lock, guessing
// "existing" wrongly is exactly the send that makes the restriction worse.
func (c *Client) checkReachoutGate(store *database.MessageStore, recipient types.JID, aliases ...string) *SendError {
	until := c.ReachoutTimelockUntil()
	if until.IsZero() || !isUserChat(recipient) {
		return nil
	}

	candidates := append([]string{recipient.ToNonAD().String()}, aliases...)
	if c.Client != nil && c.Store != nil {
		if alt, err := c.Store.GetAltJID(context.Background(), recipient.ToNonAD()); err == nil && !alt.IsEmpty() {
			candidates = append(candidates, alt.String())
		}
	}

	existing := false
	if store != nil {
		var err error
		existing, err = store.ChatHasMessages(candidates...)
		if err != nil {
			c.logger.Warnf("Reachout gate could not check history for %s, treating as new contact: %v", recipient, err)
			existing = false
		}
	}
	if existing {
		return nil
	}
	return &SendError{
		Kind:  SendErrReachoutTimelock,
		Until: until,
		Err: fmt.Errorf("WhatsApp reachout timelock active until %s: sending to new contact %s refused (replies to existing chats still work)",
			until.Format(time.RFC3339), recipient),
	}
}

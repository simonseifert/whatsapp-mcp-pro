package whatsapp

import (
	"context"

	"go.mau.fi/whatsmeow/types"

	"whatsapp-bridge/internal/antiban"
	"whatsapp-bridge/internal/database"
)

// gateTarget is one account or chat an action will contact, with the raw string
// the caller passed (an alias for first-contact detection).
type gateTarget struct {
	jid types.JID
	raw string
}

// parseGateTargets parses JID strings for gateContacts; a bad one is a 400.
func parseGateTargets(raws []string, what string) ([]gateTarget, *SendError) {
	out := make([]gateTarget, 0, len(raws))
	for _, raw := range raws {
		jid, err := types.ParseJID(raw)
		if err != nil {
			return nil, newSendError(SendErrInvalid, "invalid %s JID %s: %w", what, raw, err)
		}
		out = append(out, gateTarget{jid: jid, raw: raw})
	}
	return out, nil
}

// gateContacts applies to a non-send action that still puts something in
// another account's chat (group create and add, rename, topic, disappearing
// timer) the checks a send gets: WHATSAPP_ALLOWLIST_JIDS, the reachout gate for
// first contacts, and the antiban pacing and caps. Every target is checked
// before any is counted, so a refused action leaves no trace in the caps.
//
// msgType picks the cap budget: antiban.Text for something that lands in a
// person's chat (each such target counts as a first contact when it has no
// history), antiban.Edit for a change to a group's own settings.
//
// On success the caller performs the action and then reports it with
// finishGate.
func (c *Client) gateContacts(ctx context.Context, store *database.MessageStore, msgType antiban.MessageType, targets []gateTarget) *SendError {
	for _, t := range targets {
		if se := c.checkAllowlist(t.jid); se != nil {
			return se
		}
	}
	for _, t := range targets {
		if se := c.checkReachoutGate(store, t.jid, t.raw); se != nil {
			return se
		}
	}
	for _, t := range targets {
		if err := c.antiban.Precheck(c.antibanRequest(store, msgType, t.jid, 0, t.raw)); err != nil {
			return classify(err, "antiban blocked action: %w")
		}
	}
	for _, t := range targets {
		if err := c.antiban.BeforeSend(ctx, c.antibanRequest(store, msgType, t.jid, 0, t.raw)); err != nil {
			return classify(err, "antiban blocked action: %w")
		}
	}
	return nil
}

// finishGate reports the outcome of a gated action to antiban's health score.
func (c *Client) finishGate(msgType antiban.MessageType, err error) {
	if err != nil {
		c.antiban.AfterSendFailed(msgType, err)
		return
	}
	c.antiban.AfterSend(msgType)
}

// allowlistsActive reports whether WHATSAPP_ALLOWLIST_JIDS restricts sending.
func (c *Client) allowlistsActive() bool {
	return c.cfg != nil && len(c.cfg.AllowlistJIDs) > 0
}

// gateWithoutRecipient gates an action that contacts nobody in particular
// (creating a newsletter). With an allowlist set there is no listed recipient
// to authorise it, so it is refused; otherwise only the antiban caps apply.
func (c *Client) gateWithoutRecipient(ctx context.Context, msgType antiban.MessageType) *SendError {
	if c.allowlistsActive() {
		return newSendError(SendErrForbidden, "action is blocked by the WHATSAPP_ALLOWLIST_JIDS safety gate: it has no listed recipient")
	}
	if err := c.antiban.BeforeSend(ctx, antiban.SendRequest{Type: msgType}); err != nil {
		return classify(err, "antiban blocked action: %w")
	}
	return nil
}

// disappearingTimerType picks the cap budget for a disappearing-timer change:
// in a one-to-one chat it is a message to that person (a first contact when the
// chat is new), in a group an edit to the group's settings.
func disappearingTimerType(chat types.JID) antiban.MessageType {
	if isUserChat(chat) {
		return antiban.Text
	}
	return antiban.Edit
}

// gateStore is the message store first-contact detection reads, the one
// EnableReachoutTracking was given. Nil (everything counts as a new contact)
// before that.
func (c *Client) gateStore() *database.MessageStore {
	c.reachout.mu.RLock()
	defer c.reachout.mu.RUnlock()
	return c.reachout.store
}

// gateGroupSetting gates a change to a group's own settings (name, topic).
func (c *Client) gateGroupSetting(groupJID string) *SendError {
	targets, se := parseGateTargets([]string{groupJID}, "group")
	if se != nil {
		return se
	}
	return c.gateContacts(context.Background(), c.gateStore(), antiban.Edit, targets)
}

// gateGroupAction gates removing, promoting, demoting or leaving: each puts a
// system message in the group, so the group goes through the allowlists and the
// antiban caps (an edit to the group). The people it acts on only need to pass
// the allowlist: they are already members, so it is not a first contact and
// the reachout gate does not apply.
func (c *Client) gateGroupAction(groupJID string, people []string) *SendError {
	targets, se := parseGateTargets(people, "participant")
	if se != nil {
		return se
	}
	for _, p := range targets {
		if se := c.checkAllowlist(p.jid); se != nil {
			return se
		}
	}
	return c.gateGroupSetting(groupJID)
}

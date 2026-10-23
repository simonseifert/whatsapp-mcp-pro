package whatsapp

import (
	"context"
	"fmt"
	"time"

	"go.mau.fi/whatsmeow/types"

	"whatsapp-bridge/internal/antiban"
	"whatsapp-bridge/internal/database"
)

// SendOptions describes a text or media send for SendMessageWithOptions.
type SendOptions struct {
	Recipient       string
	Message         string
	MediaPath       string
	QuotedMessageID string
	MentionedJIDs   []string

	// MessageID forces the WhatsApp message ID. An idempotent retry passes the
	// ID of the earlier, possibly-delivered attempt: recipients' devices key
	// messages on (chat, sender, id), so a resend with the same ID collapses
	// into the original instead of showing up twice. Empty = generate.
	MessageID string
	// BeforeDispatch, if set, is called with the message ID just before the
	// message is handed to WhatsApp, so the caller can persist it first. If the
	// process dies mid-send, the retry still knows which ID to reuse.
	BeforeDispatch func(messageID string)
}

// ReactionOptions describes a reaction for SendReactionWithOptions.
type ReactionOptions struct {
	ChatJID         string
	TargetMessageID string
	Emoji           string
	MessageID       string       // see SendOptions.MessageID
	BeforeDispatch  func(string) // see SendOptions.BeforeDispatch
}

// PollOptions describes a poll for CreatePollWithOptions.
type PollOptions struct {
	ChatJID        string
	Question       string
	Options        []string
	MultiSelect    bool
	MessageID      string       // see SendOptions.MessageID
	BeforeDispatch func(string) // see SendOptions.BeforeDispatch
}

// checkAllowlist applies the WHATSAPP_ALLOWLIST_JIDS safety gate to any
// outgoing message, not just /api/send: reactions, polls, edits and deletes
// all put a protocol message in the recipient's chat, and an autonomous sender
// confined to one chat must not be able to touch another through them.
// Matching is exact (IsRecipientAllowed). Nil when no allowlist is configured.
func (c *Client) checkAllowlist(chat types.JID) *SendError {
	if c.cfg == nil || len(c.cfg.AllowlistJIDs) == 0 {
		return nil
	}
	if IsRecipientAllowed(c.cfg.AllowlistJIDs, chat.ToNonAD()) {
		return nil
	}
	// A chat filed under its LID is the same person as the allowlisted phone
	// JID (SendMessage resolves LID->phone before its own check for this reason).
	if c.Client != nil && c.Store != nil && chat.Server == types.HiddenUserServer {
		if alt, err := c.Store.GetAltJID(context.Background(), chat.ToNonAD()); err == nil && !alt.IsEmpty() &&
			IsRecipientAllowed(c.cfg.AllowlistJIDs, alt) {
			return nil
		}
	}
	return newSendError(SendErrForbidden, "recipient %s is blocked by WHATSAPP_ALLOWLIST_JIDS safety gate", chat)
}

// antibanRequest describes a send to chat for antiban. First-contact detection
// only runs for types that open a conversation; a reaction or edit always
// targets a message that already exists.
func (c *Client) antibanRequest(store *database.MessageStore, msgType antiban.MessageType, chat types.JID, textLen int, aliases ...string) antiban.SendRequest {
	req := antiban.SendRequest{Type: msgType, Len: textLen, Recipient: c.capKey(chat)}
	if msgType == antiban.Text || msgType == antiban.Poll {
		req.NewRecipient = c.isNewContact(store, chat, aliases...)
	}
	return req
}

// capKey is the per-recipient cap bucket for chat. The same person can be
// addressed as a phone JID or as an @lid; keyed on the raw form, alternating
// between the two would get two budgets. A LID is resolved to its phone JID
// through the same LID map isNewContact and SendMessage use; an unmapped LID
// stays as given (there is nothing to unify it with yet).
func (c *Client) capKey(chat types.JID) string {
	chat = chat.ToNonAD()
	if chat.Server != types.HiddenUserServer {
		return chat.String()
	}
	lookup := c.altJIDLookup
	if lookup == nil {
		if c.Client == nil || c.Store == nil {
			return chat.String()
		}
		lookup = c.Store.GetAltJID
	}
	if pn, err := lookup(context.Background(), chat); err == nil && !pn.IsEmpty() {
		return pn.ToNonAD().String()
	}
	return chat.String()
}

// prepareMessageID returns the ID to send with (reusing want when given) and
// reports it to beforeDispatch.
func (c *Client) prepareMessageID(want string, beforeDispatch func(string)) types.MessageID {
	id := types.MessageID(want)
	if id == "" {
		id = c.GenerateMessageID()
	}
	if beforeDispatch != nil {
		beforeDispatch(string(id))
	}
	return id
}

// sendFailure classifies an error returned by whatsmeow's SendMessage and does
// the bookkeeping: a 463 arms the reachout timelock (which records its own
// antiban event); anything else is reported to antiban as a failed send.
func (c *Client) sendFailure(to types.JID, msgType antiban.MessageType, err error, format string) *SendError {
	if IsReachoutTimelockError(err) && to.Server == types.HiddenUserServer {
		// The server also answers 463 to a send addressed straight to a LID it
		// can't route (no phone mapping yet; see SendMessage). Arming the
		// reachout lock on that would block every new contact for a day over
		// one unroutable address, so it is reported as a failed send instead.
		// A real timelock still arrives via NotifyAccountReachoutTimelock.
		c.antiban.AfterSendFailed(msgType, err)
		return &SendError{
			Kind: SendErrUpstream,
			Err: fmt.Errorf("WhatsApp refused the send to %s with error 463; for a LID address this usually means no phone-number mapping is known yet, so it was not treated as a reachout timelock: %w",
				to, err),
		}
	}
	if IsReachoutTimelockError(err) {
		until := c.recordReachoutTimelock(to)
		return &SendError{
			Kind:  SendErrReachoutTimelock,
			Until: until,
			Err: fmt.Errorf("WhatsApp refused the send with error 463 (reachout timelock: this account may not start new chats right now); sends to new contacts are blocked until %s, replies to existing chats still work: %w",
				until.Format(time.RFC3339), err),
		}
	}
	c.antiban.AfterSendFailed(msgType, err)
	return classify(err, format)
}

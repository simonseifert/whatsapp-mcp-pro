package whatsapp

import (
	"fmt"
	"time"

	"go.mau.fi/whatsmeow/types"

	"whatsapp-bridge/internal/antiban"
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

package whatsapp

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"go.mau.fi/whatsmeow/types"

	"whatsapp-bridge/internal/database"
)

// ErrBareLIDRecipient is returned when a bare number given as a recipient is a
// known LID user part. Sending it as {digits}@s.whatsapp.net would address
// whoever owns that phone number, a stranger.
var ErrBareLIDRecipient = errors.New("bare number is a known LID")

// StoredSender is the form messages.sender is written in: the full JID
// without a device suffix. The user part alone cannot be told apart as phone
// number or LID later, which is what broke group reactions and quotes.
func StoredSender(jid types.JID) string {
	if jid.IsEmpty() {
		return ""
	}
	return jid.ToNonAD().String()
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// participantJID turns a stored messages.sender value into a full JID for a
// message key or quote participant. Old rows hold bare digits; parsing those
// with types.ParseJID yields a JID with the digits as *server* and no user,
// which whatsmeow sends without complaint and WhatsApp matches to nothing.
//
// A bare value is resolved, in order, as the chat's own JID when it is the
// other side of a non-group chat, as a LID when isKnownLID says so, and
// otherwise as a phone number. A full JID is kept as stored (device stripped):
// a LID stays a LID, since that is what the original message was keyed on.
func participantJID(sender, chatJID string, isKnownLID func(user string) bool) (types.JID, error) {
	sender = strings.TrimSpace(sender)
	if sender == "" {
		return types.EmptyJID, errors.New("empty sender")
	}
	if strings.Contains(sender, "@") {
		jid, err := types.ParseJID(sender)
		if err != nil {
			return types.EmptyJID, fmt.Errorf("parse sender %q: %w", sender, err)
		}
		if jid.User == "" || jid.Server == "" {
			return types.EmptyJID, fmt.Errorf("sender %q is not a user JID", sender)
		}
		return jid.ToNonAD(), nil
	}
	if !isDigits(sender) {
		return types.EmptyJID, fmt.Errorf("sender %q is neither a JID nor a number", sender)
	}
	if chat, err := types.ParseJID(chatJID); err == nil && chat.Server != types.GroupServer && chat.User == sender {
		return chat.ToNonAD(), nil
	}
	if isKnownLID != nil && isKnownLID(sender) {
		return types.JID{User: sender, Server: types.HiddenUserServer}, nil
	}
	return types.JID{User: sender, Server: types.DefaultUserServer}, nil
}

// bareRecipientJID turns a recipient given as a bare number into a phone JID,
// refusing digits that are a known LID (see ErrBareLIDRecipient).
func bareRecipientJID(recipient string, isKnownLID func(user string) bool) (types.JID, error) {
	recipient = strings.TrimSpace(recipient)
	if isKnownLID != nil && isKnownLID(recipient) {
		return types.EmptyJID, fmt.Errorf("%w: %s; pass the full JID (%s@lid, or the contact's phone JID) instead",
			ErrBareLIDRecipient, recipient, recipient)
	}
	return types.JID{User: recipient, Server: types.DefaultUserServer}, nil
}

// knownLIDFunc answers "is this bare user part a LID?" from whatsmeow's own
// LID map and the identity directory. Either can know a mapping the other
// does not yet: the directory is rebuilt periodically, the LID map is filled
// as messages arrive.
func (c *Client) knownLIDFunc(store *database.MessageStore) func(user string) bool {
	return func(user string) bool {
		if !isDigits(user) {
			return false
		}
		if c != nil && c.Client != nil && c.Store != nil && c.Store.LIDs != nil {
			lid := types.JID{User: user, Server: types.HiddenUserServer}
			if pn, err := c.Store.LIDs.GetPNForLID(context.Background(), lid); err == nil && !pn.IsEmpty() {
				return true
			}
		}
		if store != nil {
			if known, err := store.IsKnownLID(user); err == nil && known {
				return true
			}
		}
		return false
	}
}

// quotedParticipant resolves the ContextInfo.Participant for quoting a stored
// message in chat. One-to-one sends are always addressed to the phone JID (a
// send to a LID is refused with 463), so a LID author is mapped to its phone
// JID there, which is also what the quote code did before senders were stored
// in full. Group quotes keep the stored form, which matches the addressing
// mode the group used for the quoted message.
func (c *Client) quotedParticipant(store *database.MessageStore, sender string, chat types.JID, isFromMe bool) (types.JID, error) {
	if isFromMe && strings.TrimSpace(sender) == "" && c != nil && c.Client != nil && c.Store != nil && c.Store.ID != nil {
		return c.Store.ID.ToNonAD(), nil
	}
	p, err := participantJID(sender, chat.String(), c.knownLIDFunc(store))
	if err != nil {
		return types.EmptyJID, err
	}
	if chat.Server != types.GroupServer && p.Server == types.HiddenUserServer && c != nil && c.Client != nil && c.Store != nil {
		if pn, altErr := c.Store.GetAltJID(context.Background(), p); altErr == nil && !pn.IsEmpty() {
			return pn.ToNonAD(), nil
		}
	}
	return p, nil
}

// ChatJIDForms returns the chat JID the store files a conversation under (see
// NormalizeChatJID) and, when known, the other identity of the same 1:1 chat.
// alt is empty for groups and for chats whose LID/phone mapping is unknown.
func (c *Client) ChatJIDForms(chat types.JID) (canonical, alt types.JID) {
	chat = chat.ToNonAD()
	if chat.Server != types.HiddenUserServer && chat.Server != types.DefaultUserServer {
		return chat, types.EmptyJID
	}
	if c == nil || c.Client == nil || c.Store == nil {
		return chat, types.EmptyJID
	}
	canonical = c.NormalizeChatJID(chat)
	if canonical != chat {
		return canonical, chat
	}
	if chat.Server == types.DefaultUserServer {
		if other, err := c.Store.GetAltJID(context.Background(), chat); err == nil && !other.IsEmpty() {
			return chat, other.ToNonAD()
		}
	}
	return chat, types.EmptyJID
}

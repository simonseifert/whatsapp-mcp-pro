package whatsapp

import (
	"context"
	"strings"
	"time"

	"whatsapp-bridge/internal/database"

	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

// canonicalReactor picks the one JID a reactor is stored under, so the same
// person reacting once as a LID and once as a phone JID lands on one row of
// message_reactions instead of two. A phone JID wins; a LID is mapped to its
// phone JID through the event's SenderAlt, then lookupPN (whatsmeow's LID map).
//
// The two forms are never linked by equal digits: a LID's digits can be some
// other person's phone number. When no mapping is known the LID itself is
// returned, so the person gets a per-form row until the mapping arrives. That
// is an accepted limitation, not something to paper over by guessing.
// Device suffixes are always dropped.
func canonicalReactor(sender, senderAlt types.JID, lookupPN func(types.JID) types.JID) types.JID {
	sender = sender.ToNonAD()
	if sender.Server != types.HiddenUserServer {
		return sender
	}
	if alt := senderAlt.ToNonAD(); alt.Server == types.DefaultUserServer && alt.User != "" {
		return alt
	}
	if lookupPN != nil {
		if pn := lookupPN(sender).ToNonAD(); pn.Server == types.DefaultUserServer && pn.User != "" {
			return pn
		}
	}
	return sender
}

// lookupPN resolves a LID to its phone JID from whatsmeow's LID map. It
// returns an empty JID when the map is unavailable or has no entry.
func (c *Client) lookupPN(lid types.JID) types.JID {
	if c == nil || c.Client == nil || c.Store == nil || c.Store.LIDs == nil {
		return types.EmptyJID
	}
	pn, err := c.Store.LIDs.GetPNForLID(context.Background(), lid)
	if err != nil {
		return types.EmptyJID
	}
	return pn
}

// reactorSender is the stored sender string for a reactor.
func (c *Client) reactorSender(sender, senderAlt types.JID) string {
	return StoredSender(canonicalReactor(sender, senderAlt, c.lookupPN))
}

// reactionTime uses the reactor's own claimed time when present, clamped to
// the moment the server relayed it: a forged future time must not win the
// "newer wins" guard in StoreReaction (same rule as edits).
func reactionTime(claimedMS int64, received time.Time) time.Time {
	if claimedMS <= 0 {
		return received
	}
	t := time.UnixMilli(claimedMS)
	if !received.IsZero() && t.After(received) {
		return received
	}
	return t
}

// historyReactionTime is reactionTime for replayed history: with no claimed
// time the reaction is dated by the message it came with, never "now", and a
// claimed time is capped at now. Dating a replay "now" would let it outrank,
// and overwrite, a real live reaction that happened since.
func historyReactionTime(claimedMS int64, msgTime, now time.Time) time.Time {
	if claimedMS <= 0 {
		return msgTime
	}
	return reactionTime(claimedMS, now)
}

// reactionFromMessage extracts the reaction a live message carries. sender is
// the already-canonical stored sender. An empty emoji is a removal.
func reactionFromMessage(chatJID, sender string, msg *events.Message) (database.Reaction, bool) {
	if msg == nil || msg.Message == nil {
		return database.Reaction{}, false
	}
	rm := msg.Message.GetReactionMessage()
	if rm == nil || rm.GetKey().GetID() == "" {
		return database.Reaction{}, false
	}
	return database.Reaction{
		ChatJID:  chatJID,
		TargetID: rm.GetKey().GetID(),
		Sender:   sender,
		Emoji:    rm.GetText(),
		At:       reactionTime(rm.GetSenderTimestampMS(), msg.Info.Timestamp),
	}, true
}

// reactionsFromHistory extracts the reactions a history-sync message carries:
// its Reactions list (all aimed at that message) and, when the message is
// itself a reaction, that one. senderOf resolves a message key to the stored
// reactor, and reports false when it cannot do so reliably; such reactions are
// skipped rather than attributed to a guess. Targets that are imported IDs are
// skipped too.
func reactionsFromHistory(chatJID string, wmi *waWeb.WebMessageInfo, senderOf func(*waCommon.MessageKey) (string, bool)) []database.Reaction {
	if wmi == nil {
		return nil
	}
	now := time.Now()
	var out []database.Reaction
	msgTime := time.Unix(int64(wmi.GetMessageTimestamp()), 0)
	add := func(target string, key *waCommon.MessageKey, emoji string, claimedMS int64) {
		if target == "" || database.IsImportedMessageID(target) {
			return
		}
		sender, ok := senderOf(key)
		if !ok || sender == "" {
			return
		}
		out = append(out, database.Reaction{
			ChatJID: chatJID, TargetID: target, Sender: sender, Emoji: emoji,
			At: historyReactionTime(claimedMS, msgTime, now),
		})
	}
	target := wmi.GetKey().GetID()
	for _, r := range wmi.GetReactions() {
		add(target, r.GetKey(), r.GetText(), r.GetSenderTimestampMS())
	}
	if rm := wmi.GetMessage().GetReactionMessage(); rm != nil {
		add(rm.GetKey().GetID(), wmi.GetKey(), rm.GetText(), rm.GetSenderTimestampMS())
	}
	return out
}

// historyReactorSender resolves who sent a history reaction from its key: our
// own JID for FromMe, the participant in a group, the peer in a 1:1 chat.
// History carries no SenderAlt, so only the LID map can unify the forms.
func (c *Client) historyReactorSender(chat types.JID) func(*waCommon.MessageKey) (string, bool) {
	return func(key *waCommon.MessageKey) (string, bool) {
		if key == nil {
			return "", false
		}
		if key.GetFromMe() {
			if c.Store == nil || c.Store.ID == nil {
				return "", false
			}
			return c.reactorSender(*c.Store.ID, types.EmptyJID), true
		}
		if p := key.GetParticipant(); p != "" {
			jid, err := types.ParseJID(p)
			if err != nil || jid.User == "" {
				return "", false
			}
			return c.reactorSender(jid, types.EmptyJID), true
		}
		if chat.Server == types.GroupServer || strings.HasPrefix(chat.Server, "broadcast") || chat.Server == types.NewsletterServer {
			return "", false
		}
		return c.reactorSender(chat, types.EmptyJID), true
	}
}

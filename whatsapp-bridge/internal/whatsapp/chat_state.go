package whatsapp

import (
	"time"

	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"

	"whatsapp-bridge/internal/database"
)

// msThreshold separates seconds from milliseconds: 1e12 seconds is the year
// 33658, while 1e12 milliseconds is September 2001.
const msThreshold = 1_000_000_000_000

// muteEndSeconds converts a WhatsApp mute end time to unix seconds. whatsmeow
// reads MuteAction.MuteEndTimestamp as milliseconds (time.UnixMilli), but the
// history-sync Conversation.MuteEndTime carries no unit in its proto, so values
// above msThreshold are treated as milliseconds and anything smaller as
// seconds. Negative means muted forever and maps to -1; zero means not muted.
func muteEndSeconds(v int64) int64 {
	switch {
	case v < 0:
		return -1
	case v > msThreshold:
		return v / 1000
	default:
		return v
	}
}

// ownReadReceipt reports when the user read a chat on one of their own devices.
func ownReadReceipt(r *events.Receipt) (at time.Time, ok bool) {
	if r == nil || !r.IsFromMe {
		return time.Time{}, false
	}
	if r.Type != types.ReceiptTypeRead && r.Type != types.ReceiptTypeReadSelf {
		return time.Time{}, false
	}
	return r.Timestamp, true
}

// HandleChatStateEvent stores pin, mute, archive and read changes. Only
// *events.Pin, *events.Mute, *events.Archive, *events.MarkChatAsRead and
// *events.Receipt are handled; other events are ignored.
func (c *Client) HandleChatStateEvent(store *database.MessageStore, evt any) {
	var (
		chat types.JID
		err  error
	)
	switch v := evt.(type) {
	case *events.Pin:
		if v.Action == nil {
			return
		}
		chat = c.NormalizeChatJID(v.JID)
		err = store.SetChatPinned(chat.String(), v.Action.GetPinned(), v.Timestamp)
	case *events.Archive:
		if v.Action == nil {
			return
		}
		chat = c.NormalizeChatJID(v.JID)
		err = store.SetChatArchived(chat.String(), v.Action.GetArchived(), v.Timestamp)
	case *events.Mute:
		if v.Action == nil {
			return
		}
		chat = c.NormalizeChatJID(v.JID)
		var until int64
		if v.Action.GetMuted() {
			until = muteEndSeconds(v.Action.GetMuteEndTimestamp())
		}
		err = store.SetChatMuted(chat.String(), until, v.Timestamp)
	case *events.MarkChatAsRead:
		if v.Action == nil {
			return
		}
		chat = c.NormalizeChatJID(v.JID)
		if v.Action.GetRead() {
			err = store.MarkChatReadAt(chat.String(), v.Timestamp)
		} else {
			err = store.SetChatMarkedUnread(chat.String(), true, v.Timestamp)
		}
	case *events.Receipt:
		at, ok := ownReadReceipt(v)
		if !ok || at.IsZero() { // never default a missing time to now
			return
		}
		chat = c.NormalizeChatJID(v.Chat)
		err = store.MarkChatReadAt(chat.String(), at)
	default:
		return
	}
	if err != nil {
		c.logger.Warnf("Failed to store chat state for %s: %v", chat, err)
	}
}

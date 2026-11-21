package whatsapp

import (
	"testing"
	"time"

	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/proto/waSyncAction"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

func TestOwnReadReceipt(t *testing.T) {
	at := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
	tests := []struct {
		name   string
		fromMe bool
		typ    types.ReceiptType
		want   bool
	}{
		{"own read", true, types.ReceiptTypeRead, true},
		{"own read-self", true, types.ReceiptTypeReadSelf, true},
		{"not from me", false, types.ReceiptTypeRead, false},
		{"own delivered", true, types.ReceiptTypeDelivered, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			r := &events.Receipt{
				MessageSource: types.MessageSource{IsFromMe: tt.fromMe},
				Timestamp:     at, Type: tt.typ,
			}
			got, ok := ownReadReceipt(r)
			if ok != tt.want {
				t.Fatalf("ok = %v, want %v", ok, tt.want)
			}
			if ok && !got.Equal(at) {
				t.Errorf("at = %v, want %v", got, at)
			}
		})
	}
}

func TestMuteEndSeconds(t *testing.T) {
	tests := []struct {
		name string
		in   int64
		want int64
	}{
		{"forever", -1, -1},
		{"unmuted", 0, 0},
		{"milliseconds", 1900000000123, 1900000000},
		{"already seconds", 1900000000, 1900000000},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := muteEndSeconds(tt.in); got != tt.want {
				t.Errorf("muteEndSeconds(%d) = %d, want %d", tt.in, got, tt.want)
			}
		})
	}
}

func TestHandleChatStateEvent(t *testing.T) {
	store := openTestStore(t)
	c := &Client{logger: waLog.Noop}
	chat := types.JID{User: "123", Server: types.DefaultUserServer}
	group := types.JID{User: "G", Server: types.GroupServer}
	t1 := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
	t2 := t1.Add(time.Hour)

	c.HandleChatStateEvent(store, &events.Pin{JID: chat, Timestamp: t1, Action: &waSyncAction.PinAction{Pinned: proto.Bool(true)}})
	c.HandleChatStateEvent(store, &events.Archive{JID: chat, Timestamp: t1, Action: &waSyncAction.ArchiveChatAction{Archived: proto.Bool(true)}})
	c.HandleChatStateEvent(store, &events.Mute{JID: chat, Timestamp: t1, Action: &waSyncAction.MuteAction{Muted: proto.Bool(true), MuteEndTimestamp: proto.Int64(1900000000000)}})
	c.HandleChatStateEvent(store, &events.MarkChatAsRead{JID: chat, Timestamp: t1, Action: &waSyncAction.MarkChatAsReadAction{Read: proto.Bool(false)}})
	st, err := store.GetChatState(chat.String())
	if err != nil {
		t.Fatal(err)
	}
	if !st.Pinned || !st.Archived || st.MutedUntil != 1900000000 || !st.MarkedUnread {
		t.Fatalf("state after events = %+v", st)
	}

	c.HandleChatStateEvent(store, &events.Mute{JID: chat, Timestamp: t2, Action: &waSyncAction.MuteAction{Muted: proto.Bool(false), MuteEndTimestamp: proto.Int64(1900000000000)}})
	c.HandleChatStateEvent(store, &events.MarkChatAsRead{JID: chat, Timestamp: t2, Action: &waSyncAction.MarkChatAsReadAction{Read: proto.Bool(true)}})
	st, _ = store.GetChatState(chat.String())
	if st.MutedUntil != 0 {
		t.Errorf("MutedUntil = %d after unmute, want 0", st.MutedUntil)
	}
	if st.MarkedUnread || st.LastReadAt == nil || !st.LastReadAt.Equal(t2) {
		t.Errorf("read state = %+v", st)
	}

	// Own read receipt in a group, and someone else's receipt is ignored.
	c.HandleChatStateEvent(store, &events.Receipt{
		MessageSource: types.MessageSource{Chat: group, IsFromMe: true, IsGroup: true},
		Timestamp:     t2, Type: types.ReceiptTypeRead,
	})
	c.HandleChatStateEvent(store, &events.Receipt{
		MessageSource: types.MessageSource{Chat: chat, IsFromMe: false},
		Timestamp:     t2.Add(time.Hour), Type: types.ReceiptTypeRead,
	})
	gst, _ := store.GetChatState(group.String())
	if gst.LastReadAt == nil || !gst.LastReadAt.Equal(t2) {
		t.Errorf("group LastReadAt = %v, want %v", gst.LastReadAt, t2)
	}
	st, _ = store.GetChatState(chat.String())
	if !st.LastReadAt.Equal(t2) {
		t.Errorf("foreign receipt moved LastReadAt to %v", st.LastReadAt)
	}
}

func TestSeedsChatState(t *testing.T) {
	for _, tt := range []struct {
		typ  waHistorySync.HistorySync_HistorySyncType
		want bool
	}{
		{waHistorySync.HistorySync_INITIAL_BOOTSTRAP, true},
		{waHistorySync.HistorySync_RECENT, true},
		{waHistorySync.HistorySync_FULL, true},
		{waHistorySync.HistorySync_ON_DEMAND, false},
		{waHistorySync.HistorySync_PUSH_NAME, false},
		{waHistorySync.HistorySync_NON_BLOCKING_DATA, false},
		{waHistorySync.HistorySync_INITIAL_STATUS_V3, false},
	} {
		if got := seedsChatState(tt.typ); got != tt.want {
			t.Errorf("seedsChatState(%v) = %v, want %v", tt.typ, got, tt.want)
		}
	}
}

func TestChatSeedFromConversationOnlyPresentFields(t *testing.T) {
	at := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
	empty := chatSeedFromConversation(&waHistorySync.Conversation{}, at)
	if empty.Pinned != nil || empty.Archived != nil || empty.MarkedUnread != nil || empty.MutedUntil != nil || empty.UnreadCount != nil {
		t.Errorf("empty conversation produced a seed: %+v", empty)
	}
	full := chatSeedFromConversation(&waHistorySync.Conversation{
		Pinned: proto.Uint32(0), Archived: proto.Bool(true), MarkedAsUnread: proto.Bool(false),
		MuteEndTime: proto.Uint64(1900000000), UnreadCount: proto.Uint32(0),
	}, at)
	if full.Pinned == nil || *full.Pinned || full.Archived == nil || !*full.Archived ||
		full.MarkedUnread == nil || *full.MarkedUnread || full.MutedUntil == nil || *full.MutedUntil != 1900000000 ||
		full.UnreadCount == nil || *full.UnreadCount != 0 {
		t.Errorf("full conversation seed = %+v", full)
	}
}

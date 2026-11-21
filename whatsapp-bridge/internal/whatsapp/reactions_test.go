package whatsapp

import (
	"testing"
	"time"

	"whatsapp-bridge/internal/database"

	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

func TestCanonicalReactor(t *testing.T) {
	pn := types.NewJID("123", types.DefaultUserServer)
	lid := types.NewJID("999", types.HiddenUserServer)
	hit := func(types.JID) types.JID { return pn }
	miss := func(types.JID) types.JID { return types.EmptyJID }
	tests := []struct {
		name   string
		sender types.JID
		alt    types.JID
		lookup func(types.JID) types.JID
		want   types.JID
	}{
		{"phone sender", pn, types.EmptyJID, miss, pn},
		{"lid with phone alt", lid, pn, miss, pn},
		{"lid with lookup hit", lid, types.EmptyJID, hit, pn},
		{"lid with lookup miss", lid, types.EmptyJID, miss, lid},
		{"lid with nil lookup", lid, types.EmptyJID, nil, lid},
		{"device suffixed phone", types.JID{User: "123", Device: 3, Server: types.DefaultUserServer}, types.EmptyJID, miss, pn},
		{"equal digits are not linked", types.NewJID("123", types.HiddenUserServer), types.EmptyJID, miss, types.NewJID("123", types.HiddenUserServer)},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := canonicalReactor(tt.sender, tt.alt, tt.lookup); got != tt.want {
				t.Errorf("got %v, want %v", got, tt.want)
			}
		})
	}
}

func TestReactionFromMessage(t *testing.T) {
	recv := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	claimed := recv.Add(-time.Minute)
	mk := func(text string, ms int64) *events.Message {
		return &events.Message{
			Info: types.MessageInfo{Timestamp: recv},
			Message: &waE2E.Message{ReactionMessage: &waE2E.ReactionMessage{
				Key: &waCommon.MessageKey{ID: proto.String("M1")}, Text: proto.String(text), SenderTimestampMS: proto.Int64(ms),
			}},
		}
	}
	tests := []struct {
		name   string
		msg    *events.Message
		wantOK bool
		want   database.Reaction
	}{
		{"claimed time", mk("👍", claimed.UnixMilli()), true, database.Reaction{ChatJID: "c", TargetID: "M1", Sender: "s", Emoji: "👍", At: claimed}},
		{"removal", mk("", claimed.UnixMilli()), true, database.Reaction{ChatJID: "c", TargetID: "M1", Sender: "s", Emoji: "", At: claimed}},
		{"no claimed time", mk("👍", 0), true, database.Reaction{ChatJID: "c", TargetID: "M1", Sender: "s", Emoji: "👍", At: recv}},
		{"future time clamped", mk("👍", recv.Add(24*time.Hour).UnixMilli()), true, database.Reaction{ChatJID: "c", TargetID: "M1", Sender: "s", Emoji: "👍", At: recv}},
		{"not a reaction", &events.Message{Message: &waE2E.Message{Conversation: proto.String("hi")}}, false, database.Reaction{}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, ok := reactionFromMessage("c", "s", tt.msg)
			if ok != tt.wantOK {
				t.Fatalf("ok = %v, want %v", ok, tt.wantOK)
			}
			if ok && (got.ChatJID != tt.want.ChatJID || got.TargetID != tt.want.TargetID || got.Sender != tt.want.Sender || got.Emoji != tt.want.Emoji || !got.At.Equal(tt.want.At)) {
				t.Errorf("got %+v, want %+v", got, tt.want)
			}
		})
	}
}

func TestReactionsFromHistory(t *testing.T) {
	ts := time.Date(2026, 10, 1, 9, 0, 0, 0, time.UTC)
	wmi := &waWeb.WebMessageInfo{
		Key: &waCommon.MessageKey{ID: proto.String("M1")},
		Reactions: []*waWeb.Reaction{
			{Key: &waCommon.MessageKey{ID: proto.String("R1"), Participant: proto.String("111@s.whatsapp.net")}, Text: proto.String("👍"), SenderTimestampMS: proto.Int64(ts.UnixMilli())},
			{Key: &waCommon.MessageKey{ID: proto.String("R2"), FromMe: proto.Bool(true)}, Text: proto.String("❤️"), SenderTimestampMS: proto.Int64(ts.UnixMilli())},
			{Key: &waCommon.MessageKey{ID: proto.String("R3")}, Text: proto.String("😂")}, // unresolvable: skipped
		},
	}
	senderOf := func(k *waCommon.MessageKey) (string, bool) {
		switch {
		case k.GetFromMe():
			return "999@s.whatsapp.net", true
		case k.GetParticipant() != "":
			return k.GetParticipant(), true
		}
		return "", false
	}
	got := reactionsFromHistory("g@g.us", wmi, senderOf)
	if len(got) != 2 {
		t.Fatalf("got %d reactions, want 2: %+v", len(got), got)
	}
	for i, want := range []struct{ sender, emoji string }{{"111@s.whatsapp.net", "👍"}, {"999@s.whatsapp.net", "❤️"}} {
		if got[i].TargetID != "M1" || got[i].Sender != want.sender || got[i].Emoji != want.emoji || got[i].ChatJID != "g@g.us" {
			t.Errorf("reaction %d = %+v", i, got[i])
		}
	}

	t.Run("imported target skipped", func(t *testing.T) {
		imp := &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{ID: proto.String("import-abc")}, Reactions: wmi.Reactions}
		if got := reactionsFromHistory("g@g.us", imp, senderOf); len(got) != 0 {
			t.Errorf("got %+v, want none", got)
		}
	})
	t.Run("reaction message inside history", func(t *testing.T) {
		rm := &waWeb.WebMessageInfo{
			Key:              &waCommon.MessageKey{ID: proto.String("R9"), Participant: proto.String("111@s.whatsapp.net")},
			MessageTimestamp: proto.Uint64(uint64(ts.Unix())),
			Message: &waE2E.Message{ReactionMessage: &waE2E.ReactionMessage{
				Key: &waCommon.MessageKey{ID: proto.String("M5")}, Text: proto.String("🔥")}},
		}
		got := reactionsFromHistory("g@g.us", rm, senderOf)
		if len(got) != 1 || got[0].TargetID != "M5" || got[0].Emoji != "🔥" || !got[0].At.Equal(ts) {
			t.Errorf("got %+v", got)
		}
	})
}

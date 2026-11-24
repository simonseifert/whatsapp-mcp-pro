package whatsapp

import (
	"testing"
	"time"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

type recordingWebhooks struct{ got []string }

func (r *recordingWebhooks) ProcessMessage(_ interface{}, msg *events.Message, _ string) {
	r.got = append(r.got, msg.Info.ID)
}

// A colliding message (another sender, no link) must not overwrite the stored
// row or its poll options, but it is still processed: webhooks see it.
func TestHandleMessageIDReuseKeepsRowButStillDelivers(t *testing.T) {
	store := newReachoutTestStore(t)
	c := &Client{logger: waLog.Noop}
	chat := types.NewJID("120363000000000000", types.GroupServer)
	simon := types.NewJID("4915100000000", types.DefaultUserServer)
	mallory := types.NewJID("4915199999999", types.DefaultUserServer)
	at := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
	hooks := &recordingWebhooks{}

	mk := func(from types.JID, text string) *events.Message {
		return &events.Message{
			Info: types.MessageInfo{
				MessageSource: types.MessageSource{Chat: chat, Sender: from},
				ID:            "REUSED1", Timestamp: at,
			},
			Message: &waE2E.Message{Conversation: proto.String(text)},
		}
	}
	c.HandleMessage(store, hooks, mk(simon, "original"))
	c.HandleMessage(store, hooks, mk(mallory, "forged"))

	var sender, content string
	if err := store.GetDB().QueryRow(`SELECT sender, content FROM messages WHERE id='REUSED1'`).Scan(&sender, &content); err != nil {
		t.Fatal(err)
	}
	if sender != simon.String() || content != "original" {
		t.Fatalf("row = %q / %q, want Simon's original", sender, content)
	}
	if len(hooks.got) != 2 {
		t.Fatalf("webhook saw %d events, want 2 (the collision is still delivered)", len(hooks.got))
	}
}

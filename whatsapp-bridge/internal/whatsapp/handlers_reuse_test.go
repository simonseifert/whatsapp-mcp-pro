package whatsapp

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

type recordingWebhooks struct{ got []string }

func (r *recordingWebhooks) ProcessMessage(_ interface{}, _ *events.Message, _ string) {
	r.got = append(r.got, "normal")
}

func (r *recordingWebhooks) ProcessCollidingMessage(_ interface{}, _ *events.Message, _ string) {
	r.got = append(r.got, "collision")
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
	if len(hooks.got) != 2 || hooks.got[0] != "normal" || hooks.got[1] != "collision" {
		t.Fatalf("webhook deliveries = %v, want [normal collision]", hooks.got)
	}
}

// A colliding message's media is not downloaded (the saved file is keyed by
// the ID), while the same media under a fresh ID is.
func TestHandleMessageIDReuseSkipsAutoDownload(t *testing.T) {
	t.Chdir(t.TempDir())
	mediaSlotsOnce = sync.Once{}
	t.Cleanup(func() { mediaSlotsOnce = sync.Once{} })
	store := newReachoutTestStore(t)
	var downloads int32
	c := &Client{logger: waLog.Noop, rawDownloadToFile: func(_ context.Context, _ whatsmeow.DownloadableMessage, f whatsmeow.File) error {
		atomic.AddInt32(&downloads, 1)
		_, err := f.Write([]byte("x"))
		return err
	}}
	chat := types.NewJID("120363000000000000", types.GroupServer)
	simon := types.NewJID("4915100000000", types.DefaultUserServer)
	mallory := types.NewJID("4915199999999", types.DefaultUserServer)
	at := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
	doc := func(from types.JID, id string) *events.Message {
		return &events.Message{
			Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: chat, Sender: from}, ID: id, Timestamp: at},
			Message: &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{
				URL: proto.String("https://mmg.whatsapp.net/x"), DirectPath: proto.String("/x"), MediaKey: []byte("k"),
				FileLength: proto.Uint64(1), FileName: proto.String("a.pdf"), FileSHA256: []byte("sha"), FileEncSHA256: []byte("enc"),
			}},
		}
	}
	waitFor := func(want int32) {
		deadline := time.Now().Add(2 * time.Second)
		for atomic.LoadInt32(&downloads) < want && time.Now().Before(deadline) {
			time.Sleep(5 * time.Millisecond)
		}
	}

	c.HandleMessage(store, nil, doc(simon, "DOC1"))
	waitFor(1)
	if n := atomic.LoadInt32(&downloads); n != 1 {
		t.Fatalf("the genuine message downloaded %d times, want 1", n)
	}
	c.HandleMessage(store, nil, doc(mallory, "DOC1")) // collides
	time.Sleep(200 * time.Millisecond)
	if n := atomic.LoadInt32(&downloads); n != 1 {
		t.Fatalf("the colliding message started a download (%d total)", n)
	}
}

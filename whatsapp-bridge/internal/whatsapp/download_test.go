package whatsapp

import (
	"errors"
	"fmt"
	"testing"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

func TestIsMediaGone(t *testing.T) {
	for _, err := range []error{whatsmeow.ErrMediaDownloadFailedWith403, whatsmeow.ErrMediaDownloadFailedWith404, whatsmeow.ErrMediaDownloadFailedWith410} {
		if !isMediaGone(fmt.Errorf("wrapped: %w", err)) {
			t.Errorf("isMediaGone(%v) = false, want true", err)
		}
	}
	if isMediaGone(errors.New("timeout")) || isMediaGone(nil) {
		t.Error("unrelated errors must not trigger a media retry")
	}
}

func TestHandleMediaRetryWakesWaiterOnce(t *testing.T) {
	id := types.MessageID("3EB0TEST")
	ch := make(chan *events.MediaRetry, 1)
	mediaRetryMu.Lock()
	mediaRetryWaiters[id] = append(mediaRetryWaiters[id], ch)
	mediaRetryMu.Unlock()

	c := &Client{}
	c.HandleMediaRetry(&events.MediaRetry{MessageID: id})
	c.HandleMediaRetry(&events.MediaRetry{MessageID: id}) // no waiter left: dropped

	if got := len(ch); got != 1 {
		t.Fatalf("waiter got %d events, want 1", got)
	}
	mediaRetryMu.Lock()
	_, left := mediaRetryWaiters[id]
	mediaRetryMu.Unlock()
	if left {
		t.Error("waiter entry not cleared after delivery")
	}
}

func TestSetDirectPathClearsStaleURL(t *testing.T) {
	url := "https://mmg.whatsapp.net/old"
	msg := &waE2E.ImageMessage{URL: &url}
	if !setDirectPath(msg, "/v/t62/new") {
		t.Fatal("setDirectPath refused an image")
	}
	if msg.GetDirectPath() != "/v/t62/new" || msg.GetURL() != "" {
		t.Errorf("got path=%q url=%q", msg.GetDirectPath(), msg.GetURL())
	}
}

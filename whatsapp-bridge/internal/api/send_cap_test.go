package api

import (
	"fmt"
	"net/http"
	"strconv"
	"testing"
	"time"

	"whatsapp-bridge/internal/antiban"
	"whatsapp-bridge/internal/types"
	"whatsapp-bridge/internal/whatsapp"
)

func capErr(retry time.Duration) *antiban.CapError {
	return &antiban.CapError{Cap: antiban.CapRecipientPerMinute, Limit: 8, Window: time.Minute, RetryAfter: retry}
}

// TestSendCapIs429OnEveryEndpoint: a cap hit must look the same from every
// message-creating endpoint: 429, code send_cap, a Retry-After header and a
// retry_after body field, so the MCP marks it retryable and says when.
func TestSendCapIs429OnEveryEndpoint(t *testing.T) {
	wrapped := &whatsapp.SendError{
		Kind:  whatsapp.SendErrCapReached,
		Until: time.Now().Add(45 * time.Second),
		Err:   fmt.Errorf("antiban blocked send: %w", capErr(45*time.Second)),
	}
	endpoints := []struct {
		name    string
		handler func(s *Server) http.HandlerFunc
		body    map[string]any
	}{
		{"send", func(s *Server) http.HandlerFunc { return s.handleSendMessage },
			map[string]any{"recipient": "4915112345678@s.whatsapp.net", "message": "hi"}},
		{"reaction", func(s *Server) http.HandlerFunc { return s.handleReaction },
			map[string]any{"chat_jid": "4915112345678@s.whatsapp.net", "message_id": "M", "emoji": "x"}},
		{"poll", func(s *Server) http.HandlerFunc { return s.handleCreatePoll },
			map[string]any{"chat_jid": "4915112345678@s.whatsapp.net", "question": "q", "options": []string{"a", "b"}}},
		{"edit", func(s *Server) http.HandlerFunc { return s.handleEditMessage },
			map[string]any{"chat_jid": "4915112345678@s.whatsapp.net", "message_id": "M", "new_content": "fixed"}},
		{"delete", func(s *Server) http.HandlerFunc { return s.handleDeleteMessage },
			map[string]any{"chat_jid": "123456789012345@lid", "message_id": "M"}},
	}
	errs := []struct {
		name string
		err  error
	}{
		{"classified", wrapped},
		// A bare CapError (no SendError wrapper) must map the same way.
		{"bare", capErr(45 * time.Second)},
	}
	for _, ep := range endpoints {
		for _, e := range errs {
			t.Run(ep.name+"/"+e.name, func(t *testing.T) {
				f := &fakeSender{result: func(int) types.SendResult {
					return types.SendResult{Success: false, Error: e.err.Error(), Err: e.err}
				}}
				s := newSendTestServer(t, f)
				rec := post(t, ep.handler(s), ep.body, nil)
				if rec.Code != http.StatusTooManyRequests {
					t.Fatalf("status %d, want 429: %s", rec.Code, rec.Body)
				}
				secs, err := strconv.Atoi(rec.Header().Get("Retry-After"))
				if err != nil || secs < 1 || secs > 46 {
					t.Fatalf("Retry-After = %q, want 1..46s", rec.Header().Get("Retry-After"))
				}
				body := decode(t, rec)
				if body["code"] != string(whatsapp.SendErrCapReached) {
					t.Fatalf("code = %v, want send_cap", body["code"])
				}
				if _, err := time.Parse(time.RFC3339, fmt.Sprint(body["retry_after"])); err != nil {
					t.Fatalf("retry_after = %v: %v", body["retry_after"], err)
				}
			})
		}
	}
}

// TestSendCapDoesNotConsumeIdempotencyKey: a capped send never went out, so
// retrying it with the same key after the window must really send, not
// replay the 429 or report it as delivered.
func TestSendCapDoesNotConsumeIdempotencyKey(t *testing.T) {
	f := &fakeSender{result: func(n int) types.SendResult {
		if n == 1 {
			err := capErr(time.Second)
			return types.SendResult{Success: false, Error: err.Error(), Err: err}
		}
		return types.SendResult{Success: true}
	}}
	s := newSendTestServer(t, f)
	body := map[string]any{"recipient": "4915112345678@s.whatsapp.net", "message": "hi", "idempotency_key": "capped"}

	if rec := post(t, s.handleSendMessage, body, nil); rec.Code != http.StatusTooManyRequests {
		t.Fatalf("first: %d, want 429", rec.Code)
	}
	rec := post(t, s.handleSendMessage, body, nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("retry: %d, want 200: %s", rec.Code, rec.Body)
	}
	if rec.Header().Get(replayHeader) != "" {
		t.Fatal("retry after a cap hit was served as a replay instead of being sent")
	}
	if f.callCount() != 2 {
		t.Fatalf("calls = %d, want 2", f.callCount())
	}
}

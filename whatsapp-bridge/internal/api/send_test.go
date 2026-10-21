package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"

	"whatsapp-bridge/internal/antiban"
	"whatsapp-bridge/internal/database"
	"whatsapp-bridge/internal/types"
	"whatsapp-bridge/internal/whatsapp"
)

// fakeSender records calls and returns whatever result() says.
type fakeSender struct {
	mu        sync.Mutex
	calls     int
	priorIDs  []string
	deadlines []bool
	result    func(n int) types.SendResult
	block     chan struct{} // if set, sends wait on it
	entered   chan struct{} // if set, signalled when a send starts
}

func (f *fakeSender) record(ctx context.Context, priorID string, beforeDispatch func(string)) types.SendResult {
	f.mu.Lock()
	f.calls++
	n := f.calls
	f.priorIDs = append(f.priorIDs, priorID)
	_, hasDeadline := ctx.Deadline()
	f.deadlines = append(f.deadlines, hasDeadline)
	f.mu.Unlock()

	id := priorID
	if id == "" {
		id = fmt.Sprintf("3EB0GEN%d", n)
	}
	if beforeDispatch != nil {
		beforeDispatch(id)
	}
	if f.entered != nil {
		f.entered <- struct{}{}
	}
	if f.block != nil {
		<-f.block
	}
	if f.result != nil {
		res := f.result(n)
		if res.MessageID == "" {
			res.MessageID = id
		}
		return res
	}
	return types.SendResult{Success: true, MessageID: id, Timestamp: time.Unix(1_800_000_000, 0)}
}

func (f *fakeSender) SendMessageWithOptions(ctx context.Context, _ *database.MessageStore, o whatsapp.SendOptions) types.SendResult {
	return f.record(ctx, o.MessageID, o.BeforeDispatch)
}

func (f *fakeSender) SendReactionWithOptions(ctx context.Context, _ *database.MessageStore, o whatsapp.ReactionOptions) types.SendResult {
	return f.record(ctx, o.MessageID, o.BeforeDispatch)
}

func (f *fakeSender) CreatePollWithOptions(ctx context.Context, _ *database.MessageStore, o whatsapp.PollOptions) types.SendResult {
	return f.record(ctx, o.MessageID, o.BeforeDispatch)
}

func (f *fakeSender) callCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls
}

func newSendTestServer(t *testing.T, f *fakeSender) *Server {
	t.Helper()
	name := strings.NewReplacer("/", "_", " ", "_").Replace(t.Name())
	store, err := database.OpenMessageStore("file:" + name + "?mode=memory&cache=shared")
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return &Server{sender: f, messageStore: store}
}

func post(t *testing.T, h http.HandlerFunc, body any, header map[string]string) *httptest.ResponseRecorder {
	t.Helper()
	b, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPost, "/api/x", bytes.NewReader(b))
	for k, v := range header {
		req.Header.Set(k, v)
	}
	rec := httptest.NewRecorder()
	h(rec, req)
	return rec
}

func decode(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &m); err != nil {
		t.Fatalf("decode %q: %v", rec.Body.String(), err)
	}
	return m
}

func TestSendWithoutKeyAlwaysSends(t *testing.T) {
	f := &fakeSender{}
	s := newSendTestServer(t, f)
	body := map[string]any{"recipient": "1@s.whatsapp.net", "message": "hi"}
	for i := 0; i < 2; i++ {
		if rec := post(t, s.handleSendMessage, body, nil); rec.Code != http.StatusOK {
			t.Fatalf("status %d: %s", rec.Code, rec.Body)
		}
	}
	if f.callCount() != 2 {
		t.Fatalf("calls = %d, want 2", f.callCount())
	}
	if !f.deadlines[0] {
		t.Fatal("send context should carry the SEND_TIMEOUT deadline")
	}
}

func TestSendReplaysCompletedKey(t *testing.T) {
	tests := []struct {
		name   string
		body   map[string]any
		header map[string]string
	}{
		{"body field", map[string]any{"recipient": "1@s.whatsapp.net", "message": "hi", "idempotency_key": "k-1"}, nil},
		{"header", map[string]any{"recipient": "1@s.whatsapp.net", "message": "hi"}, map[string]string{"Idempotency-Key": "k-1"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			f := &fakeSender{}
			s := newSendTestServer(t, f)

			first := post(t, s.handleSendMessage, tt.body, tt.header)
			if first.Code != http.StatusOK || first.Header().Get(replayHeader) != "" {
				t.Fatalf("first: %d %v %s", first.Code, first.Header(), first.Body)
			}
			second := post(t, s.handleSendMessage, tt.body, tt.header)
			if second.Code != http.StatusOK || second.Header().Get(replayHeader) != "true" {
				t.Fatalf("second: %d %v %s", second.Code, second.Header(), second.Body)
			}
			if f.callCount() != 1 {
				t.Fatalf("WhatsApp send called %d times, want 1", f.callCount())
			}
			a, b := decode(t, first), decode(t, second)
			if a["message_id"] != b["message_id"] || b["idempotent_replay"] != true {
				t.Fatalf("replay body %v does not match original %v", b, a)
			}
		})
	}
}

func TestSendKeyReusedWithDifferentPayload(t *testing.T) {
	f := &fakeSender{}
	s := newSendTestServer(t, f)
	post(t, s.handleSendMessage, map[string]any{"recipient": "1@s.whatsapp.net", "message": "hi", "idempotency_key": "k"}, nil)
	rec := post(t, s.handleSendMessage, map[string]any{"recipient": "1@s.whatsapp.net", "message": "different", "idempotency_key": "k"}, nil)
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status %d, want 422: %s", rec.Code, rec.Body)
	}
	// Same key to another recipient is a different scope, so it sends.
	rec = post(t, s.handleSendMessage, map[string]any{"recipient": "2@s.whatsapp.net", "message": "hi", "idempotency_key": "k"}, nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("other recipient: status %d", rec.Code)
	}
	if f.callCount() != 2 {
		t.Fatalf("calls = %d, want 2", f.callCount())
	}
}

func TestSendInFlightKeyConflicts(t *testing.T) {
	f := &fakeSender{block: make(chan struct{}), entered: make(chan struct{}, 1)}
	s := newSendTestServer(t, f)
	body := map[string]any{"recipient": "1@s.whatsapp.net", "message": "hi", "idempotency_key": "slow"}

	done := make(chan *httptest.ResponseRecorder)
	go func() { done <- post(t, s.handleSendMessage, body, nil) }()
	<-f.entered

	rec := post(t, s.handleSendMessage, body, nil)
	if rec.Code != http.StatusConflict {
		t.Fatalf("status %d, want 409: %s", rec.Code, rec.Body)
	}
	close(f.block)
	if first := <-done; first.Code != http.StatusOK {
		t.Fatalf("first: %d", first.Code)
	}
	if f.callCount() != 1 {
		t.Fatalf("calls = %d, want 1", f.callCount())
	}
}

func TestSendFailedKeyRetriesWithSameMessageID(t *testing.T) {
	f := &fakeSender{result: func(n int) types.SendResult {
		if n == 1 {
			err := &whatsapp.SendError{Kind: whatsapp.SendErrTimeout, Err: context.DeadlineExceeded}
			return types.SendResult{Success: false, Error: err.Error(), Err: err}
		}
		return types.SendResult{Success: true}
	}}
	s := newSendTestServer(t, f)
	body := map[string]any{"recipient": "1@s.whatsapp.net", "message": "hi", "idempotency_key": "retry"}

	if rec := post(t, s.handleSendMessage, body, nil); rec.Code != http.StatusGatewayTimeout {
		t.Fatalf("first: %d, want 504", rec.Code)
	}
	rec := post(t, s.handleSendMessage, body, nil)
	if rec.Code != http.StatusOK || rec.Header().Get(replayHeader) != "" {
		t.Fatalf("retry: %d %v", rec.Code, rec.Header())
	}
	// The retry must reuse the first attempt's WhatsApp ID so recipients
	// de-duplicate if the timed-out attempt actually went out.
	if f.priorIDs[1] != "3EB0GEN1" {
		t.Fatalf("retry used prior id %q, want 3EB0GEN1", f.priorIDs[1])
	}
	if got := decode(t, rec)["message_id"]; got != "3EB0GEN1" {
		t.Fatalf("message_id = %v", got)
	}
}

func TestSendStatusCodes(t *testing.T) {
	until := time.Now().Add(3 * time.Hour)
	tests := []struct {
		name string
		err  error
		want int
	}{
		{"bad input", &whatsapp.SendError{Kind: whatsapp.SendErrInvalid, Err: errors.New("bad jid")}, http.StatusBadRequest},
		{"allowlist", &whatsapp.SendError{Kind: whatsapp.SendErrForbidden, Err: errors.New("blocked")}, http.StatusForbidden},
		{"antiban", antiban.ErrHealthPaused, http.StatusTooManyRequests},
		{"reachout", &whatsapp.SendError{Kind: whatsapp.SendErrReachoutTimelock, Until: until, Err: errors.New("463")}, http.StatusTooManyRequests},
		{"raw 463", fmt.Errorf("%w %d", whatsmeow.ErrServerReturnedError, 463), http.StatusTooManyRequests},
		{"server error", fmt.Errorf("%w %d", whatsmeow.ErrServerReturnedError, 500), http.StatusBadGateway},
		{"not connected", whatsmeow.ErrNotConnected, http.StatusServiceUnavailable},
		{"timeout", whatsmeow.ErrMessageTimedOut, http.StatusGatewayTimeout},
		{"deadline", context.DeadlineExceeded, http.StatusGatewayTimeout},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			f := &fakeSender{result: func(int) types.SendResult {
				return types.SendResult{Success: false, Error: tt.err.Error(), Err: tt.err}
			}}
			s := newSendTestServer(t, f)
			rec := post(t, s.handleSendMessage, map[string]any{"recipient": "1@s.whatsapp.net", "message": "hi"}, nil)
			if rec.Code != tt.want {
				t.Fatalf("status %d, want %d: %s", rec.Code, tt.want, rec.Body)
			}
			body := decode(t, rec)
			if body["success"] != false || body["code"] == "" {
				t.Fatalf("body %v lacks success=false / code", body)
			}
			if tt.name == "reachout" {
				if rec.Header().Get("Retry-After") == "" || body["retry_after"] == nil {
					t.Fatalf("timelock 429 should carry Retry-After and retry_after: %v %v", rec.Header(), body)
				}
				if body["code"] != string(whatsapp.SendErrReachoutTimelock) {
					t.Fatalf("code = %v", body["code"])
				}
			}
		})
	}
}

func TestSendInputErrorsAreNotSent(t *testing.T) {
	f := &fakeSender{}
	s := newSendTestServer(t, f)
	if rec := post(t, s.handleSendMessage, map[string]any{"message": "hi"}, nil); rec.Code != http.StatusBadRequest {
		t.Fatalf("missing recipient: %d", rec.Code)
	}
	long := strings.Repeat("k", maxIdempotencyKeyLen+1)
	if rec := post(t, s.handleSendMessage, map[string]any{"recipient": "1@s.whatsapp.net", "message": "hi", "idempotency_key": long}, nil); rec.Code != http.StatusBadRequest {
		t.Fatalf("overlong key: %d", rec.Code)
	}
	if f.callCount() != 0 {
		t.Fatalf("calls = %d, want 0", f.callCount())
	}
}

func TestReactionAndPollIdempotency(t *testing.T) {
	f := &fakeSender{}
	s := newSendTestServer(t, f)

	reaction := map[string]any{"chat_jid": "1@s.whatsapp.net", "message_id": "M", "emoji": "👍", "idempotency_key": "r"}
	post(t, s.handleReaction, reaction, nil)
	if rec := post(t, s.handleReaction, reaction, nil); rec.Code != http.StatusOK || rec.Header().Get(replayHeader) != "true" {
		t.Fatalf("reaction replay: %d %v", rec.Code, rec.Header())
	}

	poll := map[string]any{"chat_jid": "1@s.whatsapp.net", "question": "Q?", "options": []string{"a", "b"}, "idempotency_key": "p"}
	post(t, s.handleCreatePoll, poll, nil)
	rec := post(t, s.handleCreatePoll, poll, nil)
	if rec.Code != http.StatusOK || rec.Header().Get(replayHeader) != "true" {
		t.Fatalf("poll replay: %d %v", rec.Code, rec.Header())
	}
	if decode(t, rec)["question"] != "Q?" {
		t.Fatalf("poll replay body: %s", rec.Body)
	}
	if f.callCount() != 2 {
		t.Fatalf("calls = %d, want 2 (one reaction, one poll)", f.callCount())
	}
}

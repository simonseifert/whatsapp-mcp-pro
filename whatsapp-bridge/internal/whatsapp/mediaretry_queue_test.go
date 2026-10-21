package whatsapp

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waMmsRetry"
	"google.golang.org/protobuf/proto"

	"whatsapp-bridge/internal/database"
)

func newQueueTestStore(t *testing.T) *database.MessageStore {
	t.Helper()
	name := strings.NewReplacer("/", "_", " ", "_").Replace(t.Name())
	store, err := database.OpenMessageStore("file:" + name + "?mode=memory&cache=shared&_foreign_keys=on")
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store
}

// fakeQueue builds a queue on an in-memory store with a controllable clock,
// connection flag and send function that records what it was asked to send.
type fakeQueue struct {
	*MediaRetryQueue
	clock     time.Time
	online    bool
	sendErr   error
	sentOrder []string
}

func newFakeQueue(t *testing.T, cfg MediaRetryConfig) *fakeQueue {
	f := &fakeQueue{clock: time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC), online: true}
	f.MediaRetryQueue = newMediaRetryQueue(newQueueTestStore(t), cfg, nil,
		func() bool { return f.online },
		func(_ context.Context, req database.MediaRequest) error {
			if f.sendErr != nil {
				return f.sendErr
			}
			f.sentOrder = append(f.sentOrder, req.MessageID)
			return nil
		})
	f.now = func() time.Time { return f.clock }
	return f
}

func (f *fakeQueue) enqueue(t *testing.T, ids ...string) {
	t.Helper()
	for _, id := range ids {
		if _, err := f.store.EnqueueMediaRequest(id, "123@s.whatsapp.net", f.clock); err != nil {
			t.Fatal(err)
		}
		f.clock = f.clock.Add(time.Second) // distinct created_at keeps FIFO order deterministic
	}
}

func (f *fakeQueue) tick(t *testing.T) int {
	t.Helper()
	n, err := f.Tick(context.Background())
	if err != nil {
		t.Fatalf("tick: %v", err)
	}
	return n
}

func (f *fakeQueue) state(t *testing.T, id string) database.MediaRequest {
	t.Helper()
	r, err := f.store.GetMediaRequest(id, "123@s.whatsapp.net")
	if err != nil || r == nil {
		t.Fatalf("get %s: %v %v", id, r, err)
	}
	return *r
}

func ids(n int) []string {
	out := make([]string, n)
	for i := range out {
		out[i] = fmt.Sprintf("MSG%02d", i)
	}
	return out
}

func TestMediaRetryBudget(t *testing.T) {
	tests := []struct {
		name                       string
		sent, maxPerHour, batch, w int
	}{
		{"fresh hour, batch caps", 0, 20, 3, 3},
		{"near the cap", 18, 20, 3, 2},
		{"at the cap", 20, 20, 3, 0},
		{"over the cap (sync sends)", 25, 20, 3, 0},
		{"no batch limit", 5, 20, 0, 15},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := mediaRetryBudget(tt.sent, tt.maxPerHour, tt.batch); got != tt.w {
				t.Errorf("budget = %d, want %d", got, tt.w)
			}
		})
	}
}

func TestTickPacing(t *testing.T) {
	tests := []struct {
		name       string
		cfg        MediaRetryConfig
		pending    int
		ticks      int
		tickEvery  time.Duration
		wantPerTic []int
	}{
		{
			name:       "batch size limits each tick",
			cfg:        MediaRetryConfig{MaxPerHour: 20, MaxAttempts: 3, BatchSize: 3, AnswerTimeout: 2 * time.Hour},
			pending:    7,
			ticks:      4,
			tickEvery:  time.Minute,
			wantPerTic: []int{3, 3, 1, 0},
		},
		{
			name:       "hourly cap stops sends until the window rolls",
			cfg:        MediaRetryConfig{MaxPerHour: 4, MaxAttempts: 3, BatchSize: 3, AnswerTimeout: 2 * time.Hour},
			pending:    10,
			ticks:      4,
			tickEvery:  21 * time.Minute,
			wantPerTic: []int{3, 1, 0, 3}, // the 4th tick is 63 min in: the first batch left the window
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			f := newFakeQueue(t, tt.cfg)
			f.enqueue(t, ids(tt.pending)...)
			for i := 0; i < tt.ticks; i++ {
				if got := f.tick(t); got != tt.wantPerTic[i] {
					t.Fatalf("tick %d sent %d, want %d", i, got, tt.wantPerTic[i])
				}
				f.clock = f.clock.Add(tt.tickEvery)
			}
		})
	}
}

func TestTickSendsOldestFirst(t *testing.T) {
	f := newFakeQueue(t, MediaRetryConfig{MaxPerHour: 20, MaxAttempts: 3, BatchSize: 2, AnswerTimeout: time.Hour})
	f.enqueue(t, "A", "B", "C")
	f.tick(t)
	if strings.Join(f.sentOrder, ",") != "A,B" {
		t.Errorf("sent %v, want A,B", f.sentOrder)
	}
}

func TestTickCountsSynchronousSendsAgainstBudget(t *testing.T) {
	f := newFakeQueue(t, MediaRetryConfig{MaxPerHour: 3, MaxAttempts: 3, BatchSize: 3, AnswerTimeout: time.Hour})
	// Two receipts sent by /api/download in the last hour.
	for _, id := range []string{"SYNC1", "SYNC2"} {
		if err := f.store.MarkMediaRequestSent(id, "123@s.whatsapp.net", f.clock.Add(-10*time.Minute)); err != nil {
			t.Fatal(err)
		}
	}
	f.enqueue(t, "Q1", "Q2")
	if got := f.tick(t); got != 1 {
		t.Errorf("sent %d, want 1 (3/hour minus 2 synchronous)", got)
	}
}

func TestTickSkipsWhileDisconnected(t *testing.T) {
	f := newFakeQueue(t, DefaultMediaRetryConfig())
	f.enqueue(t, "A")
	f.online = false
	if got := f.tick(t); got != 0 {
		t.Fatalf("sent %d while offline", got)
	}
	if r := f.state(t, "A"); r.State != database.MediaRequestPending || r.Attempts != 0 {
		t.Errorf("offline tick touched the request: %+v", r)
	}
	f.online = true
	if got := f.tick(t); got != 1 {
		t.Errorf("sent %d after reconnect, want 1", got)
	}
}

func TestTickLifecycle(t *testing.T) {
	cfg := MediaRetryConfig{MaxPerHour: 20, MaxAttempts: 3, BatchSize: 3, AnswerTimeout: time.Hour}
	tests := []struct {
		name      string
		sendErr   error
		ticks     int
		wantState database.MediaRequestState
		wantTries int
	}{
		{"one tick sends", nil, 1, database.MediaRequestSent, 1},
		{"unanswered receipts are re-sent after the timeout", nil, 2, database.MediaRequestSent, 2},
		{"never answered fails after max attempts", nil, 4, database.MediaRequestFailed, 3},
		{"send errors fail after max attempts", errors.New("boom"), 3, database.MediaRequestFailed, 3},
		{"one send error stays pending", errors.New("boom"), 1, database.MediaRequestPending, 1},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			f := newFakeQueue(t, cfg)
			f.sendErr = tt.sendErr
			f.enqueue(t, "A")
			for i := 0; i < tt.ticks; i++ {
				f.tick(t)
				f.clock = f.clock.Add(61 * time.Minute)
			}
			if r := f.state(t, "A"); r.State != tt.wantState || r.Attempts != tt.wantTries {
				t.Errorf("got %s/%d, want %s/%d (last error %q)", r.State, r.Attempts, tt.wantState, tt.wantTries, r.LastError)
			}
		})
	}
}

func TestClassifyMediaRetryAnswer(t *testing.T) {
	notif := func(r waMmsRetry.MediaRetryNotification_ResultType, path string) *waMmsRetry.MediaRetryNotification {
		return &waMmsRetry.MediaRetryNotification{Result: r.Enum(), DirectPath: proto.String(path)}
	}
	tests := []struct {
		name  string
		notif *waMmsRetry.MediaRetryNotification
		err   error
		want  mediaRetryOutcome
	}{
		{"success", notif(waMmsRetry.MediaRetryNotification_SUCCESS, "/v/t62/new"), nil, mediaRetrySuccess},
		{"success without path", notif(waMmsRetry.MediaRetryNotification_SUCCESS, ""), nil, mediaRetryAgain},
		{"not found is final", notif(waMmsRetry.MediaRetryNotification_NOT_FOUND, ""), nil, mediaRetryFailed},
		{"general error retries", notif(waMmsRetry.MediaRetryNotification_GENERAL_ERROR, ""), nil, mediaRetryAgain},
		{"decryption error retries", notif(waMmsRetry.MediaRetryNotification_DECRYPTION_ERROR, ""), nil, mediaRetryAgain},
		{"not on phone is final", nil, fmt.Errorf("x: %w", whatsmeow.ErrMediaNotAvailableOnPhone), mediaRetryFailed},
		{"unknown error retries", nil, whatsmeow.ErrUnknownMediaRetryError, mediaRetryAgain},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got, reason := classifyMediaRetryAnswer(tt.notif, tt.err); got != tt.want {
				t.Errorf("outcome = %d (%s), want %d", got, reason, tt.want)
			}
		})
	}
}

func TestMediaRetryConfigFromEnv(t *testing.T) {
	tests := []struct {
		name, perHour, attempts string
		wantPerHour, wantTries  int
	}{
		{"defaults", "", "", 20, 3},
		{"overrides", "5", "2", 5, 2},
		{"garbage keeps defaults", "lots", "-1", 20, 3},
		{"zero keeps the cap", "0", "0", 20, 3},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv("MEDIA_RETRY_MAX_PER_HOUR", tt.perHour)
			t.Setenv("MEDIA_RETRY_MAX_ATTEMPTS", tt.attempts)
			cfg := MediaRetryConfigFromEnv()
			if cfg.MaxPerHour != tt.wantPerHour || cfg.MaxAttempts != tt.wantTries {
				t.Errorf("got %d/%d, want %d/%d", cfg.MaxPerHour, cfg.MaxAttempts, tt.wantPerHour, tt.wantTries)
			}
		})
	}
}

func TestQueueLoopStopsOnCancel(t *testing.T) {
	f := newFakeQueue(t, MediaRetryConfig{MaxPerHour: 20, MaxAttempts: 3, BatchSize: 3, Interval: time.Millisecond, AnswerTimeout: time.Hour})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		f.run(ctx)
		close(done)
	}()
	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("loop did not stop after cancel")
	}
}

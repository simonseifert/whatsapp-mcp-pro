package whatsapp

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waMmsRetry"
	waLog "go.mau.fi/whatsmeow/util/log"
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

func TestFinalReasonsMatchWhatClassifyRecords(t *testing.T) {
	notFound := &waMmsRetry.MediaRetryNotification{Result: waMmsRetry.MediaRetryNotification_NOT_FOUND.Enum()}
	retryable := &waMmsRetry.MediaRetryNotification{Result: waMmsRetry.MediaRetryNotification_GENERAL_ERROR.Enum()}
	tests := []struct {
		name  string
		notif *waMmsRetry.MediaRetryNotification
		err   error
		want  bool
	}{
		{"not found", notFound, nil, true},
		{"not on phone", nil, whatsmeow.ErrMediaNotAvailableOnPhone, true},
		{"not on phone, wrapped", nil, fmt.Errorf("x: %w", whatsmeow.ErrMediaNotAvailableOnPhone), true},
		{"general error", retryable, nil, false},
		{"unknown error", nil, whatsmeow.ErrUnknownMediaRetryError, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, reason := classifyMediaRetryAnswer(tt.notif, tt.err)
			if got := isFinalMediaRetryReason(reason); got != tt.want {
				t.Errorf("isFinalMediaRetryReason(%q) = %v, want %v", reason, got, tt.want)
			}
		})
	}
	if isFinalMediaRetryReason("no answer from the phone") {
		t.Error("running out of attempts must not count as the phone saying the file is gone")
	}
}

func TestAskPhoneForMedia(t *testing.T) {
	const chat = "123@s.whatsapp.net"
	cfg := MediaRetryConfig{MaxPerHour: 2, MaxAttempts: 3}
	sendBoom := errors.New("boom")

	// Each setup leaves message "A" as a download would find it after
	// EnqueueMediaRequest; sentOthers fills the budget with other messages.
	enqueue := func(t *testing.T, s *database.MessageStore) {
		t.Helper()
		if _, err := s.EnqueueMediaRequest("A", chat, time.Now()); err != nil {
			t.Fatal(err)
		}
	}
	sendA := func(t *testing.T, s *database.MessageStore) {
		t.Helper()
		enqueue(t, s)
		if err := s.MarkMediaRequestSent("A", chat, time.Now().Add(-2*time.Hour)); err != nil {
			t.Fatal(err)
		}
	}
	failA := func(reason string) func(*testing.T, *database.MessageStore) {
		return func(t *testing.T, s *database.MessageStore) {
			t.Helper()
			sendA(t, s)
			if err := s.MarkMediaRequestFailed("A", chat, reason, time.Now()); err != nil {
				t.Fatal(err)
			}
		}
	}

	tests := []struct {
		name       string
		setup      func(*testing.T, *database.MessageStore)
		sentOthers int
		sendErr    error
		wantErr    error
		wantSends  int
		wantState  database.MediaRequestState
		wantTries  int
	}{
		{"budget left sends", enqueue, 1, nil, nil, 1, database.MediaRequestSent, 1},
		{"over budget stays queued", enqueue, 2, nil, errMediaRetryOverBudget, 0, database.MediaRequestPending, 0},
		{"NOT_FOUND is not asked again", failA(mediaRetryReasonNotFound), 0, nil, errMediaGoneFromPhone, 0, database.MediaRequestFailed, 1},
		{"not on phone is not asked again", failA(whatsmeow.ErrMediaNotAvailableOnPhone.Error()), 0, nil, errMediaGoneFromPhone, 0, database.MediaRequestFailed, 1},
		{"attempts run out may ask again", failA("no answer from the phone"), 0, nil, nil, 1, database.MediaRequestSent, 2},
		{"attempts run out still needs budget", failA("no answer from the phone"), 2, nil, errMediaRetryOverBudget, 0, database.MediaRequestFailed, 1},
		{"unanswered receipt is not repeated", sendA, 0, nil, errMediaRetryInFlight, 0, database.MediaRequestSent, 1},
		{"send error stays pending", enqueue, 0, sendBoom, sendBoom, 1, database.MediaRequestPending, 1},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			store := newQueueTestStore(t)
			tt.setup(t, store)
			for i := 0; i < tt.sentOthers; i++ {
				if err := store.MarkMediaRequestSent(fmt.Sprintf("OTHER%d", i), chat, time.Now()); err != nil {
					t.Fatal(err)
				}
			}
			sends := 0
			err := (&Client{logger: waLog.Noop}).askPhoneForMedia(context.Background(), store, "A", chat, cfg, func(context.Context) error {
				sends++
				return tt.sendErr
			})
			if !errors.Is(err, tt.wantErr) || (tt.wantErr == nil && err != nil) {
				t.Errorf("err = %v, want %v", err, tt.wantErr)
			}
			if sends != tt.wantSends {
				t.Errorf("sent %d receipts, want %d", sends, tt.wantSends)
			}
			r, err := store.GetMediaRequest("A", chat)
			if err != nil || r == nil {
				t.Fatalf("get: %v %v", r, err)
			}
			if r.State != tt.wantState || r.Attempts != tt.wantTries {
				t.Errorf("got %s/%d, want %s/%d", r.State, r.Attempts, tt.wantState, tt.wantTries)
			}
		})
	}
}

// The bug this guards against: a bulk job of hundreds of purged files all got a
// receipt. Sequential and concurrent callers alike must stop at the cap, and
// the rest must be left for the paced queue.
func TestAskPhoneForMediaHoldsTheCapUnderLoad(t *testing.T) {
	const chat = "123@s.whatsapp.net"
	store := newQueueTestStore(t)
	cfg := MediaRetryConfig{MaxPerHour: 3, MaxAttempts: 3}
	var sends atomic.Int32
	for _, id := range ids(12) {
		if _, err := store.EnqueueMediaRequest(id, chat, time.Now()); err != nil {
			t.Fatal(err)
		}
	}
	var wg sync.WaitGroup
	for _, id := range ids(12) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_ = (&Client{logger: waLog.Noop}).askPhoneForMedia(context.Background(), store, id, chat, cfg, func(context.Context) error {
				sends.Add(1)
				return nil
			})
		}()
	}
	wg.Wait()
	if got := sends.Load(); got != 3 {
		t.Errorf("sent %d receipts, want the cap of 3", got)
	}
	pending, err := store.ListMediaRequests(database.MediaRequestPending, 100)
	if err != nil || len(pending) != 9 {
		t.Errorf("%d left pending (%v), want 9", len(pending), err)
	}
}

func TestTickWaitsBeforeAskingAgain(t *testing.T) {
	f := newFakeQueue(t, MediaRetryConfig{MaxPerHour: 20, MaxAttempts: 3, BatchSize: 3, AnswerTimeout: time.Hour, RetryDelay: 6 * time.Hour})
	f.enqueue(t, "A")
	if got := f.tick(t); got != 1 {
		t.Fatalf("first ask sent %d, want 1", got)
	}
	// The phone answers with a retryable error straight away.
	if err := f.store.MarkMediaRequestRetry("A", "123@s.whatsapp.net", "phone answered GENERAL_ERROR", 3, f.clock); err != nil {
		t.Fatal(err)
	}
	f.clock = f.clock.Add(time.Minute)
	if got := f.tick(t); got != 0 {
		t.Errorf("a minute later sent %d, want 0", got)
	}
	f.clock = f.clock.Add(6 * time.Hour)
	if got := f.tick(t); got != 1 {
		t.Errorf("after the delay sent %d, want 1", got)
	}
}

// Bursts of retries at one file can't get past the cap by reusing its row.
func TestTickCountsEveryAskOfOneFile(t *testing.T) {
	f := newFakeQueue(t, MediaRetryConfig{MaxPerHour: 2, MaxAttempts: 9, BatchSize: 3, AnswerTimeout: time.Hour})
	f.enqueue(t, "A", "B")
	for range 2 {
		f.clock = f.clock.Add(time.Minute)
		if err := f.store.MarkMediaRequestSent("A", "123@s.whatsapp.net", f.clock); err != nil {
			t.Fatal(err)
		}
		if err := f.store.MarkMediaRequestRetry("A", "123@s.whatsapp.net", "phone answered GENERAL_ERROR", 9, f.clock); err != nil {
			t.Fatal(err)
		}
	}
	f.clock = f.clock.Add(time.Minute)
	if got := f.tick(t); got != 0 {
		t.Errorf("sent %d with the cap already used by one file, want 0", got)
	}
}

func TestTickSendsNothingInQuietHours(t *testing.T) {
	cfg := MediaRetryConfig{MaxPerHour: 20, MaxAttempts: 3, BatchSize: 3, AnswerTimeout: time.Hour, QuietStartHour: 23, QuietEndHour: 8}
	f := newFakeQueue(t, cfg)
	f.enqueue(t, "A")
	tests := []struct {
		hour, want int
	}{{23, 0}, {3, 0}, {7, 0}, {8, 1}}
	for _, tt := range tests {
		f.clock = time.Date(2026, 10, 22, tt.hour, 30, 0, 0, time.UTC)
		if got := f.tick(t); got != tt.want {
			t.Errorf("%02d:30 sent %d, want %d", tt.hour, got, tt.want)
		}
	}
}

func TestInQuietHours(t *testing.T) {
	tests := []struct {
		name             string
		start, end, hour int
		want             bool
	}{
		{"wrap, late", 23, 8, 23, true},
		{"wrap, early", 23, 8, 7, true},
		{"wrap, end is exclusive", 23, 8, 8, false},
		{"wrap, daytime", 23, 8, 12, false},
		{"same day, inside", 1, 5, 3, true},
		{"same day, after", 1, 5, 5, false},
		{"equal means off", 0, 0, 0, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := MediaRetryConfig{QuietStartHour: tt.start, QuietEndHour: tt.end}
			if got := cfg.inQuietHours(tt.hour); got != tt.want {
				t.Errorf("got %v, want %v", got, tt.want)
			}
		})
	}
}

func TestQuietHoursFromEnv(t *testing.T) {
	tests := []struct {
		name, env  string
		start, end int
	}{
		{"default", "", 23, 8},
		{"custom", "22-7", 22, 7},
		{"spaces", " 1 - 5 ", 1, 5},
		{"off", "off", 0, 0},
		{"out of range keeps default", "23-25", 23, 8},
		{"garbage keeps default", "night", 23, 8},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv("MEDIA_RETRY_QUIET_HOURS", tt.env)
			cfg := MediaRetryConfigFromEnv()
			if cfg.QuietStartHour != tt.start || cfg.QuietEndHour != tt.end {
				t.Errorf("got %d-%d, want %d-%d", cfg.QuietStartHour, cfg.QuietEndHour, tt.start, tt.end)
			}
		})
	}
}

func TestMediaRetryConfigFromEnv(t *testing.T) {
	tests := []struct {
		name, perHour, attempts string
		wantPerHour, wantTries  int
	}{
		{"defaults", "", "", 5, 3},
		{"overrides", "5", "2", 5, 2},
		{"garbage keeps defaults", "lots", "-1", 5, 3},
		{"zero keeps the cap", "0", "0", 5, 3},
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

package database

import (
	"testing"
	"time"
)

const (
	mrMsg  = "3EB0MEDIA"
	mrChat = "123@s.whatsapp.net"
)

var mrT0 = time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)

func mustMediaRequest(t *testing.T, store *MessageStore) MediaRequest {
	t.Helper()
	r, err := store.GetMediaRequest(mrMsg, mrChat)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if r == nil {
		t.Fatal("request missing")
	}
	return *r
}

func TestMediaRequestStateMachine(t *testing.T) {
	const maxAttempts = 3
	type step func(t *testing.T, s *MessageStore)
	enqueue := func(t *testing.T, s *MessageStore) {
		if _, err := s.EnqueueMediaRequest(mrMsg, mrChat, mrT0); err != nil {
			t.Fatal(err)
		}
	}
	sent := func(t *testing.T, s *MessageStore) {
		if err := s.MarkMediaRequestSent(mrMsg, mrChat, mrT0); err != nil {
			t.Fatal(err)
		}
	}
	retry := func(t *testing.T, s *MessageStore) {
		if err := s.MarkMediaRequestRetry(mrMsg, mrChat, "GENERAL_ERROR", maxAttempts, mrT0); err != nil {
			t.Fatal(err)
		}
	}
	notFound := func(t *testing.T, s *MessageStore) {
		if err := s.MarkMediaRequestFailed(mrMsg, mrChat, "NOT_FOUND", mrT0); err != nil {
			t.Fatal(err)
		}
	}
	succeed := func(t *testing.T, s *MessageStore) {
		if err := s.MarkMediaRequestSucceeded(mrMsg, mrChat, mrT0); err != nil {
			t.Fatal(err)
		}
	}
	sendErr := func(t *testing.T, s *MessageStore) {
		if err := s.MarkMediaRequestSendError(mrMsg, mrChat, "not connected", maxAttempts, mrT0); err != nil {
			t.Fatal(err)
		}
	}
	expire := func(t *testing.T, s *MessageStore) {
		if _, err := s.ExpireSentMediaRequests(mrT0.Add(time.Hour), maxAttempts, mrT0.Add(time.Hour)); err != nil {
			t.Fatal(err)
		}
	}

	tests := []struct {
		name      string
		steps     []step
		wantState MediaRequestState
		wantTries int
		wantErr   string
	}{
		{"enqueue starts pending", []step{enqueue}, MediaRequestPending, 0, ""},
		{"enqueue is idempotent", []step{enqueue, enqueue}, MediaRequestPending, 0, ""},
		{"send moves to sent", []step{enqueue, sent}, MediaRequestSent, 1, ""},
		{"send without enqueue creates the row", []step{sent}, MediaRequestSent, 1, ""},
		{"enqueue leaves a sent request alone", []step{enqueue, sent, enqueue}, MediaRequestSent, 1, ""},
		{"success", []step{enqueue, sent, succeed}, MediaRequestSucceeded, 1, ""},
		{"NOT_FOUND fails at once", []step{enqueue, sent, notFound}, MediaRequestFailed, 1, "NOT_FOUND"},
		{"retryable answer goes back to pending", []step{enqueue, sent, retry}, MediaRequestPending, 1, "GENERAL_ERROR"},
		{"retryable answer on last attempt fails", []step{enqueue, sent, retry, sent, retry, sent, retry}, MediaRequestFailed, 3, "GENERAL_ERROR"},
		{"unanswered send expires to pending", []step{enqueue, sent, expire}, MediaRequestPending, 1, "no answer from the phone"},
		{"unanswered last attempt expires to failed", []step{enqueue, sent, expire, sent, expire, sent, expire}, MediaRequestFailed, 3, "no answer from the phone"},
		{"send error stays pending", []step{enqueue, sendErr}, MediaRequestPending, 1, "not connected"},
		{"send errors exhaust attempts", []step{enqueue, sendErr, sendErr, sendErr}, MediaRequestFailed, 3, "not connected"},
		{"failed stays failed on enqueue", []step{enqueue, sent, notFound, enqueue}, MediaRequestFailed, 1, "NOT_FOUND"},
		{"succeeded reopens on enqueue", []step{enqueue, sent, succeed, enqueue}, MediaRequestPending, 0, ""},
		{"late answer after failure is ignored by retry", []step{enqueue, sent, notFound, retry}, MediaRequestFailed, 1, "NOT_FOUND"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			store := newTestMessageStore(t)
			for _, s := range tt.steps {
				s(t, store)
			}
			got := mustMediaRequest(t, store)
			if got.State != tt.wantState || got.Attempts != tt.wantTries || got.LastError != tt.wantErr {
				t.Errorf("got state=%s attempts=%d err=%q, want %s/%d/%q",
					got.State, got.Attempts, got.LastError, tt.wantState, tt.wantTries, tt.wantErr)
			}
		})
	}
}

func TestEnqueueMediaRequestReportsChange(t *testing.T) {
	store := newTestMessageStore(t)
	for i, want := range []bool{true, false} {
		got, err := store.EnqueueMediaRequest(mrMsg, mrChat, mrT0)
		if err != nil {
			t.Fatal(err)
		}
		if got != want {
			t.Errorf("enqueue #%d changed=%v, want %v", i+1, got, want)
		}
	}
}

func TestExpireOnlyTouchesStaleSentRequests(t *testing.T) {
	store := newTestMessageStore(t)
	if err := store.MarkMediaRequestSent("old", mrChat, mrT0); err != nil {
		t.Fatal(err)
	}
	if err := store.MarkMediaRequestSent("fresh", mrChat, mrT0.Add(50*time.Minute)); err != nil {
		t.Fatal(err)
	}
	n, err := store.ExpireSentMediaRequests(mrT0.Add(30*time.Minute), 3, mrT0.Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("expired %d, want 1", n)
	}
	fresh, _ := store.GetMediaRequest("fresh", mrChat)
	if fresh.State != MediaRequestSent {
		t.Errorf("fresh request state = %s, want sent", fresh.State)
	}
}

func TestCountMediaRequestsSentSince(t *testing.T) {
	store := newTestMessageStore(t)
	for i, at := range []time.Duration{-90 * time.Minute, -59 * time.Minute, -10 * time.Minute, 0} {
		if err := store.MarkMediaRequestSent(string(rune('a'+i)), mrChat, mrT0.Add(at)); err != nil {
			t.Fatal(err)
		}
	}
	// Enqueued but never sent: must not count against the budget.
	if _, err := store.EnqueueMediaRequest("never", mrChat, mrT0); err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		since time.Duration
		want  int
	}{
		{-2 * time.Hour, 4},
		{-time.Hour, 3},
		{-5 * time.Minute, 1},
		{time.Minute, 0},
	}
	for _, tt := range tests {
		got, err := store.CountMediaRequestsSentSince(mrT0.Add(tt.since))
		if err != nil {
			t.Fatal(err)
		}
		if got != tt.want {
			t.Errorf("since %v: got %d, want %d", tt.since, got, tt.want)
		}
	}
}

func TestDueAndOpenMediaRequests(t *testing.T) {
	store := newTestMessageStore(t)
	for i, id := range []string{"second", "first", "third"} {
		at := mrT0.Add(time.Duration([]int{2, 1, 3}[i]) * time.Minute)
		if _, err := store.EnqueueMediaRequest(id, mrChat, at); err != nil {
			t.Fatal(err)
		}
	}
	if err := store.MarkMediaRequestSent("third", mrChat, mrT0); err != nil {
		t.Fatal(err)
	}

	due, err := store.DueMediaRequests(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(due) != 2 || due[0].MessageID != "first" || due[1].MessageID != "second" {
		t.Fatalf("due = %+v, want first, second", due)
	}
	if due, _ := store.DueMediaRequests(1); len(due) != 1 {
		t.Errorf("limit ignored: %d rows", len(due))
	}

	open, err := store.OpenMediaRequestsForMessage("third")
	if err != nil || len(open) != 1 || open[0].State != MediaRequestSent {
		t.Fatalf("open = %+v, %v", open, err)
	}
	if err := store.MarkMediaRequestSucceeded("third", mrChat, mrT0); err != nil {
		t.Fatal(err)
	}
	if open, _ := store.OpenMediaRequestsForMessage("third"); len(open) != 0 {
		t.Errorf("succeeded request still open: %+v", open)
	}

	all, err := store.ListMediaRequests("", 10)
	if err != nil || len(all) != 3 {
		t.Fatalf("list all = %d, %v", len(all), err)
	}
	pending, err := store.ListMediaRequests(MediaRequestPending, 10)
	if err != nil || len(pending) != 2 {
		t.Fatalf("list pending = %d, %v", len(pending), err)
	}
}

func TestMediaRequestMigrationIsIdempotent(t *testing.T) {
	store := newTestMessageStore(t)
	if _, err := store.EnqueueMediaRequest(mrMsg, mrChat, mrT0); err != nil {
		t.Fatal(err)
	}
	if err := runMigrations(store.db); err != nil {
		t.Fatalf("second runMigrations: %v", err)
	}
	mustMediaRequest(t, store)
}

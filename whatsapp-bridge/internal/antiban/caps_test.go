package antiban

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

const (
	chatA = "4915112345678@s.whatsapp.net"
	chatB = "123456789012345@lid"
)

// capsConfig is every cap off except what the case turns on, so each case
// tests exactly one ceiling.
func capsConfig(t *testing.T, mutate func(*Config)) *Config {
	t.Helper()
	cfg := &Config{
		CapsEnabled:   true,
		CapsStatePath: filepath.Join(t.TempDir(), "caps.json"),
	}
	if mutate != nil {
		mutate(cfg)
	}
	return cfg
}

type fakeClock struct{ t time.Time }

func (c *fakeClock) now() time.Time { return c.t }

func newTestCaps(cfg *Config, clock *fakeClock) *SendCaps {
	c := NewSendCaps(cfg)
	c.now = clock.now
	return c
}

func TestSendCaps(t *testing.T) {
	text := func(chat string) SendRequest { return SendRequest{Type: Text, Recipient: chat} }
	first := func(chat string) SendRequest { return SendRequest{Type: Text, Recipient: chat, NewRecipient: true} }
	react := func(chat string) SendRequest { return SendRequest{Type: Reaction, Recipient: chat} }

	type step struct {
		at      time.Duration // offset from the start
		req     SendRequest
		wantCap string // "" = allowed
		// For refusals, the retry hint must fall in [minRetry, maxRetry].
		minRetry, maxRetry time.Duration
	}
	tests := []struct {
		name  string
		cfg   func(*Config)
		steps []step
	}{
		{
			name: "per-chat minute cap, then frees up",
			cfg:  func(c *Config) { c.RecipientMaxPerMinute = 3 },
			steps: []step{
				{at: 0, req: text(chatA)},
				{at: time.Second, req: text(chatA)},
				{at: 2 * time.Second, req: text(chatA)},
				{at: 10 * time.Second, req: text(chatA), wantCap: CapRecipientPerMinute, minRetry: 49 * time.Second, maxRetry: 51 * time.Second},
				{at: 10 * time.Second, req: text(chatB)}, // another chat is unaffected
				{at: 61 * time.Second, req: text(chatA)},
			},
		},
		{
			name: "poll and media share the message budget",
			cfg:  func(c *Config) { c.RecipientMaxPerMinute = 2 },
			steps: []step{
				{at: 0, req: text(chatA)},
				{at: 0, req: SendRequest{Type: Poll, Recipient: chatA}},
				{at: 0, req: text(chatA), wantCap: CapRecipientPerMinute, minRetry: time.Minute, maxRetry: time.Minute},
			},
		},
		{
			name: "per-chat hour cap",
			cfg:  func(c *Config) { c.RecipientMaxPerMinute = 100; c.RecipientMaxPerHour = 3 },
			steps: []step{
				{at: 0, req: text(chatA)},
				{at: 10 * time.Minute, req: text(chatA)},
				{at: 20 * time.Minute, req: text(chatA)},
				{at: 30 * time.Minute, req: text(chatA), wantCap: CapRecipientPerHour, minRetry: 30 * time.Minute, maxRetry: 30 * time.Minute},
				{at: 61 * time.Minute, req: text(chatA)},
			},
		},
		{
			name: "global hour cap spans chats",
			cfg:  func(c *Config) { c.GlobalMaxPerHour = 2 },
			steps: []step{
				{at: 0, req: text(chatA)},
				{at: time.Minute, req: text(chatB)},
				{at: 2 * time.Minute, req: text("4915112345679@s.whatsapp.net"), wantCap: CapGlobalPerHour, minRetry: 58 * time.Minute, maxRetry: 58 * time.Minute},
			},
		},
		{
			name: "new recipients per day; replies are not first contacts",
			cfg:  func(c *Config) { c.NewRecipientsPerDay = 2 },
			steps: []step{
				{at: 0, req: first(chatA)},
				{at: time.Hour, req: first(chatB)},
				{at: 2 * time.Hour, req: first("4915112345679@s.whatsapp.net"), wantCap: CapNewRecipientsDay, minRetry: 22 * time.Hour, maxRetry: 22 * time.Hour},
				{at: 2 * time.Hour, req: text(chatA)},
				{at: 24*time.Hour + time.Second, req: first("4915112345679@s.whatsapp.net")},
			},
		},
		{
			name: "reactions have their own budget and leave messages alone",
			cfg:  func(c *Config) { c.RecipientMaxPerMinute = 1; c.FeedbackMaxPerMinute = 2 },
			steps: []step{
				{at: 0, req: react(chatA)},
				{at: 0, req: SendRequest{Type: Edit, Recipient: chatA}},
				{at: 0, req: SendRequest{Type: Delete, Recipient: chatA}, wantCap: CapFeedbackPerMinute, minRetry: time.Minute, maxRetry: time.Minute},
				{at: 0, req: text(chatA)},
			},
		},
		{
			name: "feedback hour cap spans chats",
			cfg:  func(c *Config) { c.FeedbackMaxPerHour = 1 },
			steps: []step{
				{at: 0, req: react(chatA)},
				{at: time.Minute, req: react(chatB), wantCap: CapFeedbackPerHour, minRetry: 59 * time.Minute, maxRetry: 59 * time.Minute},
			},
		},
		{
			name: "history requests to the own phone are exempt",
			cfg: func(c *Config) {
				c.RecipientMaxPerMinute, c.GlobalMaxPerHour, c.FeedbackMaxPerHour = 1, 1, 1
			},
			steps: []step{
				{at: 0, req: SendRequest{Type: Peer}},
				{at: 0, req: SendRequest{Type: Peer}},
				{at: 0, req: text(chatA)},
			},
		},
		{
			name: "zero disables a cap",
			cfg:  func(c *Config) { c.RecipientMaxPerMinute = 0; c.NewRecipientsPerDay = 0 },
			steps: []step{
				{at: 0, req: first(chatA)},
				{at: 0, req: first(chatA)},
				{at: 0, req: first(chatA)},
			},
		},
		{
			name: "caps switched off",
			cfg:  func(c *Config) { c.CapsEnabled = false; c.RecipientMaxPerMinute = 1 },
			steps: []step{
				{at: 0, req: text(chatA)},
				{at: 0, req: text(chatA)},
			},
		},
		{
			name: "most distant retry wins when several caps are full",
			cfg:  func(c *Config) { c.RecipientMaxPerMinute = 1; c.RecipientMaxPerHour = 1 },
			steps: []step{
				{at: 0, req: text(chatA)},
				{at: 0, req: text(chatA), wantCap: CapRecipientPerHour, minRetry: time.Hour, maxRetry: time.Hour},
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			start := time.Date(2026, 10, 23, 9, 0, 0, 0, time.UTC)
			clock := &fakeClock{t: start}
			caps := newTestCaps(capsConfig(t, tt.cfg), clock)
			for i, st := range tt.steps {
				clock.t = start.Add(st.at)
				checkErr := caps.Check(st.req)
				err := caps.Reserve(st.req)
				if (checkErr == nil) != (err == nil) {
					t.Fatalf("step %d: Check=%v but Reserve=%v", i, checkErr, err)
				}
				if st.wantCap == "" {
					if err != nil {
						t.Fatalf("step %d: unexpected refusal: %v", i, err)
					}
					continue
				}
				var ce *CapError
				if !errors.As(err, &ce) || !errors.Is(err, ErrSendCapReached) {
					t.Fatalf("step %d: err = %v, want *CapError", i, err)
				}
				if ce.Cap != st.wantCap {
					t.Fatalf("step %d: cap = %s, want %s", i, ce.Cap, st.wantCap)
				}
				if ce.RetryAfter < st.minRetry || ce.RetryAfter > st.maxRetry {
					t.Fatalf("step %d: retry after %s, want %s..%s", i, ce.RetryAfter, st.minRetry, st.maxRetry)
				}
			}
		})
	}
}

func TestSendCapsCheckDoesNotRecord(t *testing.T) {
	clock := &fakeClock{t: time.Now()}
	caps := newTestCaps(capsConfig(t, func(c *Config) { c.RecipientMaxPerMinute = 1 }), clock)
	req := SendRequest{Type: Text, Recipient: chatA}
	for range 3 {
		if err := caps.Check(req); err != nil {
			t.Fatalf("Check alone must not use up the budget: %v", err)
		}
	}
	if err := caps.Reserve(req); err != nil {
		t.Fatal(err)
	}
	if err := caps.Check(req); err == nil {
		t.Fatal("Check after Reserve should see the cap")
	}
}

// TestNewRecipientWindowSurvivesRestart: losing the 24h window on restart
// would hand out a fresh first-contact budget.
func TestNewRecipientWindowSurvivesRestart(t *testing.T) {
	cfg := capsConfig(t, func(c *Config) { c.NewRecipientsPerDay = 1 })
	clock := &fakeClock{t: time.Now()}
	req := SendRequest{Type: Text, Recipient: chatA, NewRecipient: true}

	if err := newTestCaps(cfg, clock).Reserve(req); err != nil {
		t.Fatal(err)
	}
	restarted := newTestCaps(cfg, clock)
	if err := restarted.Check(SendRequest{Type: Text, Recipient: chatB, NewRecipient: true}); !errors.Is(err, ErrSendCapReached) {
		t.Fatalf("after restart: %v, want cap", err)
	}
}

func TestCorruptCapsStateFailsSafe(t *testing.T) {
	tests := []struct {
		name    string
		content string
	}{
		{"not json", "{\"newRecipients\": [tru"},
		{"wrong type", `{"newRecipients": "lots"}`},
		{"future timestamp", `{"newRecipients": ["2999-01-01T00:00:00Z"]}`},
		{"out of order", `{"newRecipients": ["2026-10-23T08:00:00Z", "2026-10-23T07:00:00Z"]}`},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := capsConfig(t, func(c *Config) { c.NewRecipientsPerDay = 3 })
			if err := os.WriteFile(cfg.CapsStatePath, []byte(tt.content), 0o600); err != nil {
				t.Fatal(err)
			}
			clock := &fakeClock{t: time.Date(2026, 10, 23, 9, 0, 0, 0, time.UTC)}
			caps := newTestCaps(cfg, clock)

			newContact := SendRequest{Type: Text, Recipient: chatB, NewRecipient: true}
			if err := caps.Check(newContact); !errors.Is(err, ErrSendCapReached) {
				t.Fatalf("corrupt state must block first contacts, got %v", err)
			}
			if err := caps.Check(SendRequest{Type: Text, Recipient: chatA}); err != nil {
				t.Fatalf("replies must keep working, got %v", err)
			}
			assertQuarantined(t, cfg.CapsStatePath)

			clock.t = clock.t.Add(24*time.Hour + time.Second)
			if err := caps.Check(newContact); err != nil {
				t.Fatalf("budget should be back after 24h, got %v", err)
			}
		})
	}
}

func TestWarmUpCorruptStateFailsSafe(t *testing.T) {
	aged := time.Now().Add(-30 * 24 * time.Hour).UTC().Format(time.RFC3339)
	recent := time.Now().Add(-time.Hour).UTC().Format(time.RFC3339)
	tests := []struct {
		name        string
		content     string
		wantDayZero bool // fail safe: back to the first, most restrictive day
		quarantined bool
	}{
		{"truncated json", `{"startDate": "2026-0`, true, true},
		{"not an object", `[1,2,3]`, true, true},
		{"negative count", `{"startDate":"` + aged + `","daySent":{"2026-10-23":-500},"lastSendTime":"` + recent + `"}`, true, true},
		{"start in the future", `{"startDate":"2999-01-01T00:00:00Z","daySent":{}}`, true, true},
		{"last send in the future", `{"startDate":"` + aged + `","daySent":{},"lastSendTime":"2999-01-01T00:00:00Z"}`, true, true},
		// Control: a healthy aged account must keep its progress.
		{"valid aged state", `{"startDate":"` + aged + `","daySent":{},"lastSendTime":"` + recent + `"}`, false, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "warmup.json")
			if err := os.WriteFile(path, []byte(tt.content), 0o600); err != nil {
				t.Fatal(err)
			}
			cfg := &Config{Enabled: true, WarmUpDays: 7, WarmUpStartLimit: 20, WarmUpStatePath: path}
			w, err := NewWarmUp(cfg)
			if err != nil {
				t.Fatalf("bad state must not be an error (the caller used to drop antiban on it): %v", err)
			}
			if tt.wantDayZero {
				if w.Day() != 0 || w.DailyLimit() != 20 {
					t.Fatalf("day=%d limit=%d, want day 0 with the start limit", w.Day(), w.DailyLimit())
				}
			} else if w.Day() < 7 {
				t.Fatalf("valid state lost its progress: day %d", w.Day())
			}
			if tt.quarantined {
				assertQuarantined(t, path)
			}
		})
	}
}

// TestInterceptorCorruptWarmUpStillLimits is the end-to-end form of the bug:
// a corrupt file must leave a working, restrictive interceptor, not none.
func TestInterceptorCorruptWarmUpStillLimits(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "warmup.json")
	if err := os.WriteFile(path, []byte("garbage"), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg := &Config{
		Enabled: true, WarmUpDays: 7, WarmUpStartLimit: 2, WarmUpStatePath: path,
		CapsEnabled: true, CapsStatePath: filepath.Join(dir, "caps.json"),
	}
	s, err := NewSendInterceptor(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Close() }()

	req := SendRequest{Type: Reaction, Recipient: chatA} // no pacing delay configured
	for i := range 2 {
		if err := s.BeforeSend(context.Background(), req); err != nil {
			t.Fatalf("send %d: %v", i, err)
		}
		s.AfterSend(Reaction)
	}
	if err := s.BeforeSend(context.Background(), req); !errors.Is(err, ErrWarmUpLimitReached) {
		t.Fatalf("third send: %v, want warm-up limit", err)
	}
}

// TestInterceptorCapsWithoutPacing: caps must hold with ANTIBAN_ENABLED off.
func TestInterceptorCapsWithoutPacing(t *testing.T) {
	cfg := capsConfig(t, func(c *Config) { c.RecipientMaxPerMinute = 1 })
	s, err := NewSendInterceptor(cfg)
	if err != nil {
		t.Fatal(err)
	}
	req := SendRequest{Type: Text, Recipient: chatA}
	if err := s.BeforeSend(context.Background(), req); err != nil {
		t.Fatal(err)
	}
	if err := s.BeforeSend(context.Background(), req); !errors.Is(err, ErrSendCapReached) {
		t.Fatalf("second send: %v, want cap", err)
	}
}

func assertQuarantined(t *testing.T, path string) {
	t.Helper()
	if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("corrupt file should have been moved aside, stat err = %v", err)
	}
	matches, _ := filepath.Glob(path + ".corrupt-*")
	if len(matches) != 1 {
		t.Fatalf("want one quarantined copy, got %v", matches)
	}
	if data, _ := os.ReadFile(matches[0]); len(data) == 0 {
		t.Fatal("quarantined copy is empty")
	}
}

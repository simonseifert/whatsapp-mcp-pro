package whatsapp

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"

	"whatsapp-bridge/internal/database"
)

const day = 24 * time.Hour

type fakePhoneStore struct {
	mu    sync.Mutex
	st    database.PhoneStatus
	saves int
}

func (f *fakePhoneStore) LoadPhoneStatus() (database.PhoneStatus, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.st, nil
}

func (f *fakePhoneStore) SavePhoneStatus(st database.PhoneStatus) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.st = st
	f.saves++
	return nil
}

type fakeClock struct{ t time.Time }

func (c *fakeClock) now() time.Time { return c.t }

func newTestPhoneMonitor(t *testing.T, store *fakePhoneStore, clock *fakeClock, pingEnabled bool, pingErr error) (*PhoneMonitor, *int) {
	t.Helper()
	pings := 0
	ping := func(context.Context) error {
		pings++
		return pingErr
	}
	m, err := newPhoneMonitor(context.Background(), store, waLog.Noop, pingEnabled, 7*day, ping, func() bool { return true }, clock.now)
	if err != nil {
		t.Fatalf("newPhoneMonitor: %v", err)
	}
	return m, &pings
}

func TestShouldPingPhone(t *testing.T) {
	now := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	tests := []struct {
		name       string
		offline    time.Duration
		lastPinged time.Time
		pingAfter  time.Duration
		want       bool
	}{
		{name: "recently seen", offline: 2 * day, pingAfter: 7 * day, want: false},
		{name: "exactly at threshold", offline: 7 * day, pingAfter: 7 * day, want: false},
		{name: "past threshold never pinged", offline: 7*day + time.Minute, pingAfter: 7 * day, want: true},
		{name: "pinged an hour ago", offline: 9 * day, lastPinged: now.Add(-time.Hour), pingAfter: 7 * day, want: false},
		{name: "pinged 23h ago", offline: 9 * day, lastPinged: now.Add(-23 * time.Hour), pingAfter: 7 * day, want: false},
		{name: "pinged a day ago", offline: 9 * day, lastPinged: now.Add(-day), pingAfter: 7 * day, want: true},
		{name: "custom shorter threshold", offline: 4 * day, pingAfter: 3 * day, want: true},
		{name: "zero threshold disables", offline: 30 * day, pingAfter: 0, want: false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := shouldPingPhone(tt.offline, tt.lastPinged, now, tt.pingAfter); got != tt.want {
				t.Errorf("shouldPingPhone = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestPhoneAlertLevelFor(t *testing.T) {
	tests := []struct {
		offline time.Duration
		want    phoneAlertLevel
	}{
		{0, phoneAlertNone},
		{6 * day, phoneAlertNone},
		{7 * day, phoneAlertNone},
		{7*day + time.Second, phoneAlertWarn},
		{12 * day, phoneAlertWarn},
		{12*day + time.Second, phoneAlertError},
		{20 * day, phoneAlertError},
	}
	for _, tt := range tests {
		t.Run(tt.offline.String(), func(t *testing.T) {
			if got := phoneAlertLevelFor(tt.offline); got != tt.want {
				t.Errorf("phoneAlertLevelFor(%v) = %v, want %v", tt.offline, got, tt.want)
			}
		})
	}
}

func TestShouldLogPhoneAlert(t *testing.T) {
	now := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	tests := []struct {
		name      string
		level     phoneAlertLevel
		lastLevel phoneAlertLevel
		lastAt    time.Time
		want      bool
	}{
		{name: "nothing to report", level: phoneAlertNone, want: false},
		{name: "first warning", level: phoneAlertWarn, want: true},
		{name: "warned an hour ago", level: phoneAlertWarn, lastLevel: phoneAlertWarn, lastAt: now.Add(-time.Hour), want: false},
		{name: "warned a day ago", level: phoneAlertWarn, lastLevel: phoneAlertWarn, lastAt: now.Add(-day), want: true},
		{name: "escalation is immediate", level: phoneAlertError, lastLevel: phoneAlertWarn, lastAt: now.Add(-time.Hour), want: true},
		{name: "error repeats daily only", level: phoneAlertError, lastLevel: phoneAlertError, lastAt: now.Add(-time.Hour), want: false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := shouldLogPhoneAlert(tt.level, tt.lastLevel, tt.lastAt, now); got != tt.want {
				t.Errorf("shouldLogPhoneAlert = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestPhoneOfflineFor(t *testing.T) {
	now := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	tests := []struct {
		name string
		st   database.PhoneStatus
		want time.Duration
	}{
		{name: "nothing tracked", want: 0},
		{name: "never seen uses tracking start", st: database.PhoneStatus{TrackingSince: now.Add(-3 * day)}, want: 3 * day},
		{name: "last seen wins", st: database.PhoneStatus{LastSeen: now.Add(-day), TrackingSince: now.Add(-10 * day)}, want: day},
		{name: "future reference clamps to zero", st: database.PhoneStatus{LastSeen: now.Add(time.Hour)}, want: 0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := phoneOfflineFor(tt.st, now); got != tt.want {
				t.Errorf("phoneOfflineFor = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestPhoneSeenWorthSaving(t *testing.T) {
	prev := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	tests := []struct {
		name string
		prev time.Time
		ts   time.Time
		want bool
	}{
		{name: "first sighting", ts: prev, want: true},
		{name: "older sighting", prev: prev, ts: prev.Add(-day), want: false},
		{name: "within granularity", prev: prev, ts: prev.Add(30 * time.Minute), want: false},
		{name: "an hour later", prev: prev, ts: prev.Add(time.Hour), want: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := phoneSeenWorthSaving(tt.prev, tt.ts); got != tt.want {
				t.Errorf("phoneSeenWorthSaving = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestIsFromPrimaryPhone(t *testing.T) {
	own := types.NewJID("4915100000000", types.DefaultUserServer)
	companion := own
	companion.Device = 3
	other := types.NewJID("4915111111111", types.DefaultUserServer)

	tests := []struct {
		name string
		src  types.MessageSource
		want bool
	}{
		{name: "own phone", src: types.MessageSource{IsFromMe: true, Sender: own}, want: true},
		{name: "own other companion", src: types.MessageSource{IsFromMe: true, Sender: companion}, want: false},
		{name: "contact's phone", src: types.MessageSource{IsFromMe: false, Sender: other}, want: false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := isFromPrimaryPhone(tt.src); got != tt.want {
				t.Errorf("isFromPrimaryPhone = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestPhoneMonitorStampsTrackingSinceOnce(t *testing.T) {
	start := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	clock := &fakeClock{t: start}
	store := &fakePhoneStore{}

	newTestPhoneMonitor(t, store, clock, true, nil)
	if !store.st.TrackingSince.Equal(start) {
		t.Fatalf("TrackingSince = %v, want %v", store.st.TrackingSince, start)
	}

	// A restart later must keep the original start, or an outage spanning a
	// restart would be measured from the restart.
	clock.t = start.Add(5 * day)
	m, _ := newTestPhoneMonitor(t, store, clock, true, nil)
	if !store.st.TrackingSince.Equal(start) {
		t.Fatalf("TrackingSince moved to %v on restart", store.st.TrackingSince)
	}
	if _, offline, ok := m.Status(); !ok || offline != 5*day {
		t.Errorf("Status offline = %v ok=%v, want 5d true", offline, ok)
	}
}

func TestPhoneMonitorEventsUpdateLastSeen(t *testing.T) {
	now := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	own := types.NewJID("4915100000000", types.DefaultUserServer)
	companion := own
	companion.Device = 7

	tests := []struct {
		name     string
		evt      interface{}
		wantSeen time.Time
	}{
		{
			name:     "receipt from phone",
			evt:      &events.Receipt{MessageSource: types.MessageSource{IsFromMe: true, Sender: own}, Timestamp: now.Add(-time.Hour)},
			wantSeen: now.Add(-time.Hour),
		},
		{
			name:     "message from phone",
			evt:      &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{IsFromMe: true, Sender: own}, Timestamp: now.Add(-2 * time.Hour)}},
			wantSeen: now.Add(-2 * time.Hour),
		},
		{
			name:     "receipt from another companion is ignored",
			evt:      &events.Receipt{MessageSource: types.MessageSource{IsFromMe: true, Sender: companion}, Timestamp: now},
			wantSeen: time.Time{},
		},
		{
			name:     "media retry answer",
			evt:      &events.MediaRetry{Timestamp: now.Add(-3 * time.Hour)},
			wantSeen: now.Add(-3 * time.Hour),
		},
		{
			name:     "future timestamp clamps to now",
			evt:      &events.Receipt{MessageSource: types.MessageSource{IsFromMe: true, Sender: own}, Timestamp: now.Add(48 * time.Hour)},
			wantSeen: now,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			store := &fakePhoneStore{}
			m, _ := newTestPhoneMonitor(t, store, &fakeClock{t: now}, true, nil)
			m.handleEvent(tt.evt)
			lastSeen, _, _ := m.Status()
			if !lastSeen.Equal(tt.wantSeen) {
				t.Errorf("lastSeen = %v, want %v", lastSeen, tt.wantSeen)
			}
			if !store.st.LastSeen.Equal(tt.wantSeen) {
				t.Errorf("persisted lastSeen = %v, want %v", store.st.LastSeen, tt.wantSeen)
			}
		})
	}
}

func TestPhoneMonitorCheckPings(t *testing.T) {
	now := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	tests := []struct {
		name        string
		lastSeen    time.Time
		lastPinged  time.Time
		pingEnabled bool
		pingErr     error
		wantPings   int
	}{
		{name: "phone recently seen", lastSeen: now.Add(-day), pingEnabled: true, wantPings: 0},
		{name: "offline 8 days", lastSeen: now.Add(-8 * day), pingEnabled: true, wantPings: 1},
		{name: "offline but opted out", lastSeen: now.Add(-8 * day), pingEnabled: false, wantPings: 0},
		{name: "already pinged today", lastSeen: now.Add(-8 * day), lastPinged: now.Add(-2 * time.Hour), pingEnabled: true, wantPings: 0},
		{name: "failed ping still counts", lastSeen: now.Add(-8 * day), pingEnabled: true, pingErr: errors.New("not connected"), wantPings: 1},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			store := &fakePhoneStore{st: database.PhoneStatus{
				LastSeen:      tt.lastSeen,
				LastPinged:    tt.lastPinged,
				TrackingSince: now.Add(-30 * day),
			}}
			clock := &fakeClock{t: now}
			m, pings := newTestPhoneMonitor(t, store, clock, tt.pingEnabled, tt.pingErr)

			m.check(context.Background())
			if *pings != tt.wantPings {
				t.Fatalf("pings = %d, want %d", *pings, tt.wantPings)
			}
			if tt.wantPings > 0 && !store.st.LastPinged.Equal(now) {
				t.Errorf("persisted LastPinged = %v, want %v", store.st.LastPinged, now)
			}

			// A second check an hour later must not ping again.
			clock.t = now.Add(time.Hour)
			m.check(context.Background())
			if *pings != tt.wantPings {
				t.Errorf("pings after second check = %d, want %d", *pings, tt.wantPings)
			}
		})
	}
}

func TestPhoneMonitorSkipsWhenUnlinked(t *testing.T) {
	now := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	store := &fakePhoneStore{st: database.PhoneStatus{LastSeen: now.Add(-10 * day), TrackingSince: now.Add(-30 * day)}}
	pings := 0
	m, err := newPhoneMonitor(context.Background(), store, waLog.Noop, true, 7*day,
		func(context.Context) error { pings++; return nil }, func() bool { return false }, func() time.Time { return now })
	if err != nil {
		t.Fatalf("newPhoneMonitor: %v", err)
	}
	m.check(context.Background())
	if pings != 0 {
		t.Errorf("pinged %d times while unlinked", pings)
	}
}

func TestPhoneMonitorSightingResetsAlerts(t *testing.T) {
	now := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	own := types.NewJID("4915100000000", types.DefaultUserServer)
	store := &fakePhoneStore{st: database.PhoneStatus{LastSeen: now.Add(-13 * day), TrackingSince: now.Add(-30 * day)}}
	m, _ := newTestPhoneMonitor(t, store, &fakeClock{t: now}, false, nil)

	m.check(context.Background())
	if m.lastAlertLevel != phoneAlertError {
		t.Fatalf("lastAlertLevel = %v, want error", m.lastAlertLevel)
	}
	m.handleEvent(&events.Receipt{MessageSource: types.MessageSource{IsFromMe: true, Sender: own}, Timestamp: now})
	if m.lastAlertLevel != phoneAlertNone || !m.lastAlertAt.IsZero() {
		t.Errorf("alert state not reset after sighting: level=%v at=%v", m.lastAlertLevel, m.lastAlertAt)
	}
	if _, offline, _ := m.Status(); offline != 0 {
		t.Errorf("offline after sighting = %v, want 0", offline)
	}
}

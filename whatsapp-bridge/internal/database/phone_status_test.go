package database

import (
	"testing"
	"time"
)

func TestPhoneStatusRoundTrip(t *testing.T) {
	base := time.Date(2026, 10, 1, 12, 30, 45, 123_000_000, time.UTC)

	tests := []struct {
		name string
		in   PhoneStatus
	}{
		{name: "all zero", in: PhoneStatus{}},
		{name: "tracking only", in: PhoneStatus{TrackingSince: base}},
		{name: "all set", in: PhoneStatus{
			LastSeen:      base.Add(48 * time.Hour),
			LastPinged:    base.Add(72 * time.Hour),
			TrackingSince: base,
		}},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			store := newTestMessageStore(t)
			if err := store.SavePhoneStatus(tt.in); err != nil {
				t.Fatalf("SavePhoneStatus: %v", err)
			}
			got, err := store.LoadPhoneStatus()
			if err != nil {
				t.Fatalf("LoadPhoneStatus: %v", err)
			}
			if !got.LastSeen.Equal(tt.in.LastSeen) || !got.LastPinged.Equal(tt.in.LastPinged) || !got.TrackingSince.Equal(tt.in.TrackingSince) {
				t.Errorf("round trip = %+v, want %+v", got, tt.in)
			}
		})
	}
}

func TestLoadPhoneStatusEmptyStore(t *testing.T) {
	store := newTestMessageStore(t)
	got, err := store.LoadPhoneStatus()
	if err != nil {
		t.Fatalf("LoadPhoneStatus: %v", err)
	}
	if got != (PhoneStatus{}) {
		t.Errorf("empty store = %+v, want zero", got)
	}
}

func TestSavePhoneStatusOverwrites(t *testing.T) {
	store := newTestMessageStore(t)
	first := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	second := first.Add(5 * 24 * time.Hour)

	if err := store.SavePhoneStatus(PhoneStatus{LastSeen: first, TrackingSince: first}); err != nil {
		t.Fatalf("first save: %v", err)
	}
	if err := store.SavePhoneStatus(PhoneStatus{LastSeen: second, TrackingSince: first}); err != nil {
		t.Fatalf("second save: %v", err)
	}
	got, err := store.LoadPhoneStatus()
	if err != nil {
		t.Fatalf("LoadPhoneStatus: %v", err)
	}
	if !got.LastSeen.Equal(second) {
		t.Errorf("LastSeen = %v, want %v", got.LastSeen, second)
	}
	var rows int
	if err := store.db.QueryRow(`SELECT COUNT(*) FROM phone_status`).Scan(&rows); err != nil {
		t.Fatalf("count: %v", err)
	}
	if rows != 1 {
		t.Errorf("phone_status rows = %d, want 1", rows)
	}
}

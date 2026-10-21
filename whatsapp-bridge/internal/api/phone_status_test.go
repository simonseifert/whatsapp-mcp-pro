package api

import (
	"encoding/json"
	"testing"
	"time"

	"whatsapp-bridge/internal/types"
)

func TestPhoneStatusFields(t *testing.T) {
	seen := time.Date(2026, 10, 14, 9, 30, 0, 0, time.UTC)
	tests := []struct {
		name     string
		lastSeen time.Time
		offline  time.Duration
		ok       bool
		wantJSON string
	}{
		{name: "monitor not running", ok: false, wantJSON: `{"phone_last_seen":null,"phone_offline_days":null}`},
		{name: "never seen", offline: 36 * time.Hour, ok: true, wantJSON: `{"phone_last_seen":null,"phone_offline_days":1.5}`},
		{name: "seen", lastSeen: seen, offline: 8*24*time.Hour + 3*time.Hour, ok: true, wantJSON: `{"phone_last_seen":"2026-10-14T09:30:00Z","phone_offline_days":8.1}`},
		{name: "just seen", lastSeen: seen, offline: 0, ok: true, wantJSON: `{"phone_last_seen":"2026-10-14T09:30:00Z","phone_offline_days":0}`},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var resp types.ConnectionStatusResponse
			resp.PhoneLastSeen, resp.PhoneOfflineDays = phoneStatusFields(tt.lastSeen, tt.offline, tt.ok)
			raw, err := json.Marshal(resp)
			if err != nil {
				t.Fatalf("marshal: %v", err)
			}
			var got, want map[string]any
			if err := json.Unmarshal(raw, &got); err != nil {
				t.Fatalf("unmarshal got: %v", err)
			}
			if err := json.Unmarshal([]byte(tt.wantJSON), &want); err != nil {
				t.Fatalf("unmarshal want: %v", err)
			}
			for k, w := range want {
				if g, present := got[k]; !present || g != w {
					t.Errorf("%s = %v (present %v), want %v; full: %s", k, g, present, w, raw)
				}
			}
		})
	}
}

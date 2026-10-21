package config

import (
	"testing"
	"time"
)

func TestParseDays(t *testing.T) {
	tests := []struct {
		in      string
		want    time.Duration
		wantErr bool
	}{
		{in: "7d", want: 7 * 24 * time.Hour},
		{in: " 10d ", want: 10 * 24 * time.Hour},
		{in: "0.5d", want: 12 * time.Hour},
		{in: "168h", want: 168 * time.Hour},
		{in: "30m", want: 30 * time.Minute},
		{in: "xd", wantErr: true},
		{in: "seven", wantErr: true},
	}
	for _, tt := range tests {
		t.Run(tt.in, func(t *testing.T) {
			got, err := ParseDays(tt.in)
			if (err != nil) != tt.wantErr {
				t.Fatalf("ParseDays(%q) err = %v, wantErr %v", tt.in, err, tt.wantErr)
			}
			if !tt.wantErr && got != tt.want {
				t.Errorf("ParseDays(%q) = %v, want %v", tt.in, got, tt.want)
			}
		})
	}
}

func TestPhonePingConfigFromEnv(t *testing.T) {
	tests := []struct {
		name        string
		enabled     string
		after       string
		wantEnabled bool
		wantAfter   time.Duration
	}{
		{name: "defaults", wantEnabled: true, wantAfter: 7 * 24 * time.Hour},
		{name: "opt out", enabled: "false", wantEnabled: false, wantAfter: 7 * 24 * time.Hour},
		{name: "custom days", after: "5d", wantEnabled: true, wantAfter: 5 * 24 * time.Hour},
		{name: "invalid keeps default", after: "soon", wantEnabled: true, wantAfter: 7 * 24 * time.Hour},
		{name: "non-positive keeps default", after: "0d", wantEnabled: true, wantAfter: 7 * 24 * time.Hour},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv("PHONE_PING_ENABLED", tt.enabled)
			t.Setenv("PHONE_OFFLINE_PING_AFTER", tt.after)
			cfg := NewConfig()
			if cfg.PhonePingEnabled != tt.wantEnabled {
				t.Errorf("PhonePingEnabled = %v, want %v", cfg.PhonePingEnabled, tt.wantEnabled)
			}
			if cfg.PhoneOfflinePingAfter != tt.wantAfter {
				t.Errorf("PhoneOfflinePingAfter = %v, want %v", cfg.PhoneOfflinePingAfter, tt.wantAfter)
			}
		})
	}
}

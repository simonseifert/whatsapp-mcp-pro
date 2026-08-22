package api

import "testing"

func TestValidateCDNURL(t *testing.T) {
	t.Setenv("DISABLE_SSRF_CHECK", "")
	tests := []struct {
		url string
		ok  bool
	}{
		{"https://mmg.whatsapp.net/v/t62.7118-24/abc.enc", true},
		{"https://media-fra3-2.cdn.whatsapp.net/x.enc", true},
		{"https://scontent.xx.fbcdn.net/m.enc", true},
		{"http://mmg.whatsapp.net/x.enc", false},             // https only
		{"https://169.254.169.254/latest/meta-data/", false}, // metadata IP
		{"https://internal.service.local/x", false},          // arbitrary host
		{"https://evil-whatsapp.net.attacker.com/x", false},  // suffix spoof
		{"://bad", false},
	}
	for _, tt := range tests {
		err := validateCDNURL(tt.url)
		if tt.ok && err != nil {
			t.Errorf("validateCDNURL(%s) = %v, want nil", tt.url, err)
		}
		if !tt.ok && err == nil {
			t.Errorf("validateCDNURL(%s) = nil, want error", tt.url)
		}
	}

	t.Setenv("DISABLE_SSRF_CHECK", "true")
	if err := validateCDNURL("https://internal.service.local/x"); err != nil {
		t.Errorf("escape hatch DISABLE_SSRF_CHECK=true not honored: %v", err)
	}
}

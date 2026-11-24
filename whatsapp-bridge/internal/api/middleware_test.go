package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestIsAuthenticatedLoopback(t *testing.T) {
	t.Setenv("API_KEY", "secret")
	tests := []struct {
		name, remote, key string
		want              bool
	}{
		{"loopback with key", "127.0.0.1:5555", "secret", true},
		{"ipv6 loopback with key", "[::1]:5555", "secret", true},
		{"loopback wrong key", "127.0.0.1:5555", "nope", false},
		{"loopback no key", "127.0.0.1:5555", "", false},
		{"remote with key", "100.64.0.7:5555", "secret", false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			r := httptest.NewRequest(http.MethodGet, "/api/chats", nil)
			r.RemoteAddr = tt.remote
			if tt.key != "" {
				r.Header.Set("X-API-Key", tt.key)
			}
			if got := isAuthenticatedLoopback(r); got != tt.want {
				t.Errorf("got %v, want %v", got, tt.want)
			}
		})
	}
}

func TestIsAuthenticatedLoopbackIgnoresForwardedHeader(t *testing.T) {
	t.Setenv("API_KEY", "secret")
	t.Setenv("TRUST_PROXY_HEADERS", "true")
	r := httptest.NewRequest(http.MethodGet, "/api/chats", nil)
	r.RemoteAddr = "100.64.0.7:5555"
	r.Header.Set("X-Forwarded-For", "127.0.0.1")
	r.Header.Set("X-API-Key", "secret")
	if isAuthenticatedLoopback(r) {
		t.Error("a forwarded header must not make a remote caller loopback")
	}
}

// Origins come only from CORS_ORIGINS; the old built-in localhost ports, the
// :8090 one included, are gone (review-bridge #8).
func TestAllowedOriginsComeOnlyFromConfig(t *testing.T) {
	tests := []struct {
		name    string
		env     string
		allowed []string
		denied  []string
	}{
		{"unset trusts nothing", "", nil,
			[]string{"http://localhost:8089", "http://localhost:8090", "http://127.0.0.1:8089", "http://127.0.0.1:8090", ""}},
		{"configured origins only", "https://a.example, https://b.example",
			[]string{"https://a.example", "https://b.example"}, []string{"http://localhost:8090", "https://c.example"}},
		{"empty entries ignored", "https://a.example,,", []string{"https://a.example"}, []string{""}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv("CORS_ORIGINS", tt.env)
			got := getAllowedOrigins()
			for _, o := range tt.allowed {
				if !got[o] {
					t.Errorf("%q not allowed", o)
				}
			}
			for _, o := range tt.denied {
				if got[o] {
					t.Errorf("%q allowed", o)
				}
			}
		})
	}
}

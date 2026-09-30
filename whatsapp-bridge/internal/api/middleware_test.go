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

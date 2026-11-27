package api

import (
	"net/http"
	"testing"
)

func TestSendPassesForwardedFlag(t *testing.T) {
	f := &fakeSender{}
	s := newSendTestServer(t, f)
	post(t, s.handleSendMessage, map[string]any{"recipient": "1@s.whatsapp.net", "message": "hi", "forwarded": true}, nil)
	post(t, s.handleSendMessage, map[string]any{"recipient": "1@s.whatsapp.net", "message": "hi"}, nil)
	if len(f.sendOpts) != 2 || !f.sendOpts[0].Forwarded || f.sendOpts[1].Forwarded {
		t.Fatalf("send options = %+v", f.sendOpts)
	}
}

// Validation runs before the client is touched; a nil client would panic.
func TestStarAndMarkUnreadValidate(t *testing.T) {
	s := &Server{}
	tests := []struct {
		name    string
		handler http.HandlerFunc
		method  string
		body    map[string]any
	}{
		{"star without chat", s.handleStarMessage, http.MethodPost, map[string]any{"message_id": "M1", "star": true}},
		{"star without message", s.handleStarMessage, http.MethodPost, map[string]any{"chat_jid": "1@s.whatsapp.net", "star": true}},
		{"mark unread without chat", s.handleMarkUnread, http.MethodPost, map[string]any{"unread": true}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if rec := post(t, tt.handler, tt.body, nil); rec.Code != http.StatusBadRequest {
				t.Fatalf("status %d, want 400: %s", rec.Code, rec.Body)
			}
		})
	}
}

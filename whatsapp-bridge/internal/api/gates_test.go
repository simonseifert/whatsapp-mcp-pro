package api

import (
	"net/http"
	"testing"
)

// The group, timer, typing and newsletter endpoints reach the concrete client,
// so only the SEND_ALLOWED_JIDS refusal (which must come first) can run here.
// A gate that fails open would call the nil client and panic, which fails the
// test. The client-side gates are covered in the whatsapp package.
func TestActionEndpointsApplySendAllowlist(t *testing.T) {
	const allowed, other = "4915100000001@s.whatsapp.net", "4915199999999@s.whatsapp.net"
	const allowedGroup, otherGroup = "120363000000000001@g.us", "120363000000000002@g.us"
	tests := []struct {
		name    string
		handler func(s *Server) http.HandlerFunc
		body    map[string]any
	}{
		{"group create, unlisted participant", func(s *Server) http.HandlerFunc { return s.handleCreateGroup },
			map[string]any{"name": "g", "participants": []string{allowed, other}}},
		{"group create, only listed first then unlisted LID", func(s *Server) http.HandlerFunc { return s.handleCreateGroup },
			map[string]any{"name": "g", "participants": []string{"4915100000001@lid"}}},
		{"group create, no participants", func(s *Server) http.HandlerFunc { return s.handleCreateGroup },
			map[string]any{"name": "g"}},
		{"add members, unlisted group", func(s *Server) http.HandlerFunc { return s.handleAddGroupMembers },
			map[string]any{"group_jid": otherGroup, "participants": []string{allowed}}},
		{"add members, unlisted participant", func(s *Server) http.HandlerFunc { return s.handleAddGroupMembers },
			map[string]any{"group_jid": allowedGroup, "participants": []string{allowed, other}}},
		{"group rename, unlisted group", func(s *Server) http.HandlerFunc { return s.handleUpdateGroup },
			map[string]any{"group_jid": otherGroup, "name": "x"}},
		{"group topic, unlisted group", func(s *Server) http.HandlerFunc { return s.handleUpdateGroup },
			map[string]any{"group_jid": otherGroup, "topic": "x"}},
		{"disappearing, unlisted chat", func(s *Server) http.HandlerFunc { return s.handleSetDisappearingTimer },
			map[string]any{"chat_jid": other, "duration": "24h"}},
		{"typing, unlisted chat", func(s *Server) http.HandlerFunc { return s.handleSendTyping },
			map[string]any{"chat_jid": other}},
		{"typing, bare number never matches a group", func(s *Server) http.HandlerFunc { return s.handleSendTyping },
			map[string]any{"chat_jid": "4915100000001@g.us"}},
		{"newsletter create with an allowlist set", func(s *Server) http.HandlerFunc { return s.handleCreateNewsletter },
			map[string]any{"name": "n"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			setSendAllowlist(t, allowed+","+allowedGroup)
			s := newSendTestServer(t, &fakeSender{})
			rec := post(t, tt.handler(s), tt.body, nil)
			if rec.Code != http.StatusForbidden {
				t.Fatalf("status %d, want 403: %s", rec.Code, rec.Body)
			}
		})
	}
}

// A bare number in SEND_ALLOWED_JIDS means the phone JID only (review #7).
func TestRecipientAllowedBareNumberIsPhoneOnly(t *testing.T) {
	tests := []struct {
		name      string
		allowlist string
		recipient string
		want      bool
	}{
		{"bare number, phone JID", "4915100000001", "4915100000001@s.whatsapp.net", true},
		{"bare number, phone JID with device", "4915100000001", "4915100000001:7@s.whatsapp.net", true},
		{"bare number, bare recipient", "4915100000001", "4915100000001", true},
		{"bare number does not match a LID with the same digits", "4915100000001", "4915100000001@lid", false},
		{"bare number does not match a group", "4915100000001", "4915100000001@g.us", false},
		{"explicit LID entry matches the LID", "4915100000001@lid", "4915100000001@lid", true},
		{"explicit LID entry does not match the phone", "4915100000001@lid", "4915100000001@s.whatsapp.net", false},
		{"group entry", "120363000000000001@g.us", "120363000000000001@g.us", true},
		{"no allowlist allows all", "", "4915100000001@lid", true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			setSendAllowlist(t, tt.allowlist)
			if got := recipientAllowed(tt.recipient); got != tt.want {
				t.Fatalf("recipientAllowed(%q) with %q = %v, want %v", tt.recipient, tt.allowlist, got, tt.want)
			}
		})
	}
}

package api

import (
	"net/http"
	"os"
	"strings"
	"sync"
)

// Send allowlist (upstream issue #47): when SEND_ALLOWED_JIDS is set to a
// comma-separated list of JIDs, every outbound send is checked against it and
// non-listed recipients are rejected. Unset or empty = allow all (default).
//
// Entries match a full JID exactly, or a bare number against the user part of a
// phone JID (@s.whatsapp.net) only. A LID's digits can equal someone else's
// phone number, so a bare number never matches a LID or a group; those need
// their full JID listed.
var (
	sendAllowlistOnce sync.Once
	sendAllowlist     map[string]bool
)

func loadSendAllowlist() {
	raw := strings.TrimSpace(os.Getenv("SEND_ALLOWED_JIDS"))
	if raw == "" {
		return
	}
	sendAllowlist = make(map[string]bool)
	for _, entry := range strings.Split(raw, ",") {
		entry = strings.TrimSpace(entry)
		if entry != "" {
			sendAllowlist[entry] = true
		}
	}
}

// refuseUnlistedRecipient writes a 403 and returns true when chatJID fails the
// SEND_ALLOWED_JIDS gate. Every endpoint that puts a message in a chat (send,
// reaction, poll, edit, delete) calls it: gating only /api/send let a sender
// confined to one chat still react, poll, edit or revoke in any other.
func refuseUnlistedRecipient(w http.ResponseWriter, chatJID string) bool {
	if recipientAllowed(chatJID) {
		return false
	}
	SendJSONError(w, "Recipient not in SEND_ALLOWED_JIDS allowlist", http.StatusForbidden)
	return true
}

// refuseUnlisted is refuseUnlistedRecipient for a chat that may be filed under
// its LID: the LID is also checked as the phone JID the bridge maps it to, so
// an allowlisted contact stays reachable under either identity.
func (s *Server) refuseUnlisted(w http.ResponseWriter, chatJID string) bool {
	if recipientAllowed(chatJID) {
		return false
	}
	if s.sender != nil {
		if mapped := s.sender.NormalizeChat(chatJID); mapped != chatJID && recipientAllowed(mapped) {
			return false
		}
	}
	return refuseUnlistedRecipient(w, chatJID)
}

// refuseUnlistedAll applies refuseUnlisted to every JID an action contacts and
// stops at the first refusal, which it has already written.
func (s *Server) refuseUnlistedAll(w http.ResponseWriter, jids ...string) bool {
	for _, j := range jids {
		if s.refuseUnlisted(w, j) {
			return true
		}
	}
	return false
}

// sendAllowlistActive reports whether SEND_ALLOWED_JIDS is configured.
func sendAllowlistActive() bool {
	sendAllowlistOnce.Do(loadSendAllowlist)
	return sendAllowlist != nil
}

// recipientAllowed reports whether the recipient passes the send allowlist.
func recipientAllowed(recipient string) bool {
	sendAllowlistOnce.Do(loadSendAllowlist)
	if sendAllowlist == nil {
		return true
	}
	if sendAllowlist[recipient] {
		return true
	}
	user, server, hasServer := strings.Cut(recipient, "@")
	if j := strings.IndexByte(user, ':'); j >= 0 {
		user = user[:j] // device suffix
	}
	if !hasServer {
		return sendAllowlist[user] // a bare number is sent as a phone JID
	}
	if sendAllowlist[user+"@"+server] {
		return true
	}
	return server == "s.whatsapp.net" && sendAllowlist[user]
}

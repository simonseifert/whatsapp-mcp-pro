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
// Entries match exactly, or by bare number against the JID's user part, so
// "4915112345678" allows both 4915112345678@s.whatsapp.net and @lid forms.
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

// recipientAllowed reports whether the recipient passes the send allowlist.
func recipientAllowed(recipient string) bool {
	sendAllowlistOnce.Do(loadSendAllowlist)
	if sendAllowlist == nil {
		return true
	}
	if sendAllowlist[recipient] {
		return true
	}
	user := recipient
	if i := strings.IndexByte(user, '@'); i >= 0 {
		user = user[:i]
	}
	if j := strings.IndexByte(user, ':'); j >= 0 {
		user = user[:j]
	}
	return sendAllowlist[user]
}

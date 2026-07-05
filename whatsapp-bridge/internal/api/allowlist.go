package api

import (
	"os"
	"strings"
	"sync"
)

// Send allowlist (upstream issue #47): when SEND_ALLOWED_JIDS is set to a
// comma-separated list of JIDs, every outbound send is checked against it and
// non-listed recipients are rejected. Unset or empty = allow all (default).
//
// Entries match exactly, or by bare number against the JID's user part, so
// "385991234567" allows both 385991234567@s.whatsapp.net and @lid forms.
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

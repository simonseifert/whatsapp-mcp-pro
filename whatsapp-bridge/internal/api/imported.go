package api

import (
	"net/http"

	"whatsapp-bridge/internal/database"
)

// refuseImportedTarget answers 422 when a request would hand WhatsApp the ID
// of a row that came from a chat-export import. Such IDs are synthetic, so a
// reaction, quote, edit or revoke built on one is accepted by the server and
// attaches to nothing: the caller would see success for an action that never
// happened. Returns true when it has written the response.
func refuseImportedTarget(w http.ResponseWriter, messageID string) bool {
	if !database.IsImportedMessageID(messageID) {
		return false
	}
	// With a code, so the MCP doesn't read this 422 as its idempotency one.
	refuseChange(w, http.StatusUnprocessableEntity, codeImported,
		"message "+messageID+" was imported from a chat export and does not exist on WhatsApp")
	return true
}

// withoutImportedIDs splits off the imported IDs of a batch; skipped is how
// many were dropped.
func withoutImportedIDs(ids []string) (kept []string, skipped int) {
	for _, id := range ids {
		if database.IsImportedMessageID(id) {
			skipped++
			continue
		}
		kept = append(kept, id)
	}
	return kept, skipped
}

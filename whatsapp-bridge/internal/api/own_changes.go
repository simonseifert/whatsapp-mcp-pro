package api

import (
	"encoding/json"
	"log"
	"net/http"
	"strings"

	"whatsapp-bridge/internal/database"
)

// Codes of a refused /api/edit or /api/delete, in the body's "code" field.
const (
	codeNotStored      = "message_not_stored"
	codeNotOwnMessage  = "not_own_message"
	codeNotGroupAdmin  = "not_group_admin"
	codeSenderUnknown  = "sender_unknown"
	codeStoreReadError = "store_error"
	codeImported       = "imported_message"
)

// chatForms lists the forms chatJID may be stored under, canonical first.
func (s *Server) chatForms(chatJID string) []string {
	if s.client != nil {
		return s.client.StoredChatForms(chatJID)
	}
	return []string{chatJID}
}

func refuseChange(w http.ResponseWriter, status int, code, msg string) {
	body, _ := json.Marshal(errorBody(code, msg))
	writeJSON(w, status, body)
}

// changeTarget looks up the message an edit or revoke names, answering the
// request itself when it must not go out. WhatsApp does not refuse an edit or
// revoke of a message that isn't ours: the server relays it, recipients
// ignore it, and the caller would see success for something that never
// happened. Worse, recording it would rewrite our store. So the store is the
// check, and a message it doesn't have is refused too: there is nothing to
// check the author against. ok is false when the response has been written.
func (s *Server) changeTarget(w http.ResponseWriter, chatJID, messageID string) (database.StoredAuthor, bool) {
	if s.messageStore == nil {
		refuseChange(w, http.StatusServiceUnavailable, codeStoreReadError, "message store unavailable")
		return database.StoredAuthor{}, false
	}
	author, found, err := s.messageStore.LookupMessageAuthor(messageID, s.chatForms(chatJID))
	if err != nil {
		refuseChange(w, http.StatusInternalServerError, codeStoreReadError, "could not look up message: "+err.Error())
		return author, false
	}
	if !found {
		refuseChange(w, http.StatusUnprocessableEntity, codeNotStored,
			"message "+messageID+" is not stored in "+chatJID+"; only stored messages can be edited or deleted, because their author is checked first")
		return author, false
	}
	return author, true
}

func isGroupChat(chatJID string) bool {
	return strings.HasSuffix(chatJID, "@g.us")
}

// recordOwnChange stores an edit or revoke this bridge just sent. The send
// already succeeded, so a failure here is logged rather than turned into an
// error response: the caller retrying would edit or delete a second time.
func (s *Server) recordOwnChange(record func(chats []string, me string) error, chatJID string) {
	if s.messageStore == nil {
		return
	}
	me := ""
	if s.client != nil {
		me = s.client.OwnJID()
	}
	if err := record(s.chatForms(chatJID), me); err != nil {
		log.Printf("WARN could not record own change in %s: %v", chatJID, err)
	}
}

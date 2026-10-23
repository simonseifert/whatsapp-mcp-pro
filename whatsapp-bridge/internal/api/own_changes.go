package api

import "log"

// recordOwnChange stores an edit or revoke this bridge just sent. The send
// already succeeded, so a failure here is logged rather than turned into an
// error response: the caller retrying would edit or delete a second time.
func (s *Server) recordOwnChange(record func(chats []string, me string) error, chatJID string) {
	if s.messageStore == nil {
		return
	}
	chats := []string{chatJID}
	me := ""
	if s.client != nil {
		chats = s.client.StoredChatForms(chatJID)
		me = s.client.OwnJID()
	}
	if err := record(chats, me); err != nil {
		log.Printf("WARN could not record own change in %s: %v", chatJID, err)
	}
}

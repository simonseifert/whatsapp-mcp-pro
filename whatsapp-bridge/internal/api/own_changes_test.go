package api

import (
	"net/http"
	"testing"
	"time"
)

// Our own edits and deletes never come back from WhatsApp as events, so the
// handlers must record them; without this the edit history only ever showed
// the other side's changes.
func TestOwnEditAndDeleteAreRecorded(t *testing.T) {
	const chat = "4915112345678@s.whatsapp.net"
	f := &fakeSender{}
	s := newSendTestServer(t, f)
	sent := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	if err := s.messageStore.StoreChat(chat, "Max", sent); err != nil {
		t.Fatal(err)
	}
	if err := s.messageStore.StoreMessage("MINE", chat, "", "", "helo", sent, true, "", "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatal(err)
	}

	if rec := post(t, s.handleEditMessage, map[string]any{"chat_jid": chat, "message_id": "MINE", "new_content": "hello"}, nil); rec.Code != http.StatusOK {
		t.Fatalf("edit: %d %s", rec.Code, rec.Body)
	}
	edits, err := s.messageStore.GetMessageEdits("MINE", chat)
	if err != nil || len(edits) != 1 || edits[0].NewContent != "hello" || edits[0].OldContent.String != "helo" || edits[0].Source != "api" {
		t.Fatalf("edits = %+v, %v", edits, err)
	}
	if edits[0].EditID != "3EB0GEN1" {
		t.Errorf("edit_id = %q, want the stanza ID the send returned", edits[0].EditID)
	}

	if rec := post(t, s.handleDeleteMessage, map[string]any{"chat_jid": chat, "message_id": "MINE"}, nil); rec.Code != http.StatusOK {
		t.Fatalf("delete: %d %s", rec.Code, rec.Body)
	}
	msgs, err := s.messageStore.GetMessages(chat, 5)
	if err != nil || len(msgs) != 1 || msgs[0].Content != "hello" {
		t.Fatalf("messages = %+v, %v", msgs, err)
	}
	if _, newly, err := s.messageStore.MarkDeleted("MINE", []string{chat}, time.Now(), ""); err != nil || newly {
		t.Errorf("message was not marked deleted by the handler (newly=%v err=%v)", newly, err)
	}
}

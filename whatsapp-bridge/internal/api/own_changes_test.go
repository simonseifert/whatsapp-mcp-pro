package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"whatsapp-bridge/internal/database"
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
	res, err := s.messageStore.MarkDeleted(database.Revoke{MessageID: "MINE", ChatJIDs: []string{chat}, At: time.Now(), Trusted: true})
	if err != nil || res.Newly {
		t.Errorf("message was not marked deleted by the handler (%+v, err=%v)", res, err)
	}
}

// WhatsApp relays an edit or revoke of someone else's message (recipients
// ignore it), so the handlers check the store before sending. Recording one
// as trusted used to rewrite the store and then lock it against replays of
// the original.
func TestEditAndDeleteRefuseOtherPeoplesMessages(t *testing.T) {
	const (
		dm      = "4915112345678@s.whatsapp.net"
		group   = "120363000000000001@g.us"
		author  = "4915187654321@s.whatsapp.net"
		message = "original"
	)
	sent := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	tests := []struct {
		name     string
		edit     bool
		chat, id string
		admin    bool
		want     int
		wantCode string
		// for a delete that went out: the sender_jid the revoke key named
		wantKey string
	}{
		{name: "edit of an incoming message", edit: true, chat: dm, id: "THEIRS", want: http.StatusForbidden, wantCode: codeNotOwnMessage},
		{name: "edit of an unstored message", edit: true, chat: dm, id: "NOPE", want: http.StatusUnprocessableEntity, wantCode: codeNotStored},
		{name: "edit of our own message", edit: true, chat: dm, id: "MINE", want: http.StatusOK},
		{name: "delete of an incoming 1:1 message", chat: dm, id: "THEIRS", admin: true, want: http.StatusForbidden, wantCode: codeNotOwnMessage},
		{name: "delete of an unstored message", chat: group, id: "NOPE", admin: true, want: http.StatusUnprocessableEntity, wantCode: codeNotStored},
		{name: "delete of another member's message, not admin", chat: group, id: "THEIRS", want: http.StatusForbidden, wantCode: codeNotGroupAdmin},
		{name: "delete of another member's message as admin", chat: group, id: "THEIRS", admin: true, want: http.StatusOK, wantKey: author},
		{name: "delete of our own group message", chat: group, id: "MINE", want: http.StatusOK, wantKey: ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			f := &fakeSender{admin: tc.admin}
			s := newSendTestServer(t, f)
			if err := s.messageStore.StoreChat(tc.chat, "", sent); err != nil {
				t.Fatal(err)
			}
			for _, m := range []struct {
				id, sender string
				fromMe     bool
			}{{"THEIRS", author, false}, {"MINE", "", true}} {
				if err := s.messageStore.StoreMessage(m.id, tc.chat, m.sender, "", message, sent, m.fromMe, "", "", "", "", nil, nil, nil, 0); err != nil {
					t.Fatal(err)
				}
			}
			var rec *httptest.ResponseRecorder
			if tc.edit {
				rec = post(t, s.handleEditMessage, map[string]any{"chat_jid": tc.chat, "message_id": tc.id, "new_content": "rewritten"}, nil)
			} else {
				// Someone else's message goes without sender_jid (the stored
				// sender fills it in); our own with a sender_jid naming someone
				// else, which must not aim the revoke elsewhere.
				body := map[string]any{"chat_jid": tc.chat, "message_id": tc.id}
				if tc.id == "MINE" {
					body["sender_jid"] = author
				}
				rec = post(t, s.handleDeleteMessage, body, nil)
			}
			if rec.Code != tc.want {
				t.Fatalf("status %d, want %d: %s", rec.Code, tc.want, rec.Body)
			}
			if tc.want != http.StatusOK {
				if body := decode(t, rec); body["code"] != tc.wantCode {
					t.Errorf("code = %v, want %s", body["code"], tc.wantCode)
				}
				if f.callCount() != 0 {
					t.Error("a refused change reached WhatsApp")
				}
				msgs, err := s.messageStore.GetMessages(tc.chat, 10)
				if err != nil || len(msgs) != 2 {
					t.Fatalf("messages = %+v, %v; want only the two seeded", msgs, err)
				}
				for _, m := range msgs {
					if m.Content != message {
						t.Errorf("refused edit changed %s to %q", m.ID, m.Content)
					}
				}
				if tc.id != "NOPE" {
					res, err := s.messageStore.MarkDeleted(database.Revoke{MessageID: tc.id, ChatJIDs: []string{tc.chat}, At: time.Now(), Trusted: true})
					if err != nil || !res.Newly {
						t.Errorf("refused delete marked the message deleted (%+v, %v)", res, err)
					}
				}
				return
			}
			if !tc.edit {
				if len(f.deletes) != 1 || f.deletes[0] != tc.wantKey {
					t.Errorf("revoke key sender = %q, want %q", f.deletes, tc.wantKey)
				}
				res, err := s.messageStore.MarkDeleted(database.Revoke{MessageID: tc.id, ChatJIDs: []string{tc.chat}, At: time.Now(), Trusted: true})
				if err != nil || res.Newly {
					t.Errorf("handler did not record the revoke (%+v, %v)", res, err)
				}
			}
		})
	}
}

// An imported row was never unread on WhatsApp, and its synthetic ID must not
// anchor a history request.
func TestImportedIDsStayOffReceiptsAndHistoryRequests(t *testing.T) {
	s := newSendTestServer(t, &fakeSender{})
	rec := post(t, s.handleMarkRead, map[string]any{"chat_jid": "4915112345678@s.whatsapp.net", "message_ids": []string{"import-a", "import-b"}}, nil)
	if body := decode(t, rec); rec.Code != http.StatusOK || body["skipped"] != float64(2) {
		t.Errorf("mark read of imports only: %d %v", rec.Code, body)
	}
	if kept, skipped := withoutImportedIDs([]string{"3EB0A", "import-a", "3EB0B"}); len(kept) != 2 || skipped != 1 {
		t.Errorf("withoutImportedIDs = %v, %d", kept, skipped)
	}
	rec = post(t, s.handleRequestHistory, map[string]any{"chat_jid": "4915112345678@s.whatsapp.net", "oldest_msg_id": "import-a", "oldest_msg_timestamp": 1}, nil)
	if rec.Code != http.StatusUnprocessableEntity {
		t.Errorf("history request anchored on an import: %d %s", rec.Code, rec.Body)
	}
}

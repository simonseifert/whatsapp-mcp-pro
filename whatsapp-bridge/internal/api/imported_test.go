package api

import (
	"crypto/sha256"
	"net/http"
	"os"
	"path/filepath"
	"testing"

	"whatsapp-bridge/internal/mediastore"
)

const importedID = "import-0123456789abcdef01234567"

// Rows from a chat-export import carry no media key; the only copy of their
// media is the file the importer saved, hash-pinned by file_sha256.
func TestDownloadImportedMessage(t *testing.T) {
	data := []byte("jpeg bytes from an export")
	sum := sha256.Sum256(data)
	chat := "4915112345678@s.whatsapp.net"

	tests := []struct {
		name      string
		sha       []byte
		writeFile []byte
		wantCode  int
		wantVia   string
	}{
		{name: "saved copy served", sha: sum[:], writeFile: data, wantCode: http.StatusOK, wantVia: "saved"},
		{name: "copy missing", sha: sum[:], wantCode: http.StatusNotFound},
		{name: "copy does not match hash", sha: sum[:], writeFile: []byte("other"), wantCode: http.StatusNotFound},
		{name: "media not in export", sha: nil, wantCode: http.StatusNotFound},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Chdir(t.TempDir())
			s := newSendTestServer(t, &fakeSender{})
			db := s.messageStore.GetDB()
			if _, err := db.Exec(`INSERT INTO chats (jid, name) VALUES (?, 'Bob')`, chat); err != nil {
				t.Fatal(err)
			}
			if _, err := db.Exec(
				`INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename, file_sha256)
				 VALUES (?, ?, ?, '', '2020-10-23 09:41:00+02:00', 0, 'image', 'IMG-20201023-WA0001.jpg', ?)`,
				importedID, chat, chat, tt.sha,
			); err != nil {
				t.Fatal(err)
			}
			if tt.writeFile != nil {
				p, err := mediastore.Path(chat, importedID, "image", "IMG-20201023-WA0001.jpg")
				if err != nil {
					t.Fatal(err)
				}
				if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(p, tt.writeFile, 0o644); err != nil {
					t.Fatal(err)
				}
			}

			rec := post(t, s.handleDownload, map[string]string{"message_id": importedID, "chat_jid": chat}, nil)
			if rec.Code != tt.wantCode {
				t.Fatalf("status %d, want %d: %s", rec.Code, tt.wantCode, rec.Body)
			}
			if tt.wantVia != "" {
				if got := decode(t, rec)["via"]; got != tt.wantVia {
					t.Errorf("via = %v, want %s", got, tt.wantVia)
				}
			}
		})
	}
}

// A synthetic ID handed to WhatsApp attaches to nothing, so every endpoint
// that targets an existing message refuses it before reaching the sender.
func TestImportedMessageIsNeverATarget(t *testing.T) {
	chat := "4915112345678@s.whatsapp.net"
	tests := []struct {
		name string
		h    func(*Server) http.HandlerFunc
		body map[string]any
	}{
		{"reaction", func(s *Server) http.HandlerFunc { return s.handleReaction },
			map[string]any{"chat_jid": chat, "message_id": importedID, "emoji": "+1"}},
		{"edit", func(s *Server) http.HandlerFunc { return s.handleEditMessage },
			map[string]any{"chat_jid": chat, "message_id": importedID, "new_content": "x"}},
		{"delete", func(s *Server) http.HandlerFunc { return s.handleDeleteMessage },
			map[string]any{"chat_jid": chat, "message_id": importedID}},
		{"quote", func(s *Server) http.HandlerFunc { return s.handleSendMessage },
			map[string]any{"recipient": chat, "message": "re", "quoted_message_id": importedID}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			f := &fakeSender{}
			s := newSendTestServer(t, f)
			rec := post(t, tt.h(s), tt.body, nil)
			if rec.Code != http.StatusUnprocessableEntity {
				t.Fatalf("status %d, want 422: %s", rec.Code, rec.Body)
			}
			if f.callCount() != 0 {
				t.Errorf("sender called %d times", f.callCount())
			}
		})
	}
}

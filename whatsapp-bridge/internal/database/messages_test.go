package database

import (
	"database/sql"
	"errors"
	"os"
	"testing"
	"time"
)

// newTestStore opens an isolated SQLite file, runs the schema, and returns a
// MessageStore plus a cleanup func. Each test gets a fresh database.
func newTestStore(t *testing.T) (*MessageStore, func()) {
	t.Helper()
	f, err := os.CreateTemp("", "messages_test_*.db")
	if err != nil {
		t.Fatalf("temp db: %v", err)
	}
	path := f.Name()
	_ = f.Close()

	db, err := sql.Open("sqlite3", "file:"+path+"?_foreign_keys=on")
	if err != nil {
		os.Remove(path)
		t.Fatalf("open db: %v", err)
	}
	if err := createTables(db); err != nil {
		db.Close()
		os.Remove(path)
		t.Fatalf("create tables: %v", err)
	}
	if err := runMigrations(db); err != nil {
		db.Close()
		os.Remove(path)
		t.Fatalf("migrations: %v", err)
	}

	store := &MessageStore{db: db}
	return store, func() {
		db.Close()
		os.Remove(path)
	}
}

func TestStoreAndGetMessageMedia(t *testing.T) {
	store, cleanup := newTestStore(t)
	defer cleanup()

	const (
		id        = "ABCDEF1234567890"
		chatJID   = "1234567890@s.whatsapp.net"
		mediaType = "image"
		filename  = "image_test.jpg"
		mediaURL  = "https://mmg.whatsapp.net/v/t62.7118-24/example.enc"
		direct    = "/v/t62.7118-24/example.enc"
	)
	mediaKey := []byte{0x01, 0x02, 0x03}
	fileSHA := []byte{0xaa, 0xbb}
	fileEnc := []byte{0xcc, 0xdd}

	// messages.chat_jid has FK to chats.jid, so seed the chat row first.
	if err := store.StoreChat(chatJID, "Test Chat", time.Unix(1700000000, 0)); err != nil {
		t.Fatalf("StoreChat: %v", err)
	}

	err := store.StoreMessage(
		id, chatJID, "sender@s.whatsapp.net", "Sender Name", "",
		time.Unix(1700000000, 0), false,
		mediaType, filename, mediaURL, direct,
		mediaKey, fileSHA, fileEnc, 12345,
	)
	if err != nil {
		t.Fatalf("StoreMessage: %v", err)
	}

	gotType, gotName, gotURL, gotDirect, gotKey, gotSHA, gotEnc, gotLen, err := store.GetMessageMedia(id, chatJID)
	if err != nil {
		t.Fatalf("GetMessageMedia: %v", err)
	}
	if gotType != mediaType || gotName != filename || gotURL != mediaURL || gotDirect != direct {
		t.Errorf("string fields mismatch: got (%q,%q,%q,%q)", gotType, gotName, gotURL, gotDirect)
	}
	if string(gotKey) != string(mediaKey) || string(gotSHA) != string(fileSHA) || string(gotEnc) != string(fileEnc) {
		t.Errorf("blob fields mismatch")
	}
	if gotLen != 12345 {
		t.Errorf("file_length: got %d, want 12345", gotLen)
	}
}

func TestGetMessageMediaNotFound(t *testing.T) {
	store, cleanup := newTestStore(t)
	defer cleanup()

	_, _, _, _, _, _, _, _, err := store.GetMessageMedia("nope", "nope@s.whatsapp.net")
	if !errors.Is(err, sql.ErrNoRows) {
		t.Fatalf("expected sql.ErrNoRows, got %v", err)
	}
}

// TestDirectPathColumnMigratesOnExistingDB simulates a database written before
// direct_path existed: createTables alone (no migration), then a row inserted
// with the legacy column set, then runMigrations adds the column and reads
// should return empty string for the new field instead of failing.
func TestDirectPathColumnMigratesOnExistingDB(t *testing.T) {
	f, err := os.CreateTemp("", "messages_legacy_*.db")
	if err != nil {
		t.Fatalf("temp db: %v", err)
	}
	path := f.Name()
	_ = f.Close()
	defer os.Remove(path)

	db, err := sql.Open("sqlite3", "file:"+path+"?_foreign_keys=on")
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()

	// Simulate a pre-migration schema: create tables WITHOUT direct_path.
	_, err = db.Exec(`
		CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP);
		CREATE TABLE messages (
			id TEXT, chat_jid TEXT, sender TEXT, sender_name TEXT, content TEXT,
			timestamp TIMESTAMP, is_from_me BOOLEAN, media_type TEXT, filename TEXT,
			url TEXT, media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB,
			file_length INTEGER, PRIMARY KEY (id, chat_jid)
		);
	`)
	if err != nil {
		t.Fatalf("legacy schema: %v", err)
	}
	_, err = db.Exec(
		`INSERT INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me,
		 media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length)
		 VALUES ('legacy', 'jid@s.whatsapp.net', 's', 's', '', ?, 0, 'image', 'old.jpg', 'http://x', NULL, NULL, NULL, 1)`,
		time.Unix(1700000000, 0),
	)
	if err != nil {
		t.Fatalf("legacy insert: %v", err)
	}

	if err := runMigrations(db); err != nil {
		t.Fatalf("runMigrations: %v", err)
	}

	store := &MessageStore{db: db}
	gotType, _, gotURL, gotDirect, _, _, _, _, err := store.GetMessageMedia("legacy", "jid@s.whatsapp.net")
	if err != nil {
		t.Fatalf("GetMessageMedia: %v", err)
	}
	if gotType != "image" || gotURL != "http://x" {
		t.Errorf("unexpected fields: type=%q url=%q", gotType, gotURL)
	}
	if gotDirect != "" {
		t.Errorf("direct_path on legacy row should be empty, got %q", gotDirect)
	}
}

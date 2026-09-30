package database

import (
	"database/sql"
	"errors"
	"testing"
	"time"

	_ "github.com/mattn/go-sqlite3"
)

func newTestMessageStore(t *testing.T) *MessageStore {
	t.Helper()

	db, err := sql.Open("sqlite3", "file:"+t.Name()+"?mode=memory&cache=shared&_foreign_keys=on")
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })

	if err := createTables(db); err != nil {
		t.Fatalf("create tables: %v", err)
	}
	if err := createIdentityTable(db); err != nil {
		t.Fatalf("create identity table: %v", err)
	}
	if err := runMigrations(db); err != nil {
		t.Fatalf("run migrations: %v", err)
	}

	return &MessageStore{db: db}
}

func TestStoreChatKeepsNewestTimestamp(t *testing.T) {
	store := newTestMessageStore(t)
	older := time.Date(2026, 5, 15, 10, 0, 0, 0, time.UTC)
	newer := older.Add(2 * time.Hour)

	if err := store.StoreChat("123@s.whatsapp.net", "Alice", newer); err != nil {
		t.Fatalf("store newer chat: %v", err)
	}
	if err := store.StoreChat("123@s.whatsapp.net", "Alice Old", older); err != nil {
		t.Fatalf("store older chat: %v", err)
	}

	var name string
	var got time.Time
	if err := store.db.QueryRow("SELECT name, last_message_time FROM chats WHERE jid = ?", "123@s.whatsapp.net").Scan(&name, &got); err != nil {
		t.Fatalf("read chat: %v", err)
	}
	if !got.Equal(newer) {
		t.Fatalf("last_message_time = %v, want %v", got, newer)
	}
	if name != "Alice Old" {
		t.Fatalf("name = %q, want latest non-empty name", name)
	}
}

func TestStoreChatEmptyNameDoesNotEraseExistingName(t *testing.T) {
	store := newTestMessageStore(t)
	now := time.Date(2026, 5, 15, 10, 0, 0, 0, time.UTC)

	if err := store.StoreChat("123@s.whatsapp.net", "Alice", now); err != nil {
		t.Fatalf("store named chat: %v", err)
	}
	if err := store.StoreChat("123@s.whatsapp.net", "", now.Add(time.Hour)); err != nil {
		t.Fatalf("store empty-name chat: %v", err)
	}

	var name string
	if err := store.db.QueryRow("SELECT name FROM chats WHERE jid = ?", "123@s.whatsapp.net").Scan(&name); err != nil {
		t.Fatalf("read chat: %v", err)
	}
	if name != "Alice" {
		t.Fatalf("name = %q, want existing name", name)
	}
}

func TestOldestMessage(t *testing.T) {
	store := newTestMessageStore(t)
	chat := "120363@g.us"
	base := time.Date(2025, 10, 1, 9, 0, 0, 0, time.UTC)
	if err := store.StoreChat(chat, "FIDIT", base); err != nil {
		t.Fatal(err)
	}
	for i, id := range []string{"B", "A", "C"} {
		ts := base.Add(time.Duration(i-1) * time.Hour) // A is the oldest
		if _, err := store.GetDB().Exec(
			"INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, ?, 'x', ?, 0)",
			id, chat, "38591"+id, ts); err != nil {
			t.Fatal(err)
		}
	}

	id, sender, fromMe, ts, err := store.OldestMessage(chat)
	if err != nil {
		t.Fatal(err)
	}
	if id != "B" || sender != "38591B" || fromMe || !ts.Equal(base.Add(-time.Hour)) {
		t.Errorf("got id=%s sender=%s fromMe=%v ts=%v", id, sender, fromMe, ts)
	}

	if _, _, _, _, err := store.OldestMessage("nobody@s.whatsapp.net"); !errors.Is(err, sql.ErrNoRows) {
		t.Errorf("empty chat: got %v, want sql.ErrNoRows", err)
	}
}

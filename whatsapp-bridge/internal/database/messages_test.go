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

	// Imported history is older still, but the phone cannot find its
	// synthetic IDs, so it must never become a history-request anchor.
	if _, err := store.GetDB().Exec(
		"INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, 'x', 'x', ?, 0)",
		ImportedIDPrefix+"abc", chat, base.Add(-24*time.Hour)); err != nil {
		t.Fatal(err)
	}
	if id, _, _, _, err := store.OldestMessage(chat); err != nil || id != "B" {
		t.Errorf("with an imported row: got id=%s err=%v, want B", id, err)
	}
}

func TestStoreMessageContext(t *testing.T) {
	store := newTestMessageStore(t)
	ts := time.Date(2026, 10, 13, 19, 43, 0, 0, time.UTC)
	if err := store.StoreChat("g@g.us", "Friday", ts); err != nil {
		t.Fatalf("store chat: %v", err)
	}
	if err := store.StoreMessage("M1", "g@g.us", "me", "Simon", "analyze this", ts, true, "", "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatalf("store: %v", err)
	}
	if err := store.StoreMessageContext("M1", "g@g.us", "Q1", "123@s.whatsapp.net", "https://x.test/reel", true); err != nil {
		t.Fatalf("store context: %v", err)
	}
	var qid, qtext, reply string
	var fwd bool
	if err := store.db.QueryRow("SELECT quoted_message_id, quoted_text_preview, reply_to_message_id, is_forwarded FROM messages WHERE id = 'M1'").Scan(&qid, &qtext, &reply, &fwd); err != nil {
		t.Fatalf("read: %v", err)
	}
	if qid != "Q1" || qtext != "https://x.test/reel" || reply != "Q1" || !fwd {
		t.Fatalf("got qid=%q qtext=%q reply=%q fwd=%v", qid, qtext, reply, fwd)
	}
}

func TestStoreMessageRedeliveryKeepsLaterWrites(t *testing.T) {
	store := newTestMessageStore(t)
	chat := "123@s.whatsapp.net"
	ts := time.Date(2026, 10, 1, 9, 0, 0, 0, time.UTC)
	if err := store.StoreChat(chat, "Alice", ts); err != nil {
		t.Fatalf("store chat: %v", err)
	}
	store1 := func() {
		t.Helper()
		if err := store.StoreMessage("v1", chat, "123", "Alice", "", ts, false,
			"audio", "audio_1.ogg", "https://mmg/old", "/v/old", []byte("k"), []byte("s"), []byte("e"), 42); err != nil {
			t.Fatalf("store voice note: %v", err)
		}
	}
	store1()
	var rowid int64
	if err := store.db.QueryRow("SELECT rowid FROM messages WHERE id = 'v1'").Scan(&rowid); err != nil {
		t.Fatalf("read rowid: %v", err)
	}

	// Written after the original insert: a transcript, a refreshed direct_path, reply context.
	if _, err := store.db.Exec("UPDATE messages SET content = 'hello', direct_path = '/v/fresh' WHERE id = 'v1'"); err != nil {
		t.Fatalf("simulate later writes: %v", err)
	}
	if err := store.StoreMessageContext("v1", chat, "q1", "Bob", "earlier", false); err != nil {
		t.Fatalf("store context: %v", err)
	}

	store1() // history sync / reconnect backfill re-delivers the same message

	var content, directPath, quoted string
	var gotRowid int64
	if err := store.db.QueryRow(
		"SELECT rowid, content, direct_path, COALESCE(quoted_message_id,'') FROM messages WHERE id = 'v1'",
	).Scan(&gotRowid, &content, &directPath, &quoted); err != nil {
		t.Fatalf("read message: %v", err)
	}
	if content != "hello" || directPath != "/v/fresh" || quoted != "q1" {
		t.Fatalf("re-delivery clobbered data: content=%q direct_path=%q quoted=%q", content, directPath, quoted)
	}
	if gotRowid != rowid {
		t.Fatalf("rowid changed %d -> %d (recall index keys on rowid)", rowid, gotRowid)
	}
}

func TestStoreMessageNewTextOverwrites(t *testing.T) {
	store := newTestMessageStore(t)
	chat := "123@s.whatsapp.net"
	ts := time.Date(2026, 10, 1, 9, 0, 0, 0, time.UTC)
	if err := store.StoreChat(chat, "Alice", ts); err != nil {
		t.Fatalf("store chat: %v", err)
	}
	for _, text := range []string{"first", "edited"} {
		if err := store.StoreMessage("m1", chat, "123", "Alice", text, ts, false, "", "", "", "", nil, nil, nil, 0); err != nil {
			t.Fatalf("store %q: %v", text, err)
		}
	}
	var content string
	if err := store.db.QueryRow("SELECT content FROM messages WHERE id = 'm1'").Scan(&content); err != nil {
		t.Fatalf("read: %v", err)
	}
	if content != "edited" {
		t.Fatalf("content = %q, want %q", content, "edited")
	}
}

// The reconnect backfill anchors on the newest message; an imported row's
// synthetic ID would anchor it on nothing.
func TestNewestMessageSkipsImportedRows(t *testing.T) {
	store := newTestMessageStore(t)
	chat := "4915112345678@s.whatsapp.net"
	at := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	if err := store.StoreChat(chat, "Max", at); err != nil {
		t.Fatal(err)
	}
	storeTestMessage(t, store, "REAL", chat, "hi", at)
	storeTestMessage(t, store, ImportedIDPrefix+"abc", chat, "from the export", at.Add(time.Hour))
	id, _, _, _, err := store.NewestMessage(chat)
	if err != nil || id != "REAL" {
		t.Errorf("NewestMessage = %q, %v; want REAL", id, err)
	}
}

func TestGetPreviousMessageTimeAcrossTheDSTChange(t *testing.T) {
	store := newTestMessageStore(t)
	chat := "123@s.whatsapp.net"
	summer := time.FixedZone("CEST", 2*3600)
	winter := time.FixedZone("CET", 3600)
	// 25 Oct 2026, Europe/Zagreb: 03:00 CEST becomes 02:00 CET, so 02:30 occurs twice.
	first := time.Date(2026, 10, 25, 2, 30, 0, 0, summer)   // 00:30 UTC
	second := time.Date(2026, 10, 25, 2, 10, 0, 0, winter)  // 01:10 UTC, smaller as text
	current := time.Date(2026, 10, 25, 2, 30, 0, 0, winter) // 01:30 UTC

	if err := store.StoreChat(chat, "Alice", current); err != nil {
		t.Fatal(err)
	}
	for id, ts := range map[string]time.Time{"A": first, "B": second} {
		if _, err := store.db.Exec(
			`INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, ?, '', ?, 0)`,
			id, chat, chat, ts,
		); err != nil {
			t.Fatal(err)
		}
	}

	got, err := store.GetPreviousMessageTime(chat, current)
	if err != nil {
		t.Fatal(err)
	}
	if !got.Equal(second) {
		t.Fatalf("previous = %v, want %v", got.UTC(), second.UTC())
	}
	got, err = store.GetPreviousMessageTime(chat, second)
	if err != nil {
		t.Fatal(err)
	}
	if !got.Equal(first) {
		t.Fatalf("previous of 02:10+01:00 = %v, want %v", got.UTC(), first.UTC())
	}
}

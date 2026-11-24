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

func TestRenameChatReplacesTheStoredGroupName(t *testing.T) {
	store := newTestMessageStore(t)
	now := time.Date(2026, 11, 11, 12, 0, 0, 0, time.UTC)

	if err := store.StoreChat("120363000000000001@g.us", "Old Group Name", now); err != nil {
		t.Fatalf("store chat: %v", err)
	}
	if err := store.RenameChat("120363000000000001@g.us", "New Group Name"); err != nil {
		t.Fatalf("rename chat: %v", err)
	}

	var name string
	var got time.Time
	if err := store.db.QueryRow("SELECT name, last_message_time FROM chats WHERE jid = ?", "120363000000000001@g.us").Scan(&name, &got); err != nil {
		t.Fatalf("read chat: %v", err)
	}
	if name != "New Group Name" {
		t.Fatalf("name = %q, want the new group name", name)
	}
	if !got.Equal(now) {
		t.Fatalf("last_message_time = %v, want it unchanged at %v", got, now)
	}
}

func TestRenameChatOfAnUnknownGroupCreatesNoChat(t *testing.T) {
	store := newTestMessageStore(t)

	if err := store.RenameChat("999@g.us", "Never Seen"); err != nil {
		t.Fatalf("rename chat: %v", err)
	}

	var count int
	if err := store.db.QueryRow("SELECT COUNT(*) FROM chats").Scan(&count); err != nil {
		t.Fatalf("count chats: %v", err)
	}
	if count != 0 {
		t.Fatalf("chats = %d, want 0", count)
	}
}

// An attacker reuses Simon's message ID in the same chat. The stored row must
// stay Simon's, and the attacker's follow-up edit and revoke must be refused
// (review-bridge #1).
func TestStoreMessageIDReuseKeepsOriginalAuthor(t *testing.T) {
	store := newTestMessageStore(t)
	const chat = "120363000000000000@g.us"
	const simon = "4915100000000@s.whatsapp.net"
	const mallory = "4915199999999@s.whatsapp.net"
	at := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
	if err := store.StoreChat(chat, "Group", at); err != nil {
		t.Fatalf("store chat: %v", err)
	}

	if err := store.StoreMessage("ABC123", chat, simon, "Simon", "I owe nobody anything", at, true, "", "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatalf("store original: %v", err)
	}
	err := store.StoreMessage("ABC123", chat, mallory, "Mallory", "I owe Mallory 5000 EUR", at.Add(time.Minute), false, "", "", "", "", nil, nil, nil, 0)
	if !errors.Is(err, ErrMessageIDReuse) {
		t.Fatalf("colliding store err = %v, want ErrMessageIDReuse", err)
	}

	var sender, content string
	var fromMe bool
	if err := store.db.QueryRow(`SELECT sender, content, is_from_me FROM messages WHERE id = 'ABC123' AND chat_jid = ?`, chat).
		Scan(&sender, &content, &fromMe); err != nil {
		t.Fatalf("read row: %v", err)
	}
	if sender != simon || content != "I owe nobody anything" || !fromMe {
		t.Fatalf("row changed: sender=%q content=%q from_me=%t", sender, content, fromMe)
	}

	res, err := store.ApplyEdit(Edit{
		MessageID: "ABC123", ChatJIDs: []string{chat}, NewContent: "forged edit", EditedAt: at.Add(2 * time.Minute),
		Editor: Actor{JID: mallory}, StanzaID: "EDIT1", Source: SourceLive,
	})
	if err != nil {
		t.Fatalf("apply edit: %v", err)
	}
	if res.Applied || !res.Rejected {
		t.Fatalf("attacker edit: applied=%t rejected=%t, want rejected", res.Applied, res.Rejected)
	}
	rev, err := store.MarkDeleted(Revoke{MessageID: "ABC123", ChatJIDs: []string{chat}, At: at.Add(3 * time.Minute), By: Actor{JID: mallory}})
	if err != nil {
		t.Fatalf("mark deleted: %v", err)
	}
	if !rev.Rejected {
		t.Fatalf("attacker revoke was not rejected: %+v", rev)
	}
}

func TestStoreMessageIDReuseCases(t *testing.T) {
	const chat = "120363000000000000@g.us"
	tests := []struct {
		name         string
		firstSender  string
		firstFromMe  bool
		secondSender string
		secondFromMe bool
		wantReuse    bool
		wantContent  string
	}{
		{name: "same sender redelivery upserts", firstSender: "111@s.whatsapp.net", secondSender: "111@s.whatsapp.net", wantContent: "second"},
		{name: "own device history resync upserts", firstSender: "999@s.whatsapp.net", firstFromMe: true, secondSender: "999@s.whatsapp.net", secondFromMe: true, wantContent: "second"},
		{name: "other sender rejected", firstSender: "111@s.whatsapp.net", secondSender: "222@s.whatsapp.net", wantReuse: true, wantContent: "first"},
		{name: "attacker claims from-me flag rejected", firstSender: "111@s.whatsapp.net", secondSender: "999@s.whatsapp.net", secondFromMe: true, wantReuse: true, wantContent: "first"},
		{name: "attacker takes over own message rejected", firstSender: "999@s.whatsapp.net", firstFromMe: true, secondSender: "222@s.whatsapp.net", wantReuse: true, wantContent: "first"},
		{name: "legacy bare sender matches full JID", firstSender: "111", secondSender: "111@s.whatsapp.net", wantContent: "second"},
		{name: "empty incoming sender must not replace another sender's text", firstSender: "111@s.whatsapp.net", secondSender: "", wantReuse: true, wantContent: "first"},
		{name: "own message with empty sender stays protected", firstSender: "", firstFromMe: true, secondSender: "222@s.whatsapp.net", wantReuse: true, wantContent: "first"},
		{name: "foreign row with empty sender but content is not claimable", firstSender: "", secondSender: "222@s.whatsapp.net", wantReuse: true, wantContent: "first"},
	}
	for i, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			store := newTestMessageStore(t)
			id := "ID" + string(rune('A'+i))
			at := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
			if err := store.StoreChat(chat, "Group", at); err != nil {
				t.Fatalf("store chat: %v", err)
			}
			// Insert directly so a NULL/empty sender or a legacy bare one can be set up.
			if _, err := store.db.Exec(
				`INSERT INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me) VALUES (?, ?, ?, ?, 'first', ?, ?)`,
				id, chat, tc.firstSender, tc.firstSender, at, tc.firstFromMe); err != nil {
				t.Fatalf("seed: %v", err)
			}
			err := store.StoreMessage(id, chat, tc.secondSender, "n", "second", at.Add(time.Minute), tc.secondFromMe, "", "", "", "", nil, nil, nil, 0)
			if got := errors.Is(err, ErrMessageIDReuse); got != tc.wantReuse {
				t.Fatalf("reuse = %t (err %v), want %t", got, err, tc.wantReuse)
			}
			if err != nil && !tc.wantReuse {
				t.Fatalf("unexpected error: %v", err)
			}
			var content string
			if err := store.db.QueryRow(`SELECT content FROM messages WHERE id = ? AND chat_jid = ?`, id, chat).Scan(&content); err != nil {
				t.Fatalf("read: %v", err)
			}
			if content != tc.wantContent {
				t.Fatalf("content = %q, want %q", content, tc.wantContent)
			}
		})
	}
}

// A true stand-in (no sender, not ours, no content) is what a revoke that
// arrived first leaves behind; the real message may fill it.
func TestStoreMessageFillsStandIn(t *testing.T) {
	store := newTestMessageStore(t)
	const chat = "120363000000000000@g.us"
	at := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
	if err := store.StoreChat(chat, "Group", at); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec(`INSERT INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me) VALUES ('S1', ?, '', '', '', ?, 0)`, chat, at); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreMessage("S1", chat, "111@s.whatsapp.net", "n", "real", at, false, "", "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatalf("stand-in should be fillable: %v", err)
	}
}

// If the stored author cannot be read, the overwrite is refused, not allowed.
func TestStoreMessageFailsClosedOnLookupError(t *testing.T) {
	store := newTestMessageStore(t)
	if err := store.db.Close(); err != nil {
		t.Fatal(err)
	}
	err := store.StoreMessage("X1", "c@g.us", "1@s.whatsapp.net", "n", "text", time.Now(), false, "", "", "", "", nil, nil, nil, 0)
	if err == nil {
		t.Fatal("store succeeded although the author lookup failed")
	}
	if !store.MessageIDReused("X1", "c@g.us", "1@s.whatsapp.net", false) {
		t.Fatal("MessageIDReused must report a conflict when the lookup fails")
	}
}

// Our own copy of a message may fill the stand-in an untrusted revoke left.
func TestStoreMessageOwnCopyFillsStandIn(t *testing.T) {
	store := newTestMessageStore(t)
	const chat = "120363000000000000@g.us"
	at := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
	if err := store.StoreChat(chat, "Group", at); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec(`INSERT INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me) VALUES ('S2', ?, '', '', '', ?, 0)`, chat, at); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreMessage("S2", chat, "999@s.whatsapp.net", "Simon", "mine", at, true, "", "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatalf("own copy should fill the stand-in: %v", err)
	}
}

// The same person arrives as a LID and as a phone JID with no identities row:
// the event's SenderAlt (or the LID map) links them, so the echo upserts. With
// no link at all it is refused but reported as a reuse, never an error.
func TestStoreMessagePhoneLIDEcho(t *testing.T) {
	const chat = "120363000000000000@g.us"
	const lid, phone = "123456789012345@lid", "4915112345678@s.whatsapp.net"
	tests := []struct {
		name       string
		stored     string
		incoming   string
		forms      []string
		wantReuse  bool
		wantAuthor string
	}{
		{"LID stored, phone arrives with SenderAlt", lid, phone, []string{lid}, false, phone},
		{"phone stored, LID arrives with the phone as alt", phone, lid, []string{phone}, false, lid},
		{"no link anywhere is refused, row kept", lid, phone, nil, true, lid},
		{"unrelated alt does not link", lid, phone, []string{"4915100000099@s.whatsapp.net"}, true, lid},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			store := newTestMessageStore(t)
			at := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
			if err := store.StoreChat(chat, "Group", at); err != nil {
				t.Fatal(err)
			}
			if err := store.StoreMessage("E1", chat, tt.stored, "n", "first", at, false, "", "", "", "", nil, nil, nil, 0); err != nil {
				t.Fatal(err)
			}
			if got := store.MessageIDReused("E1", chat, tt.incoming, false, tt.forms...); got != tt.wantReuse {
				t.Fatalf("MessageIDReused = %v, want %v", got, tt.wantReuse)
			}
			err := store.StoreMessageForms(tt.forms, "E1", chat, tt.incoming, "n", "second", at, false, "", "", "", "", nil, nil, nil, 0)
			if got := errors.Is(err, ErrMessageIDReuse); got != tt.wantReuse || (err != nil && !tt.wantReuse) {
				t.Fatalf("store err = %v, want reuse=%v", err, tt.wantReuse)
			}
			var sender string
			if err := store.db.QueryRow(`SELECT sender FROM messages WHERE id='E1' AND chat_jid=?`, chat).Scan(&sender); err != nil || sender != tt.wantAuthor {
				t.Fatalf("stored sender = %q (%v), want %q", sender, err, tt.wantAuthor)
			}
		})
	}
}

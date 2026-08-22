package database

import (
	"database/sql"
	"fmt"
	"testing"
	"time"

	_ "github.com/mattn/go-sqlite3"
)

func storeTestMessage(t *testing.T, store *MessageStore, id, chatJID, content string, ts time.Time) {
	t.Helper()
	if err := store.StoreMessage(id, chatJID, "sender", "Sender", content, ts, false, "", "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatalf("store message %s in %s: %v", id, chatJID, err)
	}
}

func TestResolveDisplayName(t *testing.T) {
	tests := []struct {
		name     string
		identity Identity
		want     string
	}{
		{
			name:     "nickname overrides everything",
			identity: Identity{JID: "1@s.whatsapp.net", Nickname: "Boss", FullName: "Jane Doe", PushName: "jd"},
			want:     "Boss",
		},
		{
			name:     "address book name beats self-chosen push name",
			identity: Identity{JID: "1@s.whatsapp.net", FullName: "Jane Doe", PushName: "jd"},
			want:     "Jane Doe",
		},
		{
			name:     "push name is better than a bare number",
			identity: Identity{JID: "1@s.whatsapp.net", PushName: "jd", Phone: "1"},
			want:     "jd",
		},
		{
			name:     "falls back to the phone number",
			identity: Identity{JID: "1@s.whatsapp.net", Phone: "441234"},
			want:     "441234",
		},
		{
			name:     "whitespace-only names do not count",
			identity: Identity{JID: "1@s.whatsapp.net", FullName: "   ", Phone: "441234"},
			want:     "441234",
		},
		{
			name:     "last resort is the jid",
			identity: Identity{JID: "1@lid"},
			want:     "1@lid",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := tt.identity.ResolveDisplayName(); got != tt.want {
				t.Errorf("ResolveDisplayName() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestUpsertIdentitiesIsIdempotent(t *testing.T) {
	store := newTestMessageStore(t)
	row := Identity{
		JID: "441234@s.whatsapp.net", LID: "999@lid", PhoneJID: "441234@s.whatsapp.net",
		Phone: "441234", Kind: "user", FullName: "Jane Doe", MessageCount: 3,
		Sources: []string{"contacts", "chats"},
	}

	for i := 0; i < 2; i++ {
		if err := store.UpsertIdentities([]Identity{row}); err != nil {
			t.Fatalf("upsert %d: %v", i, err)
		}
	}

	var count int
	var displayName, sources string
	if err := store.db.QueryRow("SELECT COUNT(*) FROM identities").Scan(&count); err != nil {
		t.Fatalf("count identities: %v", err)
	}
	if count != 1 {
		t.Errorf("row count = %d, want 1", count)
	}
	if err := store.db.QueryRow("SELECT display_name, sources FROM identities WHERE jid = ?", row.JID).Scan(&displayName, &sources); err != nil {
		t.Fatalf("read identity: %v", err)
	}
	if displayName != "Jane Doe" {
		t.Errorf("display_name = %q, want %q", displayName, "Jane Doe")
	}
	if sources != "contacts,chats" {
		t.Errorf("sources = %q, want %q", sources, "contacts,chats")
	}
}

func TestPruneIdentitiesKeepsOnlyCurrentRows(t *testing.T) {
	store := newTestMessageStore(t)
	rows := []Identity{
		{JID: "1@s.whatsapp.net", Kind: "user"},
		{JID: "2@s.whatsapp.net", Kind: "user"},
	}
	if err := store.UpsertIdentities(rows); err != nil {
		t.Fatalf("upsert: %v", err)
	}

	pruned, err := store.PruneIdentities([]string{"1@s.whatsapp.net"})
	if err != nil {
		t.Fatalf("prune: %v", err)
	}
	if pruned != 1 {
		t.Errorf("pruned = %d, want 1", pruned)
	}

	// An empty keep list means the sync produced nothing, which is far more
	// likely to be a failure than a genuinely empty address book — pruning on
	// it would wipe the directory.
	pruned, err = store.PruneIdentities(nil)
	if err != nil {
		t.Fatalf("prune with empty keep list: %v", err)
	}
	if pruned != 0 {
		t.Errorf("pruned = %d on empty keep list, want 0", pruned)
	}
}

func TestMergeChatMovesMessagesAndLeavesBreadcrumb(t *testing.T) {
	store := newTestMessageStore(t)
	lid := "999@lid"
	phone := "441234@s.whatsapp.net"
	earlier := time.Date(2026, 5, 15, 10, 0, 0, 0, time.UTC)
	later := earlier.Add(time.Hour)

	if err := store.StoreChat(lid, "Jane", earlier); err != nil {
		t.Fatalf("store lid chat: %v", err)
	}
	if err := store.StoreChat(phone, "Jane Doe", later); err != nil {
		t.Fatalf("store phone chat: %v", err)
	}
	storeTestMessage(t, store, "a", lid, "from the lid side", earlier)
	storeTestMessage(t, store, "b", phone, "from the phone side", later)
	// Same message ID under both identities — WhatsApp delivered it twice.
	storeTestMessage(t, store, "dupe", lid, "delivered twice", earlier)
	storeTestMessage(t, store, "dupe", phone, "delivered twice", earlier)

	result, err := store.MergeChat(lid, phone)
	if err != nil {
		t.Fatalf("merge: %v", err)
	}
	if result.MessagesMoved != 1 {
		t.Errorf("MessagesMoved = %d, want 1", result.MessagesMoved)
	}
	if result.MessagesDuplicate != 1 {
		t.Errorf("MessagesDuplicate = %d, want 1", result.MessagesDuplicate)
	}

	var inTarget, leftBehind int
	if err := store.db.QueryRow("SELECT COUNT(*) FROM messages WHERE chat_jid = ?", phone).Scan(&inTarget); err != nil {
		t.Fatalf("count target messages: %v", err)
	}
	if inTarget != 3 {
		t.Errorf("messages under %s = %d, want 3", phone, inTarget)
	}
	if err := store.db.QueryRow("SELECT COUNT(*) FROM messages WHERE chat_jid = ?", lid).Scan(&leftBehind); err != nil {
		t.Fatalf("count source messages: %v", err)
	}
	if leftBehind != 0 {
		t.Errorf("messages left under %s = %d, want 0", lid, leftBehind)
	}

	var mergedInto string
	if err := store.db.QueryRow("SELECT merged_into FROM chats WHERE jid = ?", lid).Scan(&mergedInto); err != nil {
		t.Fatalf("read merged_into: %v", err)
	}
	if mergedInto != phone {
		t.Errorf("merged_into = %q, want %q", mergedInto, phone)
	}

	// Merging again must be a no-op, not a second round of moves.
	second, err := store.MergeChat(lid, phone)
	if err != nil {
		t.Fatalf("second merge: %v", err)
	}
	if second.MessagesMoved != 0 || second.MessagesDuplicate != 0 {
		t.Errorf("second merge moved %d / dropped %d, want 0 / 0", second.MessagesMoved, second.MessagesDuplicate)
	}
}

func TestMergeChatCreatesMissingTarget(t *testing.T) {
	store := newTestMessageStore(t)
	lid := "999@lid"
	phone := "441234@s.whatsapp.net"
	ts := time.Date(2026, 5, 15, 10, 0, 0, 0, time.UTC)

	if err := store.StoreChat(lid, "Jane", ts); err != nil {
		t.Fatalf("store lid chat: %v", err)
	}
	storeTestMessage(t, store, "a", lid, "only conversation", ts)

	result, err := store.MergeChat(lid, phone)
	if err != nil {
		t.Fatalf("merge: %v", err)
	}
	if !result.TargetChatCreated {
		t.Error("TargetChatCreated = false, want true")
	}

	var name string
	if err := store.db.QueryRow("SELECT name FROM chats WHERE jid = ?", phone).Scan(&name); err != nil {
		t.Fatalf("read created chat: %v", err)
	}
	if name != "Jane" {
		t.Errorf("name = %q, want %q — the target should inherit the source's name", name, "Jane")
	}
}

func TestMergeChatRejectsDegenerateArguments(t *testing.T) {
	store := newTestMessageStore(t)
	for _, tt := range []struct{ from, to string }{
		{"1@lid", "1@lid"},
		{"", "1@s.whatsapp.net"},
		{"1@lid", ""},
	} {
		if _, err := store.MergeChat(tt.from, tt.to); err != nil {
			t.Errorf("MergeChat(%q, %q) returned error %v, want silent no-op", tt.from, tt.to, err)
		}
	}
}

func TestPruneIdentitiesHandlesLargeKeepLists(t *testing.T) {
	// One bound parameter per kept identity would hit SQLite's per-statement
	// parameter cap on a big address book; this is the regression guard.
	store := newTestMessageStore(t)
	rows := make([]Identity, 0, 2000)
	keep := make([]string, 0, 2000)
	for i := 0; i < 2000; i++ {
		jid := fmt.Sprintf("%d@s.whatsapp.net", i)
		rows = append(rows, Identity{JID: jid, Kind: "user"})
		if i%2 == 0 {
			keep = append(keep, jid)
		}
	}
	if err := store.UpsertIdentities(rows); err != nil {
		t.Fatalf("upsert: %v", err)
	}

	pruned, err := store.PruneIdentities(keep)
	if err != nil {
		t.Fatalf("prune: %v", err)
	}
	if pruned != 1000 {
		t.Errorf("pruned = %d, want 1000", pruned)
	}

	var remaining int
	if err := store.db.QueryRow("SELECT COUNT(*) FROM identities").Scan(&remaining); err != nil {
		t.Fatalf("count identities: %v", err)
	}
	if remaining != 1000 {
		t.Errorf("remaining = %d, want 1000", remaining)
	}
}

func TestGetChatStatsParsesStoredTimestampForms(t *testing.T) {
	// GetChatStats reads MIN/MAX(timestamp), which SQLite returns as raw strings
	// (aggregates drop the column's TIMESTAMP affinity). Both writers' formats
	// must parse: the Go bridge's "space + numeric offset" and Python isoformat.
	store := newTestMessageStore(t)
	chat := "441234@s.whatsapp.net"
	if err := store.StoreChat(chat, "Jane", time.Now()); err != nil {
		t.Fatalf("store chat: %v", err)
	}
	// Insert timestamps in the exact on-disk string forms, bypassing StoreMessage
	// so the test pins the parser, not Go's time formatting.
	rows := []struct{ id, ts string }{
		{"a", "2023-07-15 11:15:58+02:00"},        // bridge form, earliest
		{"b", "2026-08-21 14:03:48+02:00"},        // bridge form, latest
		{"c", "2025-01-02T09:00:00.123456+02:00"}, // python isoformat with micros
	}
	for _, r := range rows {
		if _, err := store.db.Exec(
			"INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, 's', 'x', ?, 0)",
			r.id, chat, r.ts,
		); err != nil {
			t.Fatalf("insert %s: %v", r.id, err)
		}
	}

	stats, err := store.GetChatStats()
	if err != nil {
		t.Fatalf("GetChatStats: %v", err)
	}
	got, ok := stats[chat]
	if !ok {
		t.Fatalf("no stats for %s", chat)
	}
	if got.Count != 3 {
		t.Errorf("Count = %d, want 3", got.Count)
	}
	if got.FirstSeen == nil || got.FirstSeen.Year() != 2023 {
		t.Errorf("FirstSeen = %v, want a 2023 time", got.FirstSeen)
	}
	if got.LastSeen == nil || got.LastSeen.Year() != 2026 {
		t.Errorf("LastSeen = %v, want a 2026 time", got.LastSeen)
	}
}

func TestParseStoredTimeRejectsGarbageWithoutPanicking(t *testing.T) {
	for _, v := range []sql.NullString{
		{Valid: false},
		{Valid: true, String: ""},
		{Valid: true, String: "not a date"},
	} {
		if got := parseStoredTime(v); got != nil {
			t.Errorf("parseStoredTime(%+v) = %v, want nil", v, got)
		}
	}
}

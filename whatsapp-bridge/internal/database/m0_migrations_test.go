package database

import (
	"database/sql"
	"strings"
	"testing"
	"time"
)

// assertTableColumns fails the test for every column in want that table lacks.
func assertTableColumns(t *testing.T, db *sql.DB, table string, want ...string) {
	t.Helper()
	rows, err := db.Query("PRAGMA table_info(" + table + ")")
	if err != nil {
		t.Fatalf("table_info(%s): %v", table, err)
	}
	defer func() { _ = rows.Close() }()
	have := map[string]bool{}
	for rows.Next() {
		var cid, notNull, pk int
		var name, typ string
		var dflt sql.NullString
		if err := rows.Scan(&cid, &name, &typ, &notNull, &dflt, &pk); err != nil {
			t.Fatalf("scan table_info(%s): %v", table, err)
		}
		have[name] = true
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("table_info(%s): %v", table, err)
	}
	var missing []string
	for _, c := range want {
		if !have[c] {
			missing = append(missing, c)
		}
	}
	if len(missing) > 0 {
		t.Errorf("table %s is missing columns: %s", table, strings.Join(missing, ", "))
	}
}

// TestM0MigrationsIdempotent runs the migrations a second time over a migrated
// store, as happens on every bridge restart, and checks the M0 schema is there.
func TestM0MigrationsIdempotent(t *testing.T) {
	store := newTestMessageStore(t)
	if err := runMigrations(store.db); err != nil {
		t.Fatalf("second runMigrations: %v", err)
	}
	assertTableColumns(t, store.db, "message_reactions", "chat_jid", "message_id", "sender", "emoji", "reacted_at")
	assertTableColumns(t, store.db, "chat_state", "chat_jid", "pinned", "pinned_changed_at", "muted_until", "muted_until_changed_at", "archived", "archived_changed_at", "marked_unread", "marked_unread_changed_at", "last_read_at", "history_unread_count")
	assertTableColumns(t, store.db, "identities", "is_community", "community_jid", "is_default_subgroup", "i_am_admin")
}

// TestUpsertIdentitiesCommunityColumns checks the 013 columns are written.
func TestUpsertIdentitiesCommunityColumns(t *testing.T) {
	store := newTestMessageStore(t)
	rows := []Identity{{
		JID: "G1@g.us", Kind: "group",
		IsCommunity: false, CommunityJID: "P@g.us", IsDefaultSubgroup: true, IAmAdmin: true,
	}}
	for i := 0; i < 2; i++ { // second pass exercises ON CONFLICT
		if err := store.UpsertIdentities(rows); err != nil {
			t.Fatalf("upsert: %v", err)
		}
	}
	var isCommunity, isDefault, admin bool
	var parent string
	err := store.db.QueryRow(`SELECT is_community, community_jid, is_default_subgroup, i_am_admin FROM identities WHERE jid = ?`, "G1@g.us").
		Scan(&isCommunity, &parent, &isDefault, &admin)
	if err != nil {
		t.Fatal(err)
	}
	if isCommunity || parent != "P@g.us" || !isDefault || !admin {
		t.Fatalf("got (%v,%q,%v,%v)", isCommunity, parent, isDefault, admin)
	}
}

// TestM0MigrationsUpgradePreM0Store opens a store as it was before M0 (base
// tables and the identity table without the 013 columns), puts a row in it and
// runs the migrations, as the live bridge does on its first start after the
// upgrade. The old row must survive and take the column defaults.
func TestM0MigrationsUpgradePreM0Store(t *testing.T) {
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

	// Pre-M0 really means none of the M0 additions exist yet.
	var n int
	if err := db.QueryRow(`SELECT COUNT(*) FROM sqlite_master WHERE name IN ('message_reactions', 'chat_state')`).Scan(&n); err != nil || n != 0 {
		t.Fatalf("pre-M0 store already has %d M0 tables (err %v)", n, err)
	}
	if _, err := db.Exec(`SELECT is_community FROM identities`); err == nil {
		t.Fatal("pre-M0 identities already has is_community")
	}
	if _, err := db.Exec(`INSERT INTO identities (jid, kind, full_name) VALUES ('G1@g.us', 'group', 'Old group')`); err != nil {
		t.Fatalf("insert old identity: %v", err)
	}
	if _, err := db.Exec(`INSERT INTO chats (jid, name) VALUES ('G1@g.us', 'Old group')`); err != nil {
		t.Fatalf("insert old chat: %v", err)
	}

	if err := runMigrations(db); err != nil {
		t.Fatalf("runMigrations over a pre-M0 store: %v", err)
	}

	assertTableColumns(t, db, "message_reactions", "chat_jid", "message_id", "sender", "emoji", "reacted_at")
	assertTableColumns(t, db, "chat_state", "chat_jid", "pinned", "pinned_changed_at", "muted_until", "muted_until_changed_at", "archived", "archived_changed_at", "marked_unread", "marked_unread_changed_at", "last_read_at", "history_unread_count")
	assertTableColumns(t, db, "identities", "is_community", "community_jid", "is_default_subgroup", "i_am_admin")

	var (
		name                          string
		isCommunity, isDefault, admin bool
		parent                        sql.NullString
	)
	err = db.QueryRow(`SELECT full_name, is_community, community_jid, is_default_subgroup, i_am_admin FROM identities WHERE jid = 'G1@g.us'`).
		Scan(&name, &isCommunity, &parent, &isDefault, &admin)
	if err != nil {
		t.Fatalf("read old identity: %v", err)
	}
	if name != "Old group" || isCommunity || parent.Valid || isDefault || admin {
		t.Errorf("old identity = (%q, %v, %v, %v, %v), want it kept with default community columns", name, isCommunity, parent, isDefault, admin)
	}
	if err := db.QueryRow(`SELECT COUNT(*) FROM chats WHERE jid = 'G1@g.us'`).Scan(&n); err != nil || n != 1 {
		t.Errorf("old chat rows = %d (err %v), want 1", n, err)
	}
	// And a second run is still a no-op.
	if err := runMigrations(db); err != nil {
		t.Fatalf("second runMigrations: %v", err)
	}
}

// A store that applied migrations/001 by hand has an older message_reactions.
// It is renamed, not dropped, and the new table takes its place.
func TestReactionsLegacyTableIsRenamed(t *testing.T) {
	db, err := sql.Open("sqlite3", "file:"+t.Name()+"?mode=memory&cache=shared&_foreign_keys=on")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	if err := createTables(db); err != nil {
		t.Fatal(err)
	}
	if err := createIdentityTable(db); err != nil {
		t.Fatal(err)
	}
	for _, q := range []string{
		`CREATE TABLE message_reactions (
			id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL, chat_jid TEXT NOT NULL,
			sender TEXT NOT NULL, sender_name TEXT, emoji TEXT NOT NULL,
			created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(message_id, chat_jid, sender, emoji))`,
		`CREATE INDEX idx_reactions_msg ON message_reactions(message_id, chat_jid)`,
		`CREATE INDEX idx_reactions_sender ON message_reactions(chat_jid, sender)`,
		`INSERT INTO message_reactions (message_id, chat_jid, sender, emoji) VALUES ('M1', 'c@s.whatsapp.net', 'a@s.whatsapp.net', '👍')`,
	} {
		if _, err := db.Exec(q); err != nil {
			t.Fatalf("%s: %v", q, err)
		}
	}

	for i := 0; i < 2; i++ { // the second run must be a no-op
		if err := runMigrations(db); err != nil {
			t.Fatalf("runMigrations #%d: %v", i+1, err)
		}
	}
	assertTableColumns(t, db, "message_reactions", "chat_jid", "message_id", "sender", "emoji", "reacted_at")
	assertTableColumns(t, db, "message_reactions_legacy", "id", "sender_name", "created_at")
	var n int
	if err := db.QueryRow(`SELECT COUNT(*) FROM message_reactions_legacy WHERE emoji = '👍'`).Scan(&n); err != nil || n != 1 {
		t.Errorf("legacy rows = %d (err %v), want the one row kept", n, err)
	}
	store := &MessageStore{db: db}
	if err := store.StoreReaction(Reaction{ChatJID: "c@s.whatsapp.net", TargetID: "M1", Sender: "a@s.whatsapp.net", Emoji: "🔥", At: time.Now()}); err != nil {
		t.Fatalf("StoreReaction on the upgraded store: %v", err)
	}
	if got, err := store.GetMessageReactionSummary("c@s.whatsapp.net", "M1"); err != nil || got["🔥"] != 1 || len(got) != 1 {
		t.Errorf("summary = %v, %v", got, err)
	}
}

// With message_reactions_legacy already present, a legacy message_reactions is
// left alone and the migration fails with a clear error instead of dropping.
func TestReactionsLegacyTableNameTakenFails(t *testing.T) {
	db, err := sql.Open("sqlite3", "file:"+t.Name()+"?mode=memory&cache=shared")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	for _, q := range []string{
		`CREATE TABLE message_reactions (id INTEGER PRIMARY KEY, message_id TEXT, chat_jid TEXT, sender TEXT, emoji TEXT)`,
		`CREATE TABLE message_reactions_legacy (id INTEGER PRIMARY KEY)`,
	} {
		if _, err := db.Exec(q); err != nil {
			t.Fatal(err)
		}
	}
	err = createReactionsTable(db)
	if err == nil || !strings.Contains(err.Error(), "message_reactions_legacy already exists") {
		t.Fatalf("err = %v, want a clear refusal", err)
	}
}

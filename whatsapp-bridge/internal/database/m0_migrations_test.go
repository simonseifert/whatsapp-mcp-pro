package database

import (
	"database/sql"
	"strings"
	"testing"
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

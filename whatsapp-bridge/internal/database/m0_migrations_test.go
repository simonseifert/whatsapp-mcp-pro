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
}

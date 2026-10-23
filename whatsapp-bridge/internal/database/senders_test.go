package database

import (
	"os"
	"strings"
	"testing"
	"time"
)

func TestNormaliseSendersSQLMatchesMigration(t *testing.T) {
	raw, err := os.ReadFile("../../migrations/008_normalise_senders.sql")
	if err != nil {
		t.Fatalf("read migration: %v", err)
	}
	var body []string
	for _, line := range strings.Split(string(raw), "\n") {
		if strings.HasPrefix(strings.TrimSpace(line), "--") {
			continue
		}
		body = append(body, line)
	}
	var stmts []string
	for _, s := range strings.Split(strings.Join(body, "\n"), ";") {
		if s = strings.TrimSpace(s); s != "" {
			stmts = append(stmts, s)
		}
	}
	if len(stmts) != len(normaliseSendersSQL) {
		t.Fatalf("migration has %d statements, Go mirror has %d", len(stmts), len(normaliseSendersSQL))
	}
	for i := range stmts {
		if stmts[i] != strings.TrimSpace(normaliseSendersSQL[i]) {
			t.Errorf("statement %d differs:\nfile: %s\ngo:   %s", i, stmts[i], normaliseSendersSQL[i])
		}
	}
}

func TestNormaliseBareSenders(t *testing.T) {
	store := newTestMessageStore(t)
	db := store.db
	const (
		phone   = "4915112345678"
		lid     = "123456789012345"
		group   = "120363000000000001@g.us"
		news    = "120363000000000002@newsletter"
		unknown = "987654321098765"
	)
	mustExec := func(q string, args ...any) {
		t.Helper()
		if _, err := db.Exec(q, args...); err != nil {
			t.Fatalf("%s: %v", q, err)
		}
	}
	mustExec(`INSERT INTO identities (jid, lid, phone_jid, phone) VALUES (?, ?, ?, ?)`,
		phone+"@s.whatsapp.net", lid+"@lid", phone+"@s.whatsapp.net", phone)

	ts := time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)
	rows := []struct {
		id, chat, sender, want string
	}{
		{"dm-other", phone + "@s.whatsapp.net", phone, phone + "@s.whatsapp.net"},
		{"dm-lid-chat", lid + "@lid", lid, lid + "@lid"},
		{"group-lid", group, lid, lid + "@lid"},
		{"group-phone", group, phone, phone + "@s.whatsapp.net"},
		{"group-unknown", group, unknown, unknown},
		{"newsletter", news, "120363000000000002", news},
		{"already-full", group, lid + "@lid", lid + "@lid"},
		{"empty", group, "", ""},
		{"not-digits", group, "abc", "abc"},
	}
	for _, r := range rows {
		mustExec(`INSERT OR IGNORE INTO chats (jid, name, last_message_time) VALUES (?, '', ?)`, r.chat, ts)
		if err := store.StoreMessage(r.id, r.chat, r.sender, "n", "hi", ts, false, "", "", "", "", nil, nil, nil, 0); err != nil {
			t.Fatalf("store %s: %v", r.id, err)
		}
	}

	n, err := store.NormaliseBareSenders()
	if err != nil {
		t.Fatalf("normalise: %v", err)
	}
	if n != 5 {
		t.Errorf("rewrote %d rows, want 5", n)
	}
	for _, r := range rows {
		var got string
		if err := db.QueryRow(`SELECT COALESCE(sender, '') FROM messages WHERE id = ?`, r.id).Scan(&got); err != nil {
			t.Fatalf("read %s: %v", r.id, err)
		}
		if got != r.want {
			t.Errorf("%s: sender = %q, want %q", r.id, got, r.want)
		}
	}

	if n, err := store.NormaliseBareSenders(); err != nil || n != 0 {
		t.Errorf("second run rewrote %d rows (err %v), want 0", n, err)
	}
}

func TestIsKnownLID(t *testing.T) {
	store := newTestMessageStore(t)
	if _, err := store.db.Exec(`INSERT INTO identities (jid, lid) VALUES ('4915112345678@s.whatsapp.net', '123456789012345@lid')`); err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		user string
		want bool
	}{
		{"123456789012345", true},
		{"4915112345678", false},
		{"", false},
	}
	for _, tt := range tests {
		got, err := store.IsKnownLID(tt.user)
		if err != nil || got != tt.want {
			t.Errorf("IsKnownLID(%q) = %v, %v; want %v", tt.user, got, err, tt.want)
		}
	}
}

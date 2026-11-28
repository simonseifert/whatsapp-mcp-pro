package database

import (
	"testing"
	"time"
)

func TestMigrationAddsStarsAndMentions(t *testing.T) {
	store := newTestMessageStore(t)
	var n int
	if err := store.db.QueryRow(`SELECT COUNT(*) FROM starred_messages`).Scan(&n); err != nil {
		t.Fatalf("starred_messages missing: %v", err)
	}
	if err := store.db.QueryRow(`SELECT COUNT(mentioned_jids) FROM messages`).Scan(&n); err != nil {
		t.Fatalf("messages.mentioned_jids missing: %v", err)
	}
}

func TestSetMessageStarred(t *testing.T) {
	store := newTestMessageStore(t)
	at := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
	if err := store.SetMessageStarred("1@s.whatsapp.net", "M1", true, at); err != nil {
		t.Fatal(err)
	}
	if err := store.SetMessageStarred("1@s.whatsapp.net", "M1", true, at.Add(time.Hour)); err != nil {
		t.Fatalf("starring twice: %v", err)
	}
	if ok, _ := store.IsMessageStarred("1@s.whatsapp.net", "M1"); !ok {
		t.Fatal("not starred")
	}
	if ok, _ := store.IsMessageStarred("2@s.whatsapp.net", "M1"); ok {
		t.Fatal("star leaked to another chat")
	}
	if err := store.SetMessageStarred("1@s.whatsapp.net", "M1", false, at); err != nil {
		t.Fatal(err)
	}
	if ok, _ := store.IsMessageStarred("1@s.whatsapp.net", "M1"); ok {
		t.Fatal("still starred after unstar")
	}
	if err := store.SetMessageStarred("1@s.whatsapp.net", "nope", false, at); err != nil {
		t.Fatalf("unstar of unknown: %v", err)
	}
}

func TestStoreMentionedJIDs(t *testing.T) {
	store := newTestMessageStore(t)
	chat := "G1@g.us"
	if err := store.StoreChat(chat, "G", time.Now()); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"M1", "M2"} {
		if _, err := store.db.Exec(`INSERT INTO messages (id, chat_jid, content) VALUES (?, ?, 'x')`, id, chat); err != nil {
			t.Fatal(err)
		}
	}
	if err := store.StoreMentionedJIDs("M1", chat, []string{"111@s.whatsapp.net", "222@lid"}); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreMentionedJIDs("M2", chat, nil); err != nil {
		t.Fatal(err)
	}
	got, err := store.GetMentionedJIDs("M1", chat)
	if err != nil || len(got) != 2 || got[0] != "111@s.whatsapp.net" || got[1] != "222@lid" {
		t.Fatalf("M1 mentions = %v, %v", got, err)
	}
	var raw *string
	if err := store.db.QueryRow(`SELECT mentioned_jids FROM messages WHERE id='M2'`).Scan(&raw); err != nil || raw != nil {
		t.Fatalf("M2 column = %v, %v; want NULL", raw, err)
	}
}

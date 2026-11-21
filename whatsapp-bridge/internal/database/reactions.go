package database

import (
	"database/sql"
	"fmt"
	"time"
)

// Reaction is one reactor's current emoji on one message. An empty Emoji passed
// to StoreReaction means "removed".
type Reaction struct {
	ChatJID  string
	TargetID string // the message reacted to
	Sender   string // StoredSender form
	Emoji    string
	At       time.Time
}

// createReactionsTable mirrors migrations/011_message_reactions.sql.
func createReactionsTable(db *sql.DB) error {
	_, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS message_reactions (
			chat_jid   TEXT NOT NULL,
			message_id TEXT NOT NULL,
			sender     TEXT NOT NULL,
			emoji      TEXT NOT NULL,
			reacted_at TIMESTAMP NOT NULL,
			PRIMARY KEY (chat_jid, message_id, sender)
		);
		CREATE INDEX IF NOT EXISTS idx_message_reactions_msg ON message_reactions(chat_jid, message_id);
	`)
	if err != nil {
		return fmt.Errorf("create message_reactions: %w", err)
	}
	return nil
}

// StoreReaction records r. An empty Emoji removes the reactor's row. Events can
// arrive out of order (history sync replays old ones), so a stored row whose
// time is newer than r.At is never overwritten or deleted.
func (store *MessageStore) StoreReaction(r Reaction) error {
	// The driver compares timestamps as text, so mixed zones would misorder
	// events; the edit code normalises to UTC for the same reason.
	r.At = r.At.UTC()
	if r.Emoji == "" {
		if _, err := store.db.Exec(
			`DELETE FROM message_reactions WHERE chat_jid = ? AND message_id = ? AND sender = ? AND reacted_at <= ?`,
			r.ChatJID, r.TargetID, r.Sender, r.At); err != nil {
			return fmt.Errorf("delete reaction: %w", err)
		}
		return nil
	}
	if _, err := store.db.Exec(`
		INSERT INTO message_reactions (chat_jid, message_id, sender, emoji, reacted_at)
		VALUES (?, ?, ?, ?, ?)
		ON CONFLICT(chat_jid, message_id, sender) DO UPDATE
		SET emoji = excluded.emoji, reacted_at = excluded.reacted_at
		WHERE excluded.reacted_at >= message_reactions.reacted_at`,
		r.ChatJID, r.TargetID, r.Sender, r.Emoji, r.At); err != nil {
		return fmt.Errorf("store reaction: %w", err)
	}
	return nil
}

// GetMessageReactionSummary returns emoji -> count for a message. A message
// nobody reacted to (or that is not stored) gives an empty map.
func (store *MessageStore) GetMessageReactionSummary(chatJID, msgID string) (map[string]int, error) {
	out := make(map[string]int)
	rows, err := store.db.Query(
		`SELECT emoji, COUNT(*) FROM message_reactions WHERE chat_jid = ? AND message_id = ? GROUP BY emoji`,
		chatJID, msgID)
	if err != nil {
		return nil, fmt.Errorf("query reactions: %w", err)
	}
	defer func() { _ = rows.Close() }()
	for rows.Next() {
		var emoji string
		var n int
		if err := rows.Scan(&emoji, &n); err != nil {
			return nil, fmt.Errorf("scan reaction: %w", err)
		}
		out[emoji] = n
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("read reactions: %w", err)
	}
	return out, nil
}

package database

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"time"
)

// createStarredMessagesTable mirrors migrations/015_stars_mentions.sql.
func createStarredMessagesTable(db *sql.DB) error {
	_, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS starred_messages (
			chat_jid   TEXT NOT NULL,
			message_id TEXT NOT NULL,
			starred_at TIMESTAMP,
			PRIMARY KEY (chat_jid, message_id)
		);
	`)
	if err != nil {
		return fmt.Errorf("create starred_messages: %w", err)
	}
	return nil
}

// SetMessageStarred stars (upserts) or unstars (deletes) one message. Starring
// again keeps the original starred_at.
func (store *MessageStore) SetMessageStarred(chatJID, messageID string, starred bool, at time.Time) error {
	if starred {
		_, err := store.db.Exec(
			`INSERT INTO starred_messages (chat_jid, message_id, starred_at) VALUES (?, ?, ?)
			 ON CONFLICT(chat_jid, message_id) DO NOTHING`,
			chatJID, messageID, changeTime(at),
		)
		return err
	}
	_, err := store.db.Exec(
		`DELETE FROM starred_messages WHERE chat_jid = ? AND message_id = ?`, chatJID, messageID)
	return err
}

// IsMessageStarred reports whether a message is starred.
func (store *MessageStore) IsMessageStarred(chatJID, messageID string) (bool, error) {
	var one int
	err := store.db.QueryRow(
		`SELECT 1 FROM starred_messages WHERE chat_jid = ? AND message_id = ?`, chatJID, messageID).Scan(&one)
	if err == sql.ErrNoRows {
		return false, nil
	}
	return err == nil, err
}

// StoreMentionedJIDs records who a message mentioned as a JSON array. An empty
// list leaves the column NULL.
func (store *MessageStore) StoreMentionedJIDs(id, chatJID string, jids []string) error {
	if len(jids) == 0 {
		return nil
	}
	raw, err := json.Marshal(jids)
	if err != nil {
		return err
	}
	_, err = store.db.Exec(`UPDATE messages SET mentioned_jids = ? WHERE id = ? AND chat_jid = ?`,
		string(raw), id, chatJID)
	return err
}

// GetMentionedJIDs returns the stored mentions of a message; nil when none.
func (store *MessageStore) GetMentionedJIDs(id, chatJID string) ([]string, error) {
	var raw sql.NullString
	if err := store.db.QueryRow(`SELECT mentioned_jids FROM messages WHERE id = ? AND chat_jid = ?`, id, chatJID).Scan(&raw); err != nil {
		return nil, err
	}
	if !raw.Valid || raw.String == "" {
		return nil, nil
	}
	var out []string
	if err := json.Unmarshal([]byte(raw.String), &out); err != nil {
		return nil, err
	}
	return out, nil
}

// LastMessageKey returns the newest message of a chat, for the range of a
// mark-as-unread patch. ok is false for an empty chat.
func (store *MessageStore) LastMessageKey(chatJID string) (id, sender string, fromMe bool, at time.Time, ok bool, err error) {
	var ts sql.NullTime
	err = store.db.QueryRow(
		`SELECT id, COALESCE(sender, ''), COALESCE(is_from_me, 0), timestamp FROM messages
		  WHERE chat_jid = ? ORDER BY datetime(timestamp) DESC LIMIT 1`, chatJID,
	).Scan(&id, &sender, &fromMe, &ts)
	if err == sql.ErrNoRows {
		return "", "", false, time.Time{}, false, nil
	}
	if err != nil {
		return "", "", false, time.Time{}, false, err
	}
	return id, sender, fromMe, ts.Time, true, nil
}

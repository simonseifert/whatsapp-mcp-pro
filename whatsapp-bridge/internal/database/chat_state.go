package database

import (
	"database/sql"
	"errors"
	"fmt"
	"time"
)

// ChatState is the pinned / muted / archived / read state of one chat.
type ChatState struct {
	ChatJID      string
	Pinned       bool
	Archived     bool
	MarkedUnread bool
	// MutedUntil is unix seconds; -1 means muted forever, 0 means not muted.
	// A past time also means not muted; compare with the current time.
	MutedUntil int64
	// LastReadAt is when the user last read the chat on any device; nil if unknown.
	LastReadAt *time.Time
	// HistoryUnreadCount is the unread count history sync reported; nil if never seeded.
	HistoryUnreadCount *int
}

// createChatStateTable mirrors migrations/012_chat_state.sql.
func createChatStateTable(db *sql.DB) error {
	_, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS chat_state (
			chat_jid                 TEXT PRIMARY KEY,
			pinned                   INTEGER NOT NULL DEFAULT 0,
			pinned_changed_at        TIMESTAMP,
			muted_until              INTEGER NOT NULL DEFAULT 0,
			muted_until_changed_at   TIMESTAMP,
			archived                 INTEGER NOT NULL DEFAULT 0,
			archived_changed_at      TIMESTAMP,
			marked_unread            INTEGER NOT NULL DEFAULT 0,
			marked_unread_changed_at TIMESTAMP,
			last_read_at             TIMESTAMP,
			history_unread_count     INTEGER
		);
	`)
	if err != nil {
		return fmt.Errorf("create chat_state: %w", err)
	}
	return nil
}

// changeTime converts an event time to UTC, because SQLite compares timestamps
// as text and mixed zones would misorder them. A time in the future is clamped
// to now, so a forged or skewed event cannot win every later comparison.
func changeTime(at time.Time) time.Time {
	at = at.UTC()
	if now := time.Now().UTC(); at.After(now) {
		return now
	}
	return at
}

// setChatField upserts one field (column and its _changed_at) with newer-wins
// semantics. column is a fixed identifier from this package, never user input.
func (store *MessageStore) setChatField(chatJID, column string, value any, at time.Time) error {
	at = changeTime(at)
	q := fmt.Sprintf(`
		INSERT INTO chat_state (chat_jid, %[1]s, %[1]s_changed_at) VALUES (?, ?, ?)
		ON CONFLICT(chat_jid) DO UPDATE
		SET %[1]s = excluded.%[1]s, %[1]s_changed_at = excluded.%[1]s_changed_at
		WHERE %[1]s_changed_at IS NULL OR excluded.%[1]s_changed_at >= %[1]s_changed_at`, column)
	if _, err := store.db.Exec(q, chatJID, value, at); err != nil {
		return fmt.Errorf("set chat %s: %w", column, err)
	}
	return nil
}

// SetChatPinned records whether chatJID is pinned. An event older than the
// stored one is ignored.
func (store *MessageStore) SetChatPinned(chatJID string, pinned bool, at time.Time) error {
	return store.setChatField(chatJID, "pinned", pinned, at)
}

// SetChatMuted records the mute end time in unix seconds (-1 = forever,
// 0 = not muted). An event older than the stored one is ignored.
func (store *MessageStore) SetChatMuted(chatJID string, mutedUntil int64, at time.Time) error {
	return store.setChatField(chatJID, "muted_until", mutedUntil, at)
}

// SetChatArchived records whether chatJID is archived. An event older than the
// stored one is ignored.
func (store *MessageStore) SetChatArchived(chatJID string, archived bool, at time.Time) error {
	return store.setChatField(chatJID, "archived", archived, at)
}

// SetChatMarkedUnread records whether the user manually marked chatJID unread.
// An event older than the stored one is ignored.
func (store *MessageStore) SetChatMarkedUnread(chatJID string, unread bool, at time.Time) error {
	return store.setChatField(chatJID, "marked_unread", unread, at)
}

// MarkChatReadAt records that the chat was read at the given time. It only
// moves last_read_at forward, and clears marked_unread when the read is newer
// than the mark.
func (store *MessageStore) MarkChatReadAt(chatJID string, at time.Time) error {
	at = changeTime(at)
	_, err := store.db.Exec(`
		INSERT INTO chat_state (chat_jid, last_read_at, marked_unread_changed_at) VALUES (?, ?, ?)
		ON CONFLICT(chat_jid) DO UPDATE SET
			marked_unread = CASE WHEN marked_unread_changed_at IS NULL OR excluded.last_read_at > marked_unread_changed_at
				THEN 0 ELSE marked_unread END,
			marked_unread_changed_at = CASE WHEN marked_unread_changed_at IS NULL OR excluded.last_read_at > marked_unread_changed_at
				THEN excluded.last_read_at ELSE marked_unread_changed_at END,
			last_read_at = CASE WHEN last_read_at IS NULL OR excluded.last_read_at > last_read_at
				THEN excluded.last_read_at ELSE last_read_at END`,
		chatJID, at, at)
	if err != nil {
		return fmt.Errorf("mark chat read: %w", err)
	}
	return nil
}

// SeedChatStateFromHistory fills fields that have never been set by a real
// event from a history-sync conversation. Seeded values carry no change time,
// so any later event replaces them, and existing event-driven values are never
// touched. With unreadCount 0 the chat counts as read up to latestMsg, and
// last_read_at only moves forward; the unread count takes the latest seed.
func (store *MessageStore) SeedChatStateFromHistory(chatJID string, pinned, archived, markedUnread bool, mutedUntil int64, unreadCount int, latestMsg time.Time) error {
	var lastRead any
	if unreadCount == 0 && !latestMsg.IsZero() {
		lastRead = changeTime(latestMsg)
	}
	_, err := store.db.Exec(`
		INSERT INTO chat_state (chat_jid, pinned, muted_until, archived, marked_unread, last_read_at, history_unread_count)
		VALUES (?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(chat_jid) DO UPDATE SET
			pinned = CASE WHEN pinned_changed_at IS NULL THEN excluded.pinned ELSE pinned END,
			muted_until = CASE WHEN muted_until_changed_at IS NULL THEN excluded.muted_until ELSE muted_until END,
			archived = CASE WHEN archived_changed_at IS NULL THEN excluded.archived ELSE archived END,
			marked_unread = CASE WHEN marked_unread_changed_at IS NULL THEN excluded.marked_unread ELSE marked_unread END,
			last_read_at = CASE WHEN excluded.last_read_at IS NOT NULL AND (last_read_at IS NULL OR excluded.last_read_at > last_read_at)
				THEN excluded.last_read_at ELSE last_read_at END,
			history_unread_count = excluded.history_unread_count`,
		chatJID, pinned, mutedUntil, archived, markedUnread, lastRead, unreadCount)
	if err != nil {
		return fmt.Errorf("seed chat state: %w", err)
	}
	return nil
}

// GetChatState returns the stored state for chatJID, or a zero ChatState when
// nothing is stored.
func (store *MessageStore) GetChatState(chatJID string) (ChatState, error) {
	var st ChatState
	var lastRead sql.NullTime
	var unread sql.NullInt64
	err := store.db.QueryRow(`
		SELECT chat_jid, pinned, muted_until, archived, marked_unread, last_read_at, history_unread_count
		FROM chat_state WHERE chat_jid = ?`, chatJID).
		Scan(&st.ChatJID, &st.Pinned, &st.MutedUntil, &st.Archived, &st.MarkedUnread, &lastRead, &unread)
	if errors.Is(err, sql.ErrNoRows) {
		return ChatState{}, nil
	}
	if err != nil {
		return ChatState{}, fmt.Errorf("get chat state: %w", err)
	}
	if lastRead.Valid {
		t := lastRead.Time.UTC()
		st.LastReadAt = &t
	}
	if unread.Valid {
		n := int(unread.Int64)
		st.HistoryUnreadCount = &n
	}
	return st, nil
}

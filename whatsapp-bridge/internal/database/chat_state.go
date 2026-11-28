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
// than the mark (a tie counts as newer, like setChatField).
func (store *MessageStore) MarkChatReadAt(chatJID string, at time.Time) error {
	at = changeTime(at)
	_, err := store.db.Exec(`
		INSERT INTO chat_state (chat_jid, last_read_at, marked_unread_changed_at) VALUES (?, ?, ?)
		ON CONFLICT(chat_jid) DO UPDATE SET
			marked_unread = CASE WHEN marked_unread_changed_at IS NULL OR excluded.last_read_at >= marked_unread_changed_at
				THEN 0 ELSE marked_unread END,
			marked_unread_changed_at = CASE WHEN marked_unread_changed_at IS NULL OR excluded.last_read_at >= marked_unread_changed_at
				THEN excluded.last_read_at ELSE marked_unread_changed_at END,
			last_read_at = CASE WHEN last_read_at IS NULL OR excluded.last_read_at > last_read_at
				THEN excluded.last_read_at ELSE last_read_at END`,
		chatJID, at, at)
	if err != nil {
		return fmt.Errorf("mark chat read: %w", err)
	}
	return nil
}

// ChatSeed is what a history-sync conversation says about a chat. A nil field
// was absent from the conversation and is left alone: the proto getters turn a
// missing value into 0 or false, which would mark an unseen chat read or
// unpin a pinned one, so callers must pass nil rather than a default.
type ChatSeed struct {
	Pinned       *bool
	Archived     *bool
	MarkedUnread *bool
	// MutedUntil is unix seconds; -1 means muted forever, 0 means not muted.
	MutedUntil  *int64
	UnreadCount *int
	// LatestMsg is the newest message in the conversation; with UnreadCount 0
	// the chat counts as read up to then.
	LatestMsg time.Time
}

// anyOrNil turns an optional value into a SQL parameter (NULL when absent).
func anyOrNil[T any](p *T) any {
	if p == nil {
		return nil
	}
	return *p
}

// SeedChatStateFromHistory fills fields that have never been set by a real
// event from a history-sync conversation. Seeded values carry no change time,
// so any later event replaces them, and existing event-driven values are never
// touched. Only the fields present in seed are written. With an unread count
// of 0 the chat counts as read up to LatestMsg, and last_read_at only moves
// forward; a present unread count takes the latest seed.
func (store *MessageStore) SeedChatStateFromHistory(chatJID string, seed ChatSeed) error {
	var lastRead any
	if seed.UnreadCount != nil && *seed.UnreadCount == 0 && !seed.LatestMsg.IsZero() {
		lastRead = changeTime(seed.LatestMsg)
	}
	pinned, archived := anyOrNil(seed.Pinned), anyOrNil(seed.Archived)
	markedUnread, muted := anyOrNil(seed.MarkedUnread), anyOrNil(seed.MutedUntil)
	unread := anyOrNil(seed.UnreadCount)
	_, err := store.db.Exec(`
		INSERT INTO chat_state (chat_jid, pinned, muted_until, archived, marked_unread, last_read_at, history_unread_count)
		VALUES (?, COALESCE(?, 0), COALESCE(?, 0), COALESCE(?, 0), COALESCE(?, 0), ?, ?)
		ON CONFLICT(chat_jid) DO UPDATE SET
			pinned = CASE WHEN pinned_changed_at IS NULL AND ? IS NOT NULL THEN excluded.pinned ELSE pinned END,
			muted_until = CASE WHEN muted_until_changed_at IS NULL AND ? IS NOT NULL THEN excluded.muted_until ELSE muted_until END,
			archived = CASE WHEN archived_changed_at IS NULL AND ? IS NOT NULL THEN excluded.archived ELSE archived END,
			marked_unread = CASE WHEN marked_unread_changed_at IS NULL AND ? IS NOT NULL THEN excluded.marked_unread ELSE marked_unread END,
			last_read_at = CASE WHEN excluded.last_read_at IS NOT NULL AND (last_read_at IS NULL OR excluded.last_read_at > last_read_at)
				THEN excluded.last_read_at ELSE last_read_at END,
			history_unread_count = COALESCE(excluded.history_unread_count, history_unread_count)`,
		chatJID, pinned, muted, archived, markedUnread, lastRead, unread,
		pinned, muted, archived, markedUnread)
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

// chatStateRow is one chat_state row with its change times, as the merge sees it.
type chatStateRow struct {
	pinned, archived, markedUnread bool
	muted                          int64
	pinnedAt, mutedAt              sql.NullTime
	archivedAt, markedUnreadAt     sql.NullTime
	lastRead                       sql.NullTime
	unread                         sql.NullInt64
}

// mergeField picks the surviving value of one field. A change time beats a
// seed (no change time); of two change times the newer wins, the target on a
// tie. Two seeds: the target's value if set (non-zero), else the source's.
func mergeField[T comparable](tv T, tAt sql.NullTime, sv T, sAt sql.NullTime) (T, sql.NullTime) {
	var zero T
	switch {
	case tAt.Valid && sAt.Valid:
		if sAt.Time.After(tAt.Time) {
			return sv, sAt
		}
		return tv, tAt
	case sAt.Valid:
		return sv, sAt
	case tAt.Valid:
		return tv, tAt
	case tv != zero:
		return tv, tAt
	default:
		return sv, sAt
	}
}

// mergeChatStateTx folds chat_state under chat key from into key to inside a
// merge: per field by change time (see mergeField), last_read_at the later of
// the two, history_unread_count from the target if set, and the source row is
// deleted.
func mergeChatStateTx(tx *sql.Tx, from, to string) error {
	read := func(jid string) (*chatStateRow, error) {
		var r chatStateRow
		err := tx.QueryRow(`
			SELECT pinned, pinned_changed_at, muted_until, muted_until_changed_at, archived, archived_changed_at,
			       marked_unread, marked_unread_changed_at, last_read_at, history_unread_count
			FROM chat_state WHERE chat_jid = ?`, jid).
			Scan(&r.pinned, &r.pinnedAt, &r.muted, &r.mutedAt, &r.archived, &r.archivedAt,
				&r.markedUnread, &r.markedUnreadAt, &r.lastRead, &r.unread)
		if errors.Is(err, sql.ErrNoRows) {
			return nil, nil
		}
		if err != nil {
			return nil, fmt.Errorf("read chat state %s: %w", jid, err)
		}
		return &r, nil
	}
	src, err := read(from)
	if err != nil || src == nil {
		return err
	}
	dst, err := read(to)
	if err != nil {
		return err
	}
	if dst == nil {
		dst = &chatStateRow{}
	}

	var m chatStateRow
	m.pinned, m.pinnedAt = mergeField(dst.pinned, dst.pinnedAt, src.pinned, src.pinnedAt)
	m.muted, m.mutedAt = mergeField(dst.muted, dst.mutedAt, src.muted, src.mutedAt)
	m.archived, m.archivedAt = mergeField(dst.archived, dst.archivedAt, src.archived, src.archivedAt)
	m.markedUnread, m.markedUnreadAt = mergeField(dst.markedUnread, dst.markedUnreadAt, src.markedUnread, src.markedUnreadAt)
	m.lastRead = dst.lastRead
	if src.lastRead.Valid && (!dst.lastRead.Valid || src.lastRead.Time.After(dst.lastRead.Time)) {
		m.lastRead = src.lastRead
	}
	m.unread = dst.unread
	if !m.unread.Valid {
		m.unread = src.unread
	}

	if _, err := tx.Exec(`
		INSERT INTO chat_state (chat_jid, pinned, pinned_changed_at, muted_until, muted_until_changed_at,
			archived, archived_changed_at, marked_unread, marked_unread_changed_at, last_read_at, history_unread_count)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(chat_jid) DO UPDATE SET
			pinned = excluded.pinned, pinned_changed_at = excluded.pinned_changed_at,
			muted_until = excluded.muted_until, muted_until_changed_at = excluded.muted_until_changed_at,
			archived = excluded.archived, archived_changed_at = excluded.archived_changed_at,
			marked_unread = excluded.marked_unread, marked_unread_changed_at = excluded.marked_unread_changed_at,
			last_read_at = excluded.last_read_at, history_unread_count = excluded.history_unread_count`,
		to, m.pinned, m.pinnedAt, m.muted, m.mutedAt, m.archived, m.archivedAt,
		m.markedUnread, m.markedUnreadAt, m.lastRead, m.unread); err != nil {
		return fmt.Errorf("write merged chat state %s: %w", to, err)
	}
	if _, err := tx.Exec(`DELETE FROM chat_state WHERE chat_jid = ?`, from); err != nil {
		return fmt.Errorf("drop source chat state %s: %w", from, err)
	}
	return nil
}

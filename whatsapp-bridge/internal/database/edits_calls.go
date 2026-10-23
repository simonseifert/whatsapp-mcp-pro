package database

import (
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"
)

// editsCallsSQL mirrors the statements of migrations/009_edits_calls.sql;
// TestEditsCallsSQLMatchesMigration keeps them identical. The messages columns
// that migration documents are added by runMigrations, which tolerates a
// column that already exists.
var editsCallsSQL = []string{
	`CREATE TABLE IF NOT EXISTS message_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    chat_jid TEXT NOT NULL,
    edit_id TEXT NOT NULL,
    old_content TEXT,
    new_content TEXT NOT NULL,
    edited_at TIMESTAMP NOT NULL,
    editor_jid TEXT,
    source TEXT NOT NULL DEFAULT 'live',
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (chat_jid, message_id, edit_id)
)`,
	`CREATE INDEX IF NOT EXISTS idx_message_edits_message ON message_edits(message_id, chat_jid)`,
	`CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY,
    chat_jid TEXT NOT NULL DEFAULT '',
    caller_jid TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT 'unknown',
    is_video INTEGER NOT NULL DEFAULT 0,
    is_group INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMP NOT NULL,
    accepted_at TIMESTAMP,
    ended_at TIMESTAMP,
    duration_seconds INTEGER,
    outcome TEXT NOT NULL DEFAULT 'unknown',
    end_reason TEXT,
    source TEXT NOT NULL DEFAULT 'live',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)`,
	`CREATE INDEX IF NOT EXISTS idx_calls_chat_started ON calls(chat_jid, started_at)`,
	`CREATE INDEX IF NOT EXISTS idx_calls_started ON calls(started_at)`,
	`INSERT OR IGNORE INTO calls (call_id, chat_jid, caller_jid, direction, is_video, is_group, started_at, outcome, source)
SELECT substr(id, 6), chat_jid,
       CASE WHEN is_from_me THEN ''
            WHEN instr(COALESCE(sender, ''), '@') = 0 AND chat_jid NOT LIKE '%@g.us' THEN chat_jid
            ELSE COALESCE(sender, '') END,
       CASE WHEN is_from_me THEN 'outgoing' ELSE 'incoming' END,
       CASE WHEN content LIKE '%video call%' THEN 1 ELSE 0 END,
       CASE WHEN chat_jid LIKE '%@g.us' THEN 1 ELSE 0 END,
       timestamp,
       CASE WHEN content LIKE '%Missed call%' THEN 'missed' ELSE 'unknown' END,
       'message'
FROM messages
WHERE id LIKE 'call-%' AND media_type = 'call' AND timestamp IS NOT NULL`,
}

func createEditsCallsTables(db *sql.DB) error {
	for _, stmt := range editsCallsSQL {
		if _, err := db.Exec(stmt); err != nil {
			return fmt.Errorf("edits/calls schema (migrations/009): %w", err)
		}
	}
	return nil
}

// Edit and revoke sources, recorded so a reader can tell a live event from a
// history-sync replay or an edit this bridge sent itself.
const (
	SourceLive    = "live"
	SourceHistory = "history"
	SourceAPI     = "api"
)

// EditResult says what ApplyEdit did with an edit.
type EditResult struct {
	ChatJID     string // the chat the edited message is stored under
	OldContent  string // text replaced by this edit; "" when it was not applied or unknown
	Applied     bool   // messages.content now holds this edit's text
	Duplicate   bool   // this edit was already recorded; nothing changed
	Placeholder bool   // the original was never stored; a row was created from the edit
}

// Edit describes one edit of a stored message, as ApplyEdit takes it.
type Edit struct {
	MessageID  string
	ChatJIDs   []string // every form the chat may be stored under, canonical first
	NewContent string
	EditedAt   time.Time
	Editor     string // JID of whoever edited; the author, or an admin in a newsletter
	EditorIsMe bool
	StanzaID   string // the edit's own message ID, the dedupe key; may be empty
	Source     string // SourceLive, SourceHistory or SourceAPI
}

// MessageEdit is one row of message_edits.
type MessageEdit struct {
	MessageID  string
	ChatJID    string
	EditID     string
	OldContent sql.NullString
	NewContent string
	EditedAt   time.Time
	EditorJID  string
	Source     string
}

// findMessageChat returns which of chatJIDs holds message id. A conversation
// can still be split between its LID and phone JID, and an edit or revoke is
// addressed to whichever form the sender used.
func findMessageChat(q interface {
	QueryRow(string, ...any) *sql.Row
}, id string, chatJIDs []string) (string, error) {
	for _, chat := range chatJIDs {
		if chat == "" {
			continue
		}
		var found string
		err := q.QueryRow(`SELECT chat_jid FROM messages WHERE id = ? AND chat_jid = ?`, id, chat).Scan(&found)
		if err == nil {
			return found, nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return "", fmt.Errorf("look up message %s: %w", id, err)
		}
	}
	return "", nil
}

// editID never leaves the dedupe key empty: an empty edit_id would make every
// keyless edit of one message collide with the first.
func editID(id string, editedAt time.Time) string {
	if id != "" {
		return id
	}
	return fmt.Sprintf("ts-%d", editedAt.UnixMilli())
}

// ApplyEdit records an edit of message id and, unless a newer edit is already
// in place, makes newContent the message's content.
//
// Ordering: edits reach the bridge live, through history sync and through the
// reconnect backfill, in any order and more than once. Each one is recorded in
// message_edits once (keyed on chat, message and the edit's own stanza ID), but
// messages.content only moves forward: an edit older than messages.edited_at
// is history, not the current text. StoreMessage likewise leaves the content of
// an edited message alone, so a late replay of the original cannot undo an edit.
//
// e.ChatJIDs lists every form the chat may be stored under, canonical first. When
// the original was never stored, a row is created from the edit under the
// first form so the text is not lost; a later replay of the original fills in
// its media and real timestamp but keeps the edited text.
func (store *MessageStore) ApplyEdit(e Edit) (EditResult, error) {
	var res EditResult
	id, chatJIDs, newContent, editedAt := e.MessageID, e.ChatJIDs, e.NewContent, e.EditedAt
	if id == "" || len(chatJIDs) == 0 || chatJIDs[0] == "" {
		return res, errors.New("edit needs a message ID and chat JID")
	}
	editedAt = editedAt.UTC()
	tx, err := store.db.Begin()
	if err != nil {
		return res, fmt.Errorf("begin edit: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	chat, err := findMessageChat(tx, id, chatJIDs)
	if err != nil {
		return res, err
	}
	if chat == "" {
		chat = chatJIDs[0]
		if _, err := tx.Exec(`INSERT OR IGNORE INTO chats (jid) VALUES (?)`, chat); err != nil {
			return res, fmt.Errorf("ensure chat %s: %w", chat, err)
		}
		if _, err := tx.Exec(
			`INSERT INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_edited, edit_count)
			 VALUES (?, ?, ?, ?, NULL, ?, ?, 0, 0)`,
			id, chat, e.Editor, e.Editor, editedAt, e.EditorIsMe,
		); err != nil {
			return res, fmt.Errorf("placeholder for edited message %s: %w", id, err)
		}
		res.Placeholder = true
	}
	res.ChatJID = chat

	var current sql.NullString
	var lastEdit sql.NullTime
	if err := tx.QueryRow(`SELECT content, edited_at FROM messages WHERE id = ? AND chat_jid = ?`, id, chat).Scan(&current, &lastEdit); err != nil {
		return res, fmt.Errorf("read message %s: %w", id, err)
	}
	newest := !lastEdit.Valid || !editedAt.Before(lastEdit.Time)

	var old any
	if newest && !res.Placeholder {
		old = nullIfEmpty(current.String)
	}
	ins, err := tx.Exec(
		`INSERT OR IGNORE INTO message_edits (message_id, chat_jid, edit_id, old_content, new_content, edited_at, editor_jid, source)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		id, chat, editID(e.StanzaID, editedAt), old, newContent, editedAt, nullIfEmpty(e.Editor), e.Source,
	)
	if err != nil {
		return res, fmt.Errorf("record edit of %s: %w", id, err)
	}
	if n, _ := ins.RowsAffected(); n == 0 {
		res.Duplicate = true
		return res, tx.Commit()
	}

	if newest {
		if _, err := tx.Exec(
			`UPDATE messages SET content = ?, edited_at = ?, is_edited = 1, edit_count = COALESCE(edit_count, 0) + 1
			  WHERE id = ? AND chat_jid = ?`,
			newContent, editedAt, id, chat,
		); err != nil {
			return res, fmt.Errorf("apply edit of %s: %w", id, err)
		}
		res.Applied = true
		if !res.Placeholder {
			res.OldContent = current.String
		}
		// The recall index embedded the old text; dropping its row makes the
		// indexer embed the new one. The table belongs to the MCP server and
		// may not exist yet.
		if _, err := tx.Exec(`DELETE FROM message_embeddings WHERE message_id = ? AND chat_jid = ?`, id, chat); err != nil &&
			!strings.Contains(err.Error(), "no such table") {
			return res, fmt.Errorf("invalidate embedding of %s: %w", id, err)
		}
	} else if _, err := tx.Exec(
		`UPDATE messages SET is_edited = 1, edit_count = COALESCE(edit_count, 0) + 1 WHERE id = ? AND chat_jid = ?`, id, chat,
	); err != nil {
		return res, fmt.Errorf("count edit of %s: %w", id, err)
	}
	return res, tx.Commit()
}

// MarkDeleted records that message id was revoked ("deleted for everyone").
// The content is deliberately kept: the store is the user's archive, and the
// MCP shows such a message as "(deleted)" rather than pretending it never
// existed. The first revoke wins; a replay neither moves deleted_at nor counts
// as new (newly is false).
//
// Returns the chat the message is stored under. A revoke of a message the
// store never saw creates an empty row, so the deletion itself is on record
// and a later replay of the original fills in what it said.
func (store *MessageStore) MarkDeleted(id string, chatJIDs []string, deletedAt time.Time, deletedBy string) (chat string, newly bool, err error) {
	if id == "" || len(chatJIDs) == 0 || chatJIDs[0] == "" {
		return "", false, errors.New("revoke needs a message ID and chat JID")
	}
	deletedAt = deletedAt.UTC()
	tx, err := store.db.Begin()
	if err != nil {
		return "", false, fmt.Errorf("begin revoke: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	chat, err = findMessageChat(tx, id, chatJIDs)
	if err != nil {
		return "", false, err
	}
	if chat == "" {
		chat = chatJIDs[0]
		if _, err := tx.Exec(`INSERT OR IGNORE INTO chats (jid) VALUES (?)`, chat); err != nil {
			return "", false, fmt.Errorf("ensure chat %s: %w", chat, err)
		}
		if _, err := tx.Exec(
			`INSERT INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me)
			 VALUES (?, ?, ?, ?, NULL, ?, 0)`,
			id, chat, deletedBy, deletedBy, deletedAt,
		); err != nil {
			return "", false, fmt.Errorf("placeholder for revoked message %s: %w", id, err)
		}
	}
	res, err := tx.Exec(
		`UPDATE messages SET deleted_at = ?, deleted_by = NULLIF(?, '')
		  WHERE id = ? AND chat_jid = ? AND deleted_at IS NULL`,
		deletedAt, deletedBy, id, chat,
	)
	if err != nil {
		return "", false, fmt.Errorf("mark %s deleted: %w", id, err)
	}
	n, _ := res.RowsAffected()
	return chat, n > 0, tx.Commit()
}

// GetMessageEdits returns the recorded edits of a message, oldest first.
func (store *MessageStore) GetMessageEdits(id, chatJID string) ([]MessageEdit, error) {
	rows, err := store.db.Query(
		`SELECT message_id, chat_jid, edit_id, old_content, new_content, edited_at, COALESCE(editor_jid, ''), source
		   FROM message_edits WHERE message_id = ? AND chat_jid = ?
		  ORDER BY datetime(edited_at), id`,
		id, chatJID,
	)
	if err != nil {
		return nil, fmt.Errorf("read edits of %s: %w", id, err)
	}
	defer func() { _ = rows.Close() }()
	var out []MessageEdit
	for rows.Next() {
		var e MessageEdit
		if err := rows.Scan(&e.MessageID, &e.ChatJID, &e.EditID, &e.OldContent, &e.NewContent, &e.EditedAt, &e.EditorJID, &e.Source); err != nil {
			return nil, fmt.Errorf("scan edit: %w", err)
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

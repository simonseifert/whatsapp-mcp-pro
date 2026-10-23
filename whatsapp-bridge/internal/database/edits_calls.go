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
    status TEXT NOT NULL DEFAULT 'applied',
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

// message_edits.status values; see migrations/009_edits_calls.sql.
const (
	EditApplied    = "applied"
	EditSuperseded = "superseded"
	EditRejected   = "rejected"
	EditPending    = "pending"
)

// Actor is whoever sent an edit or revoke, as the event names them.
type Actor struct {
	JID   string   // stored form (StoredSender), "" when unknown
	Forms []string // other forms of the same account: its LID or phone JID, as far as known
	IsMe  bool     // sent from one of the user's own devices
}

// EditResult says what ApplyEdit did with an edit.
type EditResult struct {
	ChatJID     string // the chat the edited message is stored under
	OldContent  string // text replaced by this edit; "" when it was not applied or unknown
	Applied     bool   // messages.content now holds this edit's text
	Duplicate   bool   // this edit was already recorded; nothing changed
	Placeholder bool   // the original was never stored; a row was created from the edit
	Rejected    bool   // the editor is not the message's author; recorded, not applied
	Pending     bool   // the original is not stored; checked against its author when it is
}

// Edit describes one edit of a stored message, as ApplyEdit takes it.
type Edit struct {
	MessageID  string
	ChatJIDs   []string // every form the chat may be stored under, canonical first
	NewContent string
	EditedAt   time.Time
	Editor     Actor
	StanzaID   string // the edit's own message ID, the dedupe key; may be empty
	Source     string // SourceLive, SourceHistory or SourceAPI
	// Trusted skips the author check. Only for edits this bridge sent itself:
	// WhatsApp already refused them unless they were ours.
	Trusted bool
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
	Status     string
}

type querier interface {
	QueryRow(string, ...any) *sql.Row
	Query(string, ...any) (*sql.Rows, error)
}

// findMessageChat returns which of chatJIDs holds message id. A conversation
// can still be split between its LID and phone JID, and an edit or revoke is
// addressed to whichever form the sender used.
func findMessageChat(q querier, id string, chatJIDs []string) (string, error) {
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

// storedSenderJID turns a messages.sender value into one full JID. Rows from
// before full JIDs were stored hold bare digits, which say nothing about the
// namespace: a LID's digits can equal someone else's phone number. So a bare
// sender is the chat's own JID when it is the other side of a 1:1 chat, a LID
// only when the identity directory knows it as one, and a phone JID otherwise
// (the rule of migration 008 and bareRecipientJID).
func storedSenderJID(q querier, sender, chatJID string) string {
	sender = strings.TrimSpace(sender)
	if sender == "" || strings.Contains(sender, "@") {
		return sender
	}
	if user, server, ok := strings.Cut(chatJID, "@"); ok && server != "g.us" && user == sender {
		return chatJID
	}
	var one int
	if err := q.QueryRow(`SELECT 1 FROM identities WHERE lid = ? LIMIT 1`, sender+"@lid").Scan(&one); err == nil {
		return sender + "@lid"
	}
	return sender + "@s.whatsapp.net"
}

// accountForms expands full JIDs to the forms the same account is known
// under. LID and phone JID are linked only through the identities row; the
// caller adds what whatsmeow's LID map or the event's SenderAlt said. Values
// without a server are dropped rather than guessed at.
func accountForms(q querier, jids ...string) map[string]bool {
	out := map[string]bool{}
	var queue []string
	for _, j := range jids {
		if j = strings.TrimSpace(j); strings.Contains(j, "@") && !out[j] {
			out[j] = true
			queue = append(queue, j)
		}
	}
	for i := 0; i < len(queue); i++ {
		var jid, lid, phone string
		err := q.QueryRow(
			`SELECT COALESCE(jid, ''), COALESCE(lid, ''), COALESCE(phone_jid, '') FROM identities WHERE ? IN (jid, lid, phone_jid) LIMIT 1`,
			queue[i],
		).Scan(&jid, &lid, &phone)
		if err != nil {
			continue // no row, or a store without the directory
		}
		for _, f := range []string{jid, lid, phone} {
			if strings.Contains(f, "@") && !out[f] {
				out[f] = true
				queue = append(queue, f)
			}
		}
	}
	return out
}

// isAuthor reports whether actor wrote a message stored with sender and
// isFromMe in chatJID. Any participant can send an edit or revoke naming any
// message ID, so this is what stops one chat member from rewriting another's
// messages.
func isAuthor(q querier, sender sql.NullString, chatJID string, isFromMe bool, actor Actor) bool {
	if isFromMe {
		return actor.IsMe
	}
	if actor.IsMe || !sender.Valid {
		return false
	}
	author := storedSenderJID(q, sender.String, chatJID)
	if author == "" {
		return false
	}
	stored := accountForms(q, author)
	for f := range accountForms(q, append([]string{actor.JID}, actor.Forms...)...) {
		if stored[f] {
			return true
		}
	}
	return false
}

// editID never leaves the dedupe key empty: an empty edit_id would make every
// keyless edit of one message collide with the first.
func editID(id string, editedAt time.Time) string {
	if id != "" {
		return id
	}
	return fmt.Sprintf("ts-%d", editedAt.UnixMilli())
}

// ApplyEdit records an edit of message id and, if it came from the message's
// author and no newer edit is in place, makes it the message's content.
//
// Authorship: WhatsApp clients show an edit only from the original sender, but
// the protocol message is just "replace message X", and anyone in the chat can
// send one. An edit from someone else is recorded as rejected and changes
// nothing. One for a message not stored yet is recorded as pending and checked
// by StoreMessage when the original arrives; it no longer creates a row,
// which would have let a forged edit stand in for a message never seen.
//
// Ordering: edits reach the bridge live, through history sync and through the
// reconnect backfill, in any order and more than once. Each one is recorded in
// message_edits once (keyed on chat, message and the edit's own stanza ID), but
// messages.content only moves forward: an edit older than messages.edited_at
// is history, not the current text. StoreMessage likewise leaves the content of
// an edited message alone, so a late replay of the original cannot undo an edit.
//
// e.ChatJIDs lists every form the chat may be stored under, canonical first.
func (store *MessageStore) ApplyEdit(e Edit) (EditResult, error) {
	var res EditResult
	id, chatJIDs, newContent, editedAt := e.MessageID, e.ChatJIDs, e.NewContent, e.EditedAt
	if id == "" || len(chatJIDs) == 0 || chatJIDs[0] == "" {
		return res, errors.New("edit needs a message ID and chat JID")
	}
	// An imported row's ID is ours, not WhatsApp's: no genuine edit can name it,
	// so one that does is forged by whoever the importer mapped as its sender.
	if IsImportedMessageID(id) {
		res.ChatJID, res.Rejected = chatJIDs[0], true
		return res, nil
	}
	editedAt = editedAt.UTC()
	tx, err := store.db.Begin()
	if err != nil {
		return res, fmt.Errorf("begin edit: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	record := func(chat string, old any, status string) (bool, error) {
		ins, err := tx.Exec(
			`INSERT OR IGNORE INTO message_edits (message_id, chat_jid, edit_id, old_content, new_content, edited_at, editor_jid, source, status)
			 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			id, chat, editID(e.StanzaID, editedAt), old, newContent, editedAt, nullIfEmpty(e.Editor.JID), e.Source, status,
		)
		if err != nil {
			return false, fmt.Errorf("record edit of %s: %w", id, err)
		}
		n, _ := ins.RowsAffected()
		return n > 0, nil
	}

	chat, err := findMessageChat(tx, id, chatJIDs)
	if err != nil {
		return res, err
	}
	if chat == "" && !e.Trusted {
		res.ChatJID, res.Pending = chatJIDs[0], true
		added, err := record(chatJIDs[0], nil, EditPending)
		if err != nil {
			return res, err
		}
		res.Duplicate = !added
		return res, tx.Commit()
	}
	if chat == "" {
		chat = chatJIDs[0]
		if _, err := tx.Exec(`INSERT OR IGNORE INTO chats (jid) VALUES (?)`, chat); err != nil {
			return res, fmt.Errorf("ensure chat %s: %w", chat, err)
		}
		if _, err := tx.Exec(
			`INSERT INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_edited, edit_count)
			 VALUES (?, ?, ?, ?, NULL, ?, ?, 0, 0)`,
			id, chat, e.Editor.JID, e.Editor.JID, editedAt, e.Editor.IsMe,
		); err != nil {
			return res, fmt.Errorf("placeholder for edited message %s: %w", id, err)
		}
		res.Placeholder = true
	}
	res.ChatJID = chat

	var current, sender sql.NullString
	var lastEdit sql.NullTime
	var fromMe bool
	if err := tx.QueryRow(
		`SELECT content, edited_at, sender, COALESCE(is_from_me, 0) FROM messages WHERE id = ? AND chat_jid = ?`, id, chat,
	).Scan(&current, &lastEdit, &sender, &fromMe); err != nil {
		return res, fmt.Errorf("read message %s: %w", id, err)
	}
	if !e.Trusted && !isAuthor(tx, sender, chat, fromMe, e.Editor) {
		res.Rejected = true
		added, err := record(chat, nil, EditRejected)
		if err != nil {
			return res, err
		}
		res.Duplicate = !added
		return res, tx.Commit()
	}
	newest := !lastEdit.Valid || !editedAt.Before(lastEdit.Time)

	var old any
	status := EditSuperseded
	if newest {
		status = EditApplied
		if !res.Placeholder {
			old = nullIfEmpty(current.String)
		}
	}
	added, err := record(chat, old, status)
	if err != nil {
		return res, err
	}
	if !added {
		res.Duplicate = true
		return res, tx.Commit()
	}

	if newest {
		if err := setEditedContent(tx, id, chat, newContent, editedAt, 1); err != nil {
			return res, err
		}
		res.Applied = true
		if !res.Placeholder {
			res.OldContent = current.String
		}
	} else if _, err := tx.Exec(
		`UPDATE messages SET is_edited = 1, edit_count = COALESCE(edit_count, 0) + 1 WHERE id = ? AND chat_jid = ?`, id, chat,
	); err != nil {
		return res, fmt.Errorf("count edit of %s: %w", id, err)
	}
	return res, tx.Commit()
}

func setEditedContent(tx *sql.Tx, id, chat, content string, editedAt time.Time, count int) error {
	if _, err := tx.Exec(
		`UPDATE messages SET content = ?, edited_at = ?, is_edited = 1, edit_count = COALESCE(edit_count, 0) + ?
		  WHERE id = ? AND chat_jid = ?`,
		content, editedAt, count, id, chat,
	); err != nil {
		return fmt.Errorf("apply edit of %s: %w", id, err)
	}
	// The recall index embedded the old text; dropping its row makes the
	// indexer embed the new one. The table belongs to the MCP server and may
	// not exist yet.
	if _, err := tx.Exec(`DELETE FROM message_embeddings WHERE message_id = ? AND chat_jid = ?`, id, chat); err != nil &&
		!strings.Contains(err.Error(), "no such table") {
		return fmt.Errorf("invalidate embedding of %s: %w", id, err)
	}
	return nil
}

// resolvePendingEdits settles the edits recorded for a message before the
// message itself was stored: those from its author are applied (the newest
// becomes the content), the rest rejected. StoreMessage calls it after every
// insert, so it must be cheap when there is nothing pending.
func (store *MessageStore) resolvePendingEdits(id, chat string) error {
	var n int
	if err := store.db.QueryRow(
		`SELECT COUNT(*) FROM message_edits WHERE message_id = ? AND chat_jid = ? AND status = ?`, id, chat, EditPending,
	).Scan(&n); err != nil {
		if strings.Contains(err.Error(), "no such table") {
			return nil
		}
		return fmt.Errorf("check pending edits of %s: %w", id, err)
	}
	if n == 0 {
		return nil
	}
	tx, err := store.db.Begin()
	if err != nil {
		return fmt.Errorf("begin pending edits: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	var content, sender sql.NullString
	var lastEdit sql.NullTime
	var fromMe bool
	if err := tx.QueryRow(
		`SELECT content, edited_at, sender, COALESCE(is_from_me, 0) FROM messages WHERE id = ? AND chat_jid = ?`, id, chat,
	).Scan(&content, &lastEdit, &sender, &fromMe); err != nil {
		return fmt.Errorf("read message %s: %w", id, err)
	}
	type pending struct {
		rowID    int64
		text     string
		editedAt time.Time
		editor   string
	}
	rows, err := tx.Query(
		`SELECT id, new_content, edited_at, COALESCE(editor_jid, '') FROM message_edits
		  WHERE message_id = ? AND chat_jid = ? AND status = ? ORDER BY datetime(edited_at), id`,
		id, chat, EditPending,
	)
	if err != nil {
		return fmt.Errorf("read pending edits of %s: %w", id, err)
	}
	var all []pending
	for rows.Next() {
		var p pending
		if err := rows.Scan(&p.rowID, &p.text, &p.editedAt, &p.editor); err != nil {
			_ = rows.Close()
			return fmt.Errorf("scan pending edit: %w", err)
		}
		all = append(all, p)
	}
	_ = rows.Close()
	if err := rows.Err(); err != nil {
		return fmt.Errorf("read pending edits of %s: %w", id, err)
	}

	// A pending edit from one of the user's own devices names the user's own
	// JID, which is what an own message's sender is stored as.
	var genuine []pending
	for _, p := range all {
		status := EditRejected
		if isAuthor(tx, sender, chat, false, Actor{JID: p.editor}) {
			status = EditSuperseded
			genuine = append(genuine, p)
		}
		if _, err := tx.Exec(`UPDATE message_edits SET status = ? WHERE id = ?`, status, p.rowID); err != nil {
			return fmt.Errorf("settle pending edit of %s: %w", id, err)
		}
	}
	if len(genuine) == 0 {
		return tx.Commit()
	}
	latest := genuine[len(genuine)-1]
	if lastEdit.Valid && latest.editedAt.Before(lastEdit.Time) {
		if _, err := tx.Exec(`UPDATE messages SET is_edited = 1, edit_count = COALESCE(edit_count, 0) + ? WHERE id = ? AND chat_jid = ?`,
			len(genuine), id, chat); err != nil {
			return fmt.Errorf("count edits of %s: %w", id, err)
		}
		return tx.Commit()
	}
	if _, err := tx.Exec(`UPDATE message_edits SET status = ?, old_content = ? WHERE id = ?`,
		EditApplied, nullIfEmpty(content.String), latest.rowID); err != nil {
		return fmt.Errorf("apply pending edit of %s: %w", id, err)
	}
	if err := setEditedContent(tx, id, chat, latest.text, latest.editedAt, len(genuine)); err != nil {
		return err
	}
	return tx.Commit()
}

// Revoke describes a "deleted for everyone", as MarkDeleted takes it.
type Revoke struct {
	MessageID string
	ChatJIDs  []string // every form the chat may be stored under, canonical first
	At        time.Time
	By        Actor
	// IsAdmin reports whether By is an admin of the (group) chat, who may
	// delete anyone's message. Called only when By is not the author; nil
	// means not an admin.
	IsAdmin func() bool
	// Trusted skips the checks: revokes this bridge sent (WhatsApp refused
	// them unless allowed) and history-sync revoke stubs, which the phone
	// wrote after the server accepted the revoke.
	Trusted bool
}

// RevokeResult says what MarkDeleted did with a revoke.
type RevokeResult struct {
	ChatJID  string
	Newly    bool // deleted_at was set now; false for a replay
	Rejected bool // sent by neither the author nor an admin; ignored
	Ignored  bool // unverifiable: the message is not stored and the revoker is not an admin
}

// MarkDeleted records that a message was revoked ("deleted for everyone").
// The content is deliberately kept: the store is the user's archive, and the
// MCP shows such a message as "(deleted)" rather than pretending it never
// existed. The first revoke wins; a replay neither moves deleted_at nor counts
// as new.
//
// Only the author or, in a group, an admin may revoke; anyone else's revoke
// is the same forgery as a foreign edit and is ignored. A revoke of a message
// the store never saw is recorded (as an empty row a later replay fills in)
// only when it is trusted or from an admin, because there is no author to check.
func (store *MessageStore) MarkDeleted(r Revoke) (RevokeResult, error) {
	var res RevokeResult
	if r.MessageID == "" || len(r.ChatJIDs) == 0 || r.ChatJIDs[0] == "" {
		return res, errors.New("revoke needs a message ID and chat JID")
	}
	if IsImportedMessageID(r.MessageID) {
		res.ChatJID, res.Rejected = r.ChatJIDs[0], true
		return res, nil
	}
	id, at := r.MessageID, r.At.UTC()
	admin := func() bool { return r.IsAdmin != nil && r.IsAdmin() }
	tx, err := store.db.Begin()
	if err != nil {
		return res, fmt.Errorf("begin revoke: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	chat, err := findMessageChat(tx, id, r.ChatJIDs)
	if err != nil {
		return res, err
	}
	if chat == "" {
		if !r.Trusted && !admin() {
			res.ChatJID, res.Ignored = r.ChatJIDs[0], true
			return res, nil
		}
		chat = r.ChatJIDs[0]
		if _, err := tx.Exec(`INSERT OR IGNORE INTO chats (jid) VALUES (?)`, chat); err != nil {
			return res, fmt.Errorf("ensure chat %s: %w", chat, err)
		}
		if _, err := tx.Exec(
			`INSERT INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me)
			 VALUES (?, ?, NULL, NULL, NULL, ?, 0)`,
			id, chat, at,
		); err != nil {
			return res, fmt.Errorf("placeholder for revoked message %s: %w", id, err)
		}
	} else if !r.Trusted {
		var sender sql.NullString
		var fromMe bool
		if err := tx.QueryRow(`SELECT sender, COALESCE(is_from_me, 0) FROM messages WHERE id = ? AND chat_jid = ?`, id, chat).
			Scan(&sender, &fromMe); err != nil {
			return res, fmt.Errorf("read message %s: %w", id, err)
		}
		if !isAuthor(tx, sender, chat, fromMe, r.By) && !admin() {
			res.ChatJID, res.Rejected = chat, true
			return res, nil
		}
	}
	res.ChatJID = chat
	upd, err := tx.Exec(
		`UPDATE messages SET deleted_at = ?, deleted_by = NULLIF(?, '')
		  WHERE id = ? AND chat_jid = ? AND deleted_at IS NULL`,
		at, r.By.JID, id, chat,
	)
	if err != nil {
		return res, fmt.Errorf("mark %s deleted: %w", id, err)
	}
	n, _ := upd.RowsAffected()
	res.Newly = n > 0
	return res, tx.Commit()
}

// GetMessageEdits returns the recorded edits of a message, oldest first,
// including rejected and pending ones (see Status).
func (store *MessageStore) GetMessageEdits(id, chatJID string) ([]MessageEdit, error) {
	rows, err := store.db.Query(
		`SELECT message_id, chat_jid, edit_id, old_content, new_content, edited_at, COALESCE(editor_jid, ''), source, status
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
		if err := rows.Scan(&e.MessageID, &e.ChatJID, &e.EditID, &e.OldContent, &e.NewContent, &e.EditedAt, &e.EditorJID, &e.Source, &e.Status); err != nil {
			return nil, fmt.Errorf("scan edit: %w", err)
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

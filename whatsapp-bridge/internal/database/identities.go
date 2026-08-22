package database

import (
	"database/sql"
	"fmt"
	"strings"
	"time"
)

// Identity is one row of the unified directory: a single person, group,
// newsletter or broadcast list, keyed on a canonical JID.
//
// WhatsApp hands us the same person under two identities — the phone JID
// ({phone}@s.whatsapp.net) and the hidden LID ({id}@lid) — and the contact
// store keeps a separate row for each, usually with different names on them
// (the phone row carries the address-book name, the LID row carries the push
// name). Nothing in whatsmeow joins the two. This table is that join, so a
// caller holding either identity can get the whole picture in one lookup.
type Identity struct {
	JID           string // canonical: phone JID when known, else whatever we have
	LID           string // {id}@lid, empty if unknown
	PhoneJID      string // {phone}@s.whatsapp.net, empty if unknown
	Phone         string // bare digits from PhoneJID
	RedactedPhone string // "+1********80" — all WhatsApp gives us for LID-only group members
	Kind          string // user | group | newsletter | broadcast
	FirstName     string
	FullName      string
	PushName      string
	BusinessName  string
	Nickname      string // user-defined, from contact_nicknames
	DisplayName   string // resolved best name; see ResolveDisplayName
	ChatName      string // name on the chat row (group subject, or WhatsApp's chat title)
	IsBusiness    bool
	IsContact     bool // present in the phone's address book
	HasChat       bool // has a row in chats
	MessageCount  int

	// Groups only, filled from live server metadata when connected.
	ParticipantCount int
	IsAnnounce       bool   // admins-only posting
	OwnerJID         string // group creator, phone JID where known

	FirstSeen *time.Time
	LastSeen  *time.Time
	Sources   []string // where the row came from: contacts, lid_map, chats, messages
}

// ChatRow is the subset of the chats table the identity sync cares about.
type ChatRow struct {
	JID             string
	Name            string
	LastMessageTime *time.Time
	MergedInto      string
}

// ChatStats are per-chat message aggregates used to enrich the directory.
type ChatStats struct {
	Count     int
	FirstSeen *time.Time
	LastSeen  *time.Time
}

// ResolveDisplayName picks the best human name available, most specific first.
// A nickname is a deliberate override by the user, so it always wins; the push
// name is self-chosen and beats nothing but the raw number.
func (i *Identity) ResolveDisplayName() string {
	for _, candidate := range []string{
		i.Nickname, i.FullName, i.FirstName, i.BusinessName, i.ChatName, i.PushName, i.Phone, i.RedactedPhone,
	} {
		if strings.TrimSpace(candidate) != "" {
			return strings.TrimSpace(candidate)
		}
	}
	return i.JID
}

// createIdentityTable creates the directory table and its indexes.
func createIdentityTable(db *sql.DB) error {
	_, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS identities (
			jid            TEXT PRIMARY KEY,
			lid            TEXT,
			phone_jid      TEXT,
			phone          TEXT,
			redacted_phone TEXT,
			kind           TEXT NOT NULL DEFAULT 'user',
			first_name     TEXT,
			full_name      TEXT,
			push_name      TEXT,
			business_name  TEXT,
			nickname       TEXT,
			display_name   TEXT,
			chat_name      TEXT,
			is_business    BOOLEAN DEFAULT 0,
			is_contact     BOOLEAN DEFAULT 0,
			has_chat       BOOLEAN DEFAULT 0,
			message_count  INTEGER DEFAULT 0,
			participant_count INTEGER,
			is_announce    BOOLEAN DEFAULT 0,
			owner_jid      TEXT,
			first_seen     TIMESTAMP,
			last_seen      TIMESTAMP,
			sources        TEXT,
			created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
			updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
		);

		-- Deliberately non-unique. A duplicate lid means the lid map disagrees with
		-- itself, which is a warning worth logging, not a reason to fail every
		-- upsert in a long-running daemon.
		CREATE INDEX IF NOT EXISTS idx_identities_lid ON identities(lid);
		CREATE INDEX IF NOT EXISTS idx_identities_phone ON identities(phone);
		CREATE INDEX IF NOT EXISTS idx_identities_phone_jid ON identities(phone_jid);
		CREATE INDEX IF NOT EXISTS idx_identities_display_name ON identities(display_name COLLATE NOCASE);
		CREATE INDEX IF NOT EXISTS idx_identities_kind_last_seen ON identities(kind, last_seen DESC);
	`)
	return err
}

// UpsertIdentities writes the whole directory in one transaction. Rows are
// computed in full by the caller before they get here, so a plain overwrite of
// every field is correct — created_at is the only thing carried forward.
func (store *MessageStore) UpsertIdentities(rows []Identity) error {
	if len(rows) == 0 {
		return nil
	}

	tx, err := store.db.Begin()
	if err != nil {
		return fmt.Errorf("begin identity upsert: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op once Commit succeeds

	stmt, err := tx.Prepare(`
		INSERT INTO identities (
			jid, lid, phone_jid, phone, redacted_phone, kind,
			first_name, full_name, push_name, business_name, nickname, display_name, chat_name,
			is_business, is_contact, has_chat, message_count,
			participant_count, is_announce, owner_jid,
			first_seen, last_seen, sources, updated_at
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
		ON CONFLICT(jid) DO UPDATE SET
			lid            = excluded.lid,
			phone_jid      = excluded.phone_jid,
			phone          = excluded.phone,
			redacted_phone = excluded.redacted_phone,
			kind           = excluded.kind,
			first_name     = excluded.first_name,
			full_name      = excluded.full_name,
			push_name      = excluded.push_name,
			business_name  = excluded.business_name,
			nickname       = excluded.nickname,
			display_name   = excluded.display_name,
			chat_name      = excluded.chat_name,
			is_business    = excluded.is_business,
			is_contact     = excluded.is_contact,
			has_chat       = excluded.has_chat,
			message_count  = excluded.message_count,
			participant_count = excluded.participant_count,
			is_announce    = excluded.is_announce,
			owner_jid      = excluded.owner_jid,
			first_seen     = excluded.first_seen,
			last_seen      = excluded.last_seen,
			sources        = excluded.sources,
			updated_at     = CURRENT_TIMESTAMP
	`)
	if err != nil {
		return fmt.Errorf("prepare identity upsert: %w", err)
	}
	defer stmt.Close()

	for _, row := range rows {
		if row.DisplayName == "" {
			row.DisplayName = row.ResolveDisplayName()
		}
		_, err = stmt.Exec(
			row.JID, nullIfEmpty(row.LID), nullIfEmpty(row.PhoneJID), nullIfEmpty(row.Phone),
			nullIfEmpty(row.RedactedPhone), row.Kind,
			nullIfEmpty(row.FirstName), nullIfEmpty(row.FullName), nullIfEmpty(row.PushName),
			nullIfEmpty(row.BusinessName), nullIfEmpty(row.Nickname), row.DisplayName, nullIfEmpty(row.ChatName),
			row.IsBusiness, row.IsContact, row.HasChat, row.MessageCount,
			nullIfZero(row.ParticipantCount), row.IsAnnounce, nullIfEmpty(row.OwnerJID),
			row.FirstSeen, row.LastSeen, strings.Join(row.Sources, ","),
		)
		if err != nil {
			return fmt.Errorf("upsert identity %s: %w", row.JID, err)
		}
	}

	return tx.Commit()
}

// PruneIdentities removes directory rows whose canonical JID is no longer
// produced by the sync — a contact deleted upstream, or a row that used to be
// keyed on a LID and now resolves to its phone JID.
//
// An empty keep list is treated as "the sync produced nothing", which is far
// more likely to be a failure than a genuinely empty address book, so it prunes
// nothing rather than emptying the directory.
//
// The keep list goes through a temp table rather than a NOT IN (?, ?, ...):
// there is one placeholder per contact, and SQLite caps bound parameters per
// statement. A large address book would start failing at whatever that cap is.
func (store *MessageStore) PruneIdentities(keep []string) (int, error) {
	if len(keep) == 0 {
		return 0, nil
	}

	tx, err := store.db.Begin()
	if err != nil {
		return 0, fmt.Errorf("begin prune: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op once Commit succeeds

	if _, err = tx.Exec("CREATE TEMP TABLE IF NOT EXISTS identity_keep (jid TEXT PRIMARY KEY)"); err != nil {
		return 0, fmt.Errorf("create keep table: %w", err)
	}
	if _, err = tx.Exec("DELETE FROM identity_keep"); err != nil {
		return 0, fmt.Errorf("clear keep table: %w", err)
	}

	stmt, err := tx.Prepare("INSERT OR IGNORE INTO identity_keep (jid) VALUES (?)")
	if err != nil {
		return 0, fmt.Errorf("prepare keep insert: %w", err)
	}
	defer stmt.Close()
	for _, jid := range keep {
		if _, err = stmt.Exec(jid); err != nil {
			return 0, fmt.Errorf("record kept identity %s: %w", jid, err)
		}
	}

	res, err := tx.Exec("DELETE FROM identities WHERE jid NOT IN (SELECT jid FROM identity_keep)")
	if err != nil {
		return 0, fmt.Errorf("prune identities: %w", err)
	}
	affected, _ := res.RowsAffected()

	if err = tx.Commit(); err != nil {
		return 0, fmt.Errorf("commit prune: %w", err)
	}
	return int(affected), nil
}

// GetChatRows returns every chat, including ones already merged away.
func (store *MessageStore) GetChatRows() ([]ChatRow, error) {
	rows, err := store.db.Query("SELECT jid, COALESCE(name, ''), last_message_time, COALESCE(merged_into, '') FROM chats")
	if err != nil {
		return nil, fmt.Errorf("query chats: %w", err)
	}
	defer rows.Close()

	var out []ChatRow
	for rows.Next() {
		var chat ChatRow
		if err := rows.Scan(&chat.JID, &chat.Name, &chat.LastMessageTime, &chat.MergedInto); err != nil {
			return nil, fmt.Errorf("scan chat: %w", err)
		}
		out = append(out, chat)
	}
	return out, rows.Err()
}

// GetChatStats aggregates message counts and activity windows per chat.
//
// MIN/MAX over the timestamp column come back as raw strings, not time.Time:
// SQLite aggregates drop the column's declared TIMESTAMP affinity, so the
// go-sqlite3 driver no longer auto-parses them the way it does a plain column
// read. We scan strings and parse them ourselves.
func (store *MessageStore) GetChatStats() (map[string]ChatStats, error) {
	rows, err := store.db.Query(`
		SELECT chat_jid, COUNT(*), MIN(timestamp), MAX(timestamp)
		FROM messages
		GROUP BY chat_jid
	`)
	if err != nil {
		return nil, fmt.Errorf("query chat stats: %w", err)
	}
	defer rows.Close()

	out := make(map[string]ChatStats)
	for rows.Next() {
		var jid string
		var stats ChatStats
		var minTS, maxTS sql.NullString
		if err := rows.Scan(&jid, &stats.Count, &minTS, &maxTS); err != nil {
			return nil, fmt.Errorf("scan chat stats: %w", err)
		}
		stats.FirstSeen = parseStoredTime(minTS)
		stats.LastSeen = parseStoredTime(maxTS)
		out[jid] = stats
	}
	return out, rows.Err()
}

// storedTimeLayouts covers both writers into messages.db: the Go bridge (space
// separator, numeric offset) and the Python MCP paths (isoformat, "T"
// separator, sometimes no zone). Ordered most-specific first.
var storedTimeLayouts = []string{
	"2006-01-02 15:04:05.999999999-07:00",
	"2006-01-02 15:04:05-07:00",
	"2006-01-02T15:04:05.999999999-07:00",
	time.RFC3339Nano,
	time.RFC3339,
	"2006-01-02 15:04:05.999999999",
	"2006-01-02T15:04:05.999999999",
	"2006-01-02 15:04:05",
	"2006-01-02T15:04:05",
	"2006-01-02",
}

// parseStoredTime tolerantly parses a stored timestamp, returning nil on a NULL
// or unrecognized value rather than failing the whole sync — a single odd row
// must not blank the directory.
func parseStoredTime(v sql.NullString) *time.Time {
	if !v.Valid || v.String == "" {
		return nil
	}
	for _, layout := range storedTimeLayouts {
		if t, err := time.Parse(layout, v.String); err == nil {
			return &t
		}
	}
	return nil
}

// GetNicknames returns every user-defined nickname keyed by JID.
func (store *MessageStore) GetNicknames() (map[string]string, error) {
	rows, err := store.db.Query("SELECT jid, nickname FROM contact_nicknames")
	if err != nil {
		return nil, fmt.Errorf("query nicknames: %w", err)
	}
	defer rows.Close()

	out := make(map[string]string)
	for rows.Next() {
		var jid, nickname string
		if err := rows.Scan(&jid, &nickname); err != nil {
			return nil, fmt.Errorf("scan nickname: %w", err)
		}
		out[jid] = nickname
	}
	return out, rows.Err()
}

// MergeResult reports what MergeChat moved.
type MergeResult struct {
	MessagesMoved     int
	MessagesDuplicate int
	EmbeddingsMoved   int
	EmbeddingsDropped int
	TargetChatCreated bool
}

// MergeChat folds a chat filed under one identity into the chat filed under
// another — in practice, a LID chat into its phone-JID twin.
//
// WhatsApp delivers the same conversation under either identity depending on
// route, so history written before NormalizeChatJID existed is split across two
// chat rows: half the messages in one, half in the other. Anything keyed on
// chat_jid (search, recall, per-chat automation) silently sees only one half.
//
// The source chat row is kept and stamped with merged_into rather than deleted,
// so the merge leaves a breadcrumb and readers can still resolve an old LID.
// Messages that already exist verbatim under the target (same message ID) are
// dropped as true duplicates — UPDATE OR IGNORE leaves them behind, and keeping
// them would resurrect the split.
func (store *MessageStore) MergeChat(from, to string) (MergeResult, error) {
	var result MergeResult
	if from == to || from == "" || to == "" {
		return result, nil
	}

	tx, err := store.db.Begin()
	if err != nil {
		return result, fmt.Errorf("begin merge %s -> %s: %w", from, to, err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op once Commit succeeds

	// The target chat row has to exist first: messages.chat_jid is a foreign key
	// into chats(jid).
	res, err := tx.Exec(`
		INSERT INTO chats (jid, name, last_message_time)
		SELECT ?, name, last_message_time FROM chats WHERE jid = ?
		ON CONFLICT(jid) DO NOTHING
	`, to, from)
	if err != nil {
		return result, fmt.Errorf("ensure target chat %s: %w", to, err)
	}
	if created, _ := res.RowsAffected(); created > 0 {
		result.TargetChatCreated = true
	}

	res, err = tx.Exec("UPDATE OR IGNORE messages SET chat_jid = ? WHERE chat_jid = ?", to, from)
	if err != nil {
		return result, fmt.Errorf("move messages %s -> %s: %w", from, to, err)
	}
	moved, _ := res.RowsAffected()
	result.MessagesMoved = int(moved)

	res, err = tx.Exec("DELETE FROM messages WHERE chat_jid = ?", from)
	if err != nil {
		return result, fmt.Errorf("drop duplicate messages in %s: %w", from, err)
	}
	dupes, _ := res.RowsAffected()
	result.MessagesDuplicate = int(dupes)

	// message_embeddings is written by the Python recall backfill and may not exist.
	if store.tableExists(tx, "message_embeddings") {
		res, err = tx.Exec("UPDATE OR IGNORE message_embeddings SET chat_jid = ? WHERE chat_jid = ?", to, from)
		if err != nil {
			return result, fmt.Errorf("move embeddings %s -> %s: %w", from, to, err)
		}
		movedEmb, _ := res.RowsAffected()
		result.EmbeddingsMoved = int(movedEmb)

		res, err = tx.Exec("DELETE FROM message_embeddings WHERE chat_jid = ?", from)
		if err != nil {
			return result, fmt.Errorf("drop duplicate embeddings in %s: %w", from, err)
		}
		droppedEmb, _ := res.RowsAffected()
		result.EmbeddingsDropped = int(droppedEmb)
	}

	// Keep the newer of the two last-activity timestamps on the target.
	if _, err = tx.Exec(`
		UPDATE chats SET last_message_time = (
			SELECT MAX(timestamp) FROM messages WHERE chat_jid = ?
		) WHERE jid = ? AND EXISTS (SELECT 1 FROM messages WHERE chat_jid = ?)
	`, to, to, to); err != nil {
		return result, fmt.Errorf("refresh last_message_time for %s: %w", to, err)
	}

	if _, err = tx.Exec("UPDATE chats SET merged_into = ? WHERE jid = ?", to, from); err != nil {
		return result, fmt.Errorf("stamp merged_into on %s: %w", from, err)
	}

	if err = tx.Commit(); err != nil {
		return result, fmt.Errorf("commit merge %s -> %s: %w", from, to, err)
	}
	return result, nil
}

func (store *MessageStore) tableExists(tx *sql.Tx, name string) bool {
	var found string
	err := tx.QueryRow("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", name).Scan(&found)
	return err == nil
}

func nullIfZero(n int) interface{} {
	if n == 0 {
		return nil
	}
	return n
}

func nullIfEmpty(s string) interface{} {
	if strings.TrimSpace(s) == "" {
		return nil
	}
	return s
}

package database

import (
	"database/sql"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"
)

// Idempotency key states. A key moves in_flight -> done | failed; a failed key
// may be claimed again (failed -> in_flight) by a retry with the same payload.
const (
	IdempotencyInFlight = "in_flight"
	IdempotencyDone     = "done"
	IdempotencyFailed   = "failed"
)

// IdempotencyTTL is how long a key is remembered. 24h covers any realistic
// agent retry loop while keeping the table tiny.
const IdempotencyTTL = 24 * time.Hour

// idempotencyStaleInFlight is how long an in_flight row may sit before it is
// treated as abandoned (the bridge died or was restarted mid-send). Sends are
// bounded by SEND_TIMEOUT_SECONDS (well under a minute), so anything older than
// this can no longer complete. Treating it as failed lets a retry proceed; the
// retry reuses the stored message_id, so recipients de-duplicate if the first
// attempt did in fact go out.
const idempotencyStaleInFlight = 5 * time.Minute

// IdempotencyRecord is one stored idempotency key.
type IdempotencyRecord struct {
	Key         string
	Scope       string
	PayloadHash string
	Status      string
	MessageID   string
	HTTPStatus  int
	Response    []byte
	CreatedAt   time.Time
	UpdatedAt   time.Time
}

// createSendStateTables mirrors migrations/007_send_idempotency.sql so a store
// upgrades itself on start. Idempotent (IF NOT EXISTS).
func createSendStateTables(db *sql.DB) error {
	_, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS idempotency_keys (
			idem_key TEXT NOT NULL,
			scope TEXT NOT NULL,
			payload_hash TEXT NOT NULL,
			status TEXT NOT NULL,
			message_id TEXT NOT NULL DEFAULT '',
			http_status INTEGER NOT NULL DEFAULT 0,
			response TEXT NOT NULL DEFAULT '',
			created_at INTEGER NOT NULL,
			updated_at INTEGER NOT NULL,
			PRIMARY KEY (idem_key, scope)
		);
		CREATE INDEX IF NOT EXISTS idx_idempotency_keys_created_at ON idempotency_keys(created_at);
		CREATE TABLE IF NOT EXISTS bridge_state (
			key TEXT PRIMARY KEY,
			value TEXT NOT NULL,
			updated_at INTEGER NOT NULL
		);
	`)
	if err != nil {
		return fmt.Errorf("create send state tables: %w", err)
	}
	return nil
}

// ClaimIdempotencyKey atomically claims (key, scope) for a new send attempt.
//
// It returns the current record and whether the caller now owns the attempt
// (proceed). proceed is true when the key was unseen, or was failed/abandoned
// with the same payload hash; in that case the record's MessageID carries any
// WhatsApp message ID from the earlier attempt, to be reused. When proceed is
// false the caller inspects the record: a different PayloadHash means the key
// was reused for another payload, Status done means replay the stored response,
// in_flight means another request is sending right now.
//
// Expired keys are purged here, so the table needs no separate janitor.
func (store *MessageStore) ClaimIdempotencyKey(key, scope, payloadHash string, now time.Time) (*IdempotencyRecord, bool, error) {
	nowUnix := now.Unix()
	if _, err := store.db.Exec(`DELETE FROM idempotency_keys WHERE created_at < ?`, now.Add(-IdempotencyTTL).Unix()); err != nil {
		return nil, false, fmt.Errorf("purge expired idempotency keys: %w", err)
	}

	// INSERT OR IGNORE + RowsAffected is the atomic "first writer wins": two
	// concurrent requests with the same key cannot both see "unseen".
	res, err := store.db.Exec(`
		INSERT OR IGNORE INTO idempotency_keys (idem_key, scope, payload_hash, status, created_at, updated_at)
		VALUES (?, ?, ?, ?, ?, ?)`,
		key, scope, payloadHash, IdempotencyInFlight, nowUnix, nowUnix)
	if err != nil {
		return nil, false, fmt.Errorf("insert idempotency key: %w", err)
	}
	if n, _ := res.RowsAffected(); n == 1 {
		return &IdempotencyRecord{
			Key: key, Scope: scope, PayloadHash: payloadHash, Status: IdempotencyInFlight,
			CreatedAt: now, UpdatedAt: now,
		}, true, nil
	}

	// Existing key. Re-claim it if the previous attempt failed or was abandoned
	// and the payload matches; the WHERE clause makes the transition atomic.
	res, err = store.db.Exec(`
		UPDATE idempotency_keys SET status = ?, updated_at = ?
		WHERE idem_key = ? AND scope = ? AND payload_hash = ?
		  AND (status = ? OR (status = ? AND updated_at < ?))`,
		IdempotencyInFlight, nowUnix, key, scope, payloadHash,
		IdempotencyFailed, IdempotencyInFlight, now.Add(-idempotencyStaleInFlight).Unix())
	if err != nil {
		return nil, false, fmt.Errorf("reclaim idempotency key: %w", err)
	}
	reclaimed, _ := res.RowsAffected()

	rec, err := store.GetIdempotencyKey(key, scope)
	if err != nil {
		return nil, false, err
	}
	if rec == nil {
		// Purged between statements (only possible right at the TTL edge).
		return nil, false, fmt.Errorf("idempotency key %q vanished during claim", key)
	}
	return rec, reclaimed == 1, nil
}

// GetIdempotencyKey returns the record for (key, scope), or nil if none.
func (store *MessageStore) GetIdempotencyKey(key, scope string) (*IdempotencyRecord, error) {
	var rec IdempotencyRecord
	var response string
	var created, updated int64
	err := store.db.QueryRow(`
		SELECT idem_key, scope, payload_hash, status, message_id, http_status, response, created_at, updated_at
		FROM idempotency_keys WHERE idem_key = ? AND scope = ?`, key, scope).
		Scan(&rec.Key, &rec.Scope, &rec.PayloadHash, &rec.Status, &rec.MessageID, &rec.HTTPStatus, &response, &created, &updated)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("get idempotency key: %w", err)
	}
	rec.Response = []byte(response)
	rec.CreatedAt = time.Unix(created, 0)
	rec.UpdatedAt = time.Unix(updated, 0)
	return &rec, nil
}

// SetIdempotencyMessageID stores the WhatsApp message ID an attempt is about to
// send with, before it is dispatched, so a retry after a crash reuses it.
func (store *MessageStore) SetIdempotencyMessageID(key, scope, messageID string) error {
	_, err := store.db.Exec(`UPDATE idempotency_keys SET message_id = ? WHERE idem_key = ? AND scope = ?`, messageID, key, scope)
	if err != nil {
		return fmt.Errorf("set idempotency message id: %w", err)
	}
	return nil
}

// FinishIdempotencyKey records the outcome of a claimed attempt. status is
// IdempotencyDone or IdempotencyFailed. An empty messageID keeps whatever ID an
// earlier attempt stored.
func (store *MessageStore) FinishIdempotencyKey(key, scope, status, messageID string, httpStatus int, response []byte, now time.Time) error {
	_, err := store.db.Exec(`
		UPDATE idempotency_keys
		SET status = ?, message_id = CASE WHEN ? <> '' THEN ? ELSE message_id END,
		    http_status = ?, response = ?, updated_at = ?
		WHERE idem_key = ? AND scope = ?`,
		status, messageID, messageID, httpStatus, string(response), now.Unix(), key, scope)
	if err != nil {
		return fmt.Errorf("finish idempotency key: %w", err)
	}
	return nil
}

// GetBridgeState returns a persisted bridge state value ("" if unset).
func (store *MessageStore) GetBridgeState(key string) (string, error) {
	var value string
	err := store.db.QueryRow(`SELECT value FROM bridge_state WHERE key = ?`, key).Scan(&value)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("get bridge state %s: %w", key, err)
	}
	return value, nil
}

// SetBridgeState persists a bridge state value. An empty value deletes the key.
func (store *MessageStore) SetBridgeState(key, value string) error {
	var err error
	if value == "" {
		_, err = store.db.Exec(`DELETE FROM bridge_state WHERE key = ?`, key)
	} else {
		_, err = store.db.Exec(`
			INSERT INTO bridge_state (key, value, updated_at) VALUES (?, ?, ?)
			ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`,
			key, value, time.Now().Unix())
	}
	if err != nil {
		return fmt.Errorf("set bridge state %s: %w", key, err)
	}
	return nil
}

// GetBridgeStateTime reads a unix-seconds timestamp from bridge state (zero if unset).
func (store *MessageStore) GetBridgeStateTime(key string) (time.Time, error) {
	raw, err := store.GetBridgeState(key)
	if err != nil || raw == "" {
		return time.Time{}, err
	}
	secs, err := strconv.ParseInt(strings.TrimSpace(raw), 10, 64)
	if err != nil {
		return time.Time{}, fmt.Errorf("parse bridge state %s=%q: %w", key, raw, err)
	}
	return time.Unix(secs, 0), nil
}

// SetBridgeStateTime stores a timestamp as unix seconds; the zero time deletes it.
func (store *MessageStore) SetBridgeStateTime(key string, t time.Time) error {
	if t.IsZero() {
		return store.SetBridgeState(key, "")
	}
	return store.SetBridgeState(key, strconv.FormatInt(t.Unix(), 10))
}

// ChatHasMessages reports whether any of the given chat JIDs has at least one
// stored message. Used to tell an existing conversation from a new contact.
func (store *MessageStore) ChatHasMessages(chatJIDs ...string) (bool, error) {
	var jids []any
	for _, j := range chatJIDs {
		if j != "" {
			jids = append(jids, j)
		}
	}
	if len(jids) == 0 {
		return false, nil
	}
	placeholders := strings.TrimSuffix(strings.Repeat("?,", len(jids)), ",")
	var exists int
	// #nosec G202 -- only "?" placeholders are concatenated, values are bound.
	err := store.db.QueryRow(`SELECT EXISTS(SELECT 1 FROM messages WHERE chat_jid IN (`+placeholders+`))`, jids...).Scan(&exists)
	if err != nil {
		return false, fmt.Errorf("check chat history: %w", err)
	}
	return exists == 1, nil
}

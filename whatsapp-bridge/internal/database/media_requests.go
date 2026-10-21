package database

import (
	"database/sql"
	"errors"
	"fmt"
	"time"
)

// MediaRequestState is where a media retry request stands.
//
//	pending   -> sent        a retry receipt went to the sender's phone
//	sent      -> succeeded   the phone re-uploaded and the file was fetched
//	sent      -> pending     the phone gave a retryable answer, or none in time
//	sent      -> failed      the phone no longer has the file, or attempts ran out
//	succeeded -> pending     the media expired again and was re-enqueued
//
// failed is terminal for the background loop: re-asking a phone that said
// NOT_FOUND only produces another "Finished syncing" notification.
type MediaRequestState string

// Media request states; see MediaRequestState for the transitions.
const (
	MediaRequestPending   MediaRequestState = "pending"
	MediaRequestSent      MediaRequestState = "sent"
	MediaRequestSucceeded MediaRequestState = "succeeded"
	MediaRequestFailed    MediaRequestState = "failed"
)

// Valid reports whether s is one of the known states.
func (s MediaRequestState) Valid() bool {
	switch s {
	case MediaRequestPending, MediaRequestSent, MediaRequestSucceeded, MediaRequestFailed:
		return true
	}
	return false
}

// MediaRequest is one persisted ask to a sender's phone to re-upload media
// that WhatsApp's servers have purged.
type MediaRequest struct {
	MessageID     string            `json:"message_id"`
	ChatJID       string            `json:"chat_jid"`
	State         MediaRequestState `json:"state"`
	Attempts      int               `json:"attempts"`
	LastError     string            `json:"last_error,omitempty"`
	LastAttemptAt *time.Time        `json:"last_attempt_at,omitempty"`
	CreatedAt     time.Time         `json:"created_at"`
	UpdatedAt     time.Time         `json:"updated_at"`
}

// createMediaRequestTable mirrors migrations/005_media_requests.sql so tests
// and fresh stores get the table without running the SQL file by hand.
func createMediaRequestTable(db *sql.DB) error {
	_, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS media_requests (
			message_id      TEXT NOT NULL,
			chat_jid        TEXT NOT NULL,
			state           TEXT NOT NULL DEFAULT 'pending',
			attempts        INTEGER NOT NULL DEFAULT 0,
			last_error      TEXT NOT NULL DEFAULT '',
			last_attempt_at TIMESTAMP,
			created_at      TIMESTAMP NOT NULL,
			updated_at      TIMESTAMP NOT NULL,
			PRIMARY KEY (message_id, chat_jid)
		);
		CREATE INDEX IF NOT EXISTS idx_media_requests_state ON media_requests(state, created_at);
		CREATE INDEX IF NOT EXISTS idx_media_requests_last_attempt ON media_requests(last_attempt_at);
	`)
	if err != nil {
		return fmt.Errorf("create media_requests: %w", err)
	}
	return nil
}

// dbTime normalises timestamps before they reach SQLite. The driver stores
// time.Time as text, and the pacing and expiry queries compare that text, so
// every value must share one zone and precision to sort correctly.
func dbTime(t time.Time) time.Time {
	return t.UTC().Truncate(time.Second)
}

// EnqueueMediaRequest records that a message's media is gone and should be
// re-requested from the sender's phone. It is idempotent: an open request is
// left alone and a failed one stays failed, so repeated downloads of the same
// dead file cannot reset the attempt budget. Only a succeeded request reopens,
// because reaching this point again means the re-uploaded file expired too.
// Reports whether a request was created or reopened.
func (store *MessageStore) EnqueueMediaRequest(messageID, chatJID string, now time.Time) (bool, error) {
	now = dbTime(now)
	res, err := store.db.Exec(`
		INSERT INTO media_requests (message_id, chat_jid, state, attempts, last_error, created_at, updated_at)
		VALUES (?, ?, 'pending', 0, '', ?, ?)
		ON CONFLICT(message_id, chat_jid) DO UPDATE
		   SET state = 'pending', attempts = 0, last_error = '', updated_at = excluded.updated_at
		 WHERE media_requests.state = 'succeeded'`,
		messageID, chatJID, now, now)
	if err != nil {
		return false, fmt.Errorf("enqueue media request: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return false, fmt.Errorf("enqueue media request: %w", err)
	}
	return n > 0, nil
}

const mediaRequestColumns = `message_id, chat_jid, state, attempts, last_error, last_attempt_at, created_at, updated_at`

func scanMediaRequest(sc interface{ Scan(...any) error }) (MediaRequest, error) {
	var (
		r    MediaRequest
		last sql.NullTime
	)
	if err := sc.Scan(&r.MessageID, &r.ChatJID, &r.State, &r.Attempts, &r.LastError, &last, &r.CreatedAt, &r.UpdatedAt); err != nil {
		return MediaRequest{}, err
	}
	if last.Valid {
		t := last.Time
		r.LastAttemptAt = &t
	}
	return r, nil
}

func (store *MessageStore) queryMediaRequests(query string, args ...any) ([]MediaRequest, error) {
	rows, err := store.db.Query(query, args...)
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()
	var out []MediaRequest
	for rows.Next() {
		r, err := scanMediaRequest(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// GetMediaRequest returns one request, or nil if none exists.
func (store *MessageStore) GetMediaRequest(messageID, chatJID string) (*MediaRequest, error) {
	r, err := scanMediaRequest(store.db.QueryRow(
		`SELECT `+mediaRequestColumns+` FROM media_requests WHERE message_id = ? AND chat_jid = ?`,
		messageID, chatJID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("get media request: %w", err)
	}
	return &r, nil
}

// ListMediaRequests returns requests in state (all states when empty), most
// recently updated first.
func (store *MessageStore) ListMediaRequests(state MediaRequestState, limit int) ([]MediaRequest, error) {
	if limit <= 0 {
		limit = 100
	}
	var (
		out []MediaRequest
		err error
	)
	if state == "" {
		out, err = store.queryMediaRequests(
			`SELECT `+mediaRequestColumns+` FROM media_requests ORDER BY updated_at DESC LIMIT ?`, limit)
	} else {
		out, err = store.queryMediaRequests(
			`SELECT `+mediaRequestColumns+` FROM media_requests WHERE state = ? ORDER BY updated_at DESC LIMIT ?`, string(state), limit)
	}
	if err != nil {
		return nil, fmt.Errorf("list media requests: %w", err)
	}
	return out, nil
}

// DueMediaRequests returns up to limit pending requests, oldest first, so a
// backlog drains in the order the media was asked for.
func (store *MessageStore) DueMediaRequests(limit int) ([]MediaRequest, error) {
	out, err := store.queryMediaRequests(
		`SELECT `+mediaRequestColumns+` FROM media_requests WHERE state = 'pending' ORDER BY created_at, message_id LIMIT ?`, limit)
	if err != nil {
		return nil, fmt.Errorf("due media requests: %w", err)
	}
	return out, nil
}

// OpenMediaRequestsForMessage returns the pending or sent requests for a
// message id. It matches on the id alone: the phone's answer names the chat by
// whichever JID form it prefers, which need not be the one stored here.
func (store *MessageStore) OpenMediaRequestsForMessage(messageID string) ([]MediaRequest, error) {
	out, err := store.queryMediaRequests(
		`SELECT `+mediaRequestColumns+` FROM media_requests WHERE message_id = ? AND state IN ('pending', 'sent')`, messageID)
	if err != nil {
		return nil, fmt.Errorf("open media requests: %w", err)
	}
	return out, nil
}

// CountMediaRequestsSentSince counts requests whose receipt went out at or
// after since. Pacing reads this from the database rather than memory so a
// restart cannot reset the hourly budget and burst notifications at the phone.
func (store *MessageStore) CountMediaRequestsSentSince(since time.Time) (int, error) {
	var n int
	if err := store.db.QueryRow(
		`SELECT COUNT(*) FROM media_requests WHERE last_attempt_at >= ?`, dbTime(since),
	).Scan(&n); err != nil {
		return 0, fmt.Errorf("count media requests: %w", err)
	}
	return n, nil
}

// MarkMediaRequestSent records a retry receipt delivered to the phone. It
// creates the row if needed, because the synchronous /api/download path may
// send a receipt for media it has only just enqueued.
func (store *MessageStore) MarkMediaRequestSent(messageID, chatJID string, now time.Time) error {
	now = dbTime(now)
	_, err := store.db.Exec(`
		INSERT INTO media_requests (message_id, chat_jid, state, attempts, last_error, last_attempt_at, created_at, updated_at)
		VALUES (?, ?, 'sent', 1, '', ?, ?, ?)
		ON CONFLICT(message_id, chat_jid) DO UPDATE
		   SET state = 'sent', attempts = media_requests.attempts + 1, last_error = '',
		       last_attempt_at = excluded.last_attempt_at, updated_at = excluded.updated_at`,
		messageID, chatJID, now, now, now)
	if err != nil {
		return fmt.Errorf("mark media request sent: %w", err)
	}
	return nil
}

// MarkMediaRequestSendError records a receipt that never left the bridge. It
// still costs an attempt, so a request whose receipt cannot be built (a
// message missing from the store, say) fails out instead of looping forever.
func (store *MessageStore) MarkMediaRequestSendError(messageID, chatJID, errMsg string, maxAttempts int, now time.Time) error {
	_, err := store.db.Exec(`
		UPDATE media_requests
		   SET attempts = attempts + 1, last_error = ?, updated_at = ?,
		       state = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'pending' END
		 WHERE message_id = ? AND chat_jid = ?`,
		errMsg, dbTime(now), maxAttempts, messageID, chatJID)
	if err != nil {
		return fmt.Errorf("mark media request send error: %w", err)
	}
	return nil
}

// MarkMediaRequestSucceeded records that the re-uploaded file was fetched.
func (store *MessageStore) MarkMediaRequestSucceeded(messageID, chatJID string, now time.Time) error {
	_, err := store.db.Exec(
		`UPDATE media_requests SET state = 'succeeded', last_error = '', updated_at = ? WHERE message_id = ? AND chat_jid = ?`,
		dbTime(now), messageID, chatJID)
	if err != nil {
		return fmt.Errorf("mark media request succeeded: %w", err)
	}
	return nil
}

// MarkMediaRequestFailed closes a request for good, e.g. when the phone
// answered NOT_FOUND.
func (store *MessageStore) MarkMediaRequestFailed(messageID, chatJID, errMsg string, now time.Time) error {
	_, err := store.db.Exec(
		`UPDATE media_requests SET state = 'failed', last_error = ?, updated_at = ? WHERE message_id = ? AND chat_jid = ?`,
		errMsg, dbTime(now), messageID, chatJID)
	if err != nil {
		return fmt.Errorf("mark media request failed: %w", err)
	}
	return nil
}

// MarkMediaRequestRetry records a retryable outcome for a sent request: back
// to pending while attempts remain, failed once they are used up.
func (store *MessageStore) MarkMediaRequestRetry(messageID, chatJID, errMsg string, maxAttempts int, now time.Time) error {
	_, err := store.db.Exec(`
		UPDATE media_requests
		   SET last_error = ?, updated_at = ?,
		       state = CASE WHEN attempts >= ? THEN 'failed' ELSE 'pending' END
		 WHERE message_id = ? AND chat_jid = ? AND state IN ('pending', 'sent')`,
		errMsg, dbTime(now), maxAttempts, messageID, chatJID)
	if err != nil {
		return fmt.Errorf("mark media request retry: %w", err)
	}
	return nil
}

// ExpireSentMediaRequests treats sent requests with no answer since before
// cutoff as unanswered, usually because the phone was offline. Each goes back
// to pending, or to failed once its attempts are used up. Returns the number
// of requests moved.
func (store *MessageStore) ExpireSentMediaRequests(cutoff time.Time, maxAttempts int, now time.Time) (int64, error) {
	res, err := store.db.Exec(`
		UPDATE media_requests
		   SET last_error = 'no answer from the phone', updated_at = ?,
		       state = CASE WHEN attempts >= ? THEN 'failed' ELSE 'pending' END
		 WHERE state = 'sent' AND last_attempt_at < ?`,
		dbTime(now), maxAttempts, dbTime(cutoff))
	if err != nil {
		return 0, fmt.Errorf("expire media requests: %w", err)
	}
	return res.RowsAffected()
}

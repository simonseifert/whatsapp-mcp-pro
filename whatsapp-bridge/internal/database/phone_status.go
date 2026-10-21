package database

import (
	"database/sql"
	"errors"
	"fmt"
	"time"
)

// PhoneStatus is the persisted evidence of when the primary phone was last
// online. A linked device only keeps working while the phone checks in now and
// then: WhatsApp unlinks companions after roughly 14 days without it. The
// bridge cannot see the phone directly, so it records the last time it saw
// something only the phone could have sent.
//
// Zero values mean "never recorded". TrackingSince is set once, when the
// bridge first starts tracking, so a store that has never seen the phone still
// has a reference point to measure an outage from (see phone monitor).
type PhoneStatus struct {
	LastSeen      time.Time
	LastPinged    time.Time
	TrackingSince time.Time
}

// phoneStatusTableSQL mirrors migrations/005_phone_status.sql; keep them identical.
// One row (id = 1): this is a single-account bridge, and a fixed key keeps the
// upsert trivial. Millisecond epochs rather than TIMESTAMP text so no driver
// time-format parsing is involved; NULL means never recorded.
const phoneStatusTableSQL = `CREATE TABLE IF NOT EXISTS phone_status (
	id INTEGER PRIMARY KEY CHECK (id = 1),
	last_seen_ms INTEGER,
	last_pinged_ms INTEGER,
	tracking_since_ms INTEGER,
	updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)`

// LoadPhoneStatus returns the stored phone status. A missing row is not an
// error: it returns a zero PhoneStatus, which callers treat as "not tracked yet".
func (store *MessageStore) LoadPhoneStatus() (PhoneStatus, error) {
	var seen, pinged, since sql.NullInt64
	err := store.db.QueryRow(
		`SELECT last_seen_ms, last_pinged_ms, tracking_since_ms FROM phone_status WHERE id = 1`,
	).Scan(&seen, &pinged, &since)
	if errors.Is(err, sql.ErrNoRows) {
		return PhoneStatus{}, nil
	}
	if err != nil {
		return PhoneStatus{}, fmt.Errorf("load phone status: %w", err)
	}
	return PhoneStatus{
		LastSeen:      msToTime(seen),
		LastPinged:    msToTime(pinged),
		TrackingSince: msToTime(since),
	}, nil
}

// SavePhoneStatus overwrites the stored phone status with st.
func (store *MessageStore) SavePhoneStatus(st PhoneStatus) error {
	_, err := store.db.Exec(`
		INSERT INTO phone_status (id, last_seen_ms, last_pinged_ms, tracking_since_ms, updated_at)
		VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP)
		ON CONFLICT(id) DO UPDATE SET
			last_seen_ms = excluded.last_seen_ms,
			last_pinged_ms = excluded.last_pinged_ms,
			tracking_since_ms = excluded.tracking_since_ms,
			updated_at = CURRENT_TIMESTAMP`,
		timeToMs(st.LastSeen), timeToMs(st.LastPinged), timeToMs(st.TrackingSince),
	)
	if err != nil {
		return fmt.Errorf("save phone status: %w", err)
	}
	return nil
}

func timeToMs(t time.Time) sql.NullInt64 {
	if t.IsZero() {
		return sql.NullInt64{}
	}
	return sql.NullInt64{Int64: t.UnixMilli(), Valid: true}
}

func msToTime(v sql.NullInt64) time.Time {
	if !v.Valid {
		return time.Time{}
	}
	return time.UnixMilli(v.Int64).UTC()
}

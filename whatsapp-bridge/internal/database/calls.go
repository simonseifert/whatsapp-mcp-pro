package database

import (
	"database/sql"
	"errors"
	"fmt"
	"time"
)

// Call outcomes. Deliberately coarse: a linked device sees only part of a
// call (see applyCallEvent), and a finer label would claim more than the
// events support.
const (
	CallMissed   = "missed"
	CallAnswered = "answered"
	CallRejected = "rejected"
	CallUnknown  = "unknown"
)

// Call event kinds accepted by RecordCallEvent.
const (
	CallEventOffer     = "offer"     // 1:1 offer or group offer notice: the call starts ringing
	CallEventAccept    = "accept"    // someone picked up
	CallEventReject    = "reject"    // the callee declined
	CallEventTerminate = "terminate" // this device stopped ringing, or the call ended
	CallEventLog       = "log"       // the phone's own call-log entry: authoritative outcome and duration
	CallEventMissed    = "missed"    // a history-sync "missed call" stub
)

// Call is one row of the calls table.
type Call struct {
	CallID          string
	ChatJID         string
	CallerJID       string
	Direction       string // incoming, outgoing, unknown
	IsVideo         bool
	IsGroup         bool
	StartedAt       time.Time
	AcceptedAt      sql.NullTime
	EndedAt         sql.NullTime
	DurationSeconds sql.NullInt64
	Outcome         string
	EndReason       string
	Source          string
}

// CallEvent is one observation of a call, from whatever source.
type CallEvent struct {
	CallID    string
	Kind      string
	ChatJID   string
	CallerJID string
	Direction string
	IsVideo   bool
	IsGroup   bool
	At        time.Time
	// ByMe is set on an accept or reject that came from one of the user's own
	// devices: the call was handled on the phone, not by the other party.
	ByMe bool
	// Reason is the terminate reason WhatsApp sent, if any.
	Reason string
	// Outcome and Duration carry a CallEventLog's verdict.
	Outcome  string
	Duration time.Duration
	Source   string
}

// acceptedElsewhere is stored as end_reason when the phone took the call.
const acceptedElsewhere = "accepted_elsewhere"

// applyCallEvent folds one event into a call's state. Pure, so the outcome
// rules are testable without a store.
//
// A linked device stops ringing as soon as the phone answers: it gets the
// accept and then, right away, a terminate for its own leg, not for the end of
// the call. So a duration is only derived when the other party accepted (an
// outgoing call); an incoming call answered on the phone stays "answered" with
// no duration unless the phone's call-log entry supplies one.
func applyCallEvent(c Call, ev CallEvent) Call {
	at := ev.At.UTC()
	if c.CallID == "" {
		c = Call{CallID: ev.CallID, StartedAt: at, Outcome: CallUnknown, Direction: "unknown", Source: ev.Source}
	}
	if c.ChatJID == "" {
		c.ChatJID = ev.ChatJID
	}
	if c.CallerJID == "" {
		c.CallerJID = ev.CallerJID
	}
	if (c.Direction == "" || c.Direction == "unknown") && ev.Direction != "" {
		c.Direction = ev.Direction
	}
	c.IsVideo = c.IsVideo || ev.IsVideo
	c.IsGroup = c.IsGroup || ev.IsGroup
	if !at.IsZero() && (c.StartedAt.IsZero() || (ev.Kind == CallEventOffer && at.Before(c.StartedAt))) {
		c.StartedAt = at
	}

	switch ev.Kind {
	case CallEventAccept:
		if !c.AcceptedAt.Valid {
			c.AcceptedAt = sql.NullTime{Time: at, Valid: true}
		}
		c.Outcome = CallAnswered
		if ev.ByMe {
			c.EndReason = acceptedElsewhere
		}
	case CallEventReject:
		if c.Outcome != CallAnswered {
			c.Outcome = CallRejected
		}
		if !c.EndedAt.Valid {
			c.EndedAt = sql.NullTime{Time: at, Valid: true}
		}
	case CallEventTerminate:
		if !c.EndedAt.Valid {
			c.EndedAt = sql.NullTime{Time: at, Valid: true}
		}
		if ev.Reason != "" && c.EndReason == "" {
			c.EndReason = ev.Reason
		}
		switch {
		case c.AcceptedAt.Valid:
			c.Outcome = CallAnswered
			if c.EndReason != acceptedElsewhere && !c.DurationSeconds.Valid {
				if d := c.EndedAt.Time.Sub(c.AcceptedAt.Time); d >= 0 {
					c.DurationSeconds = sql.NullInt64{Int64: int64(d.Seconds()), Valid: true}
				}
			}
		case c.Outcome == CallRejected || c.Outcome == CallAnswered:
		case ev.Reason == "busy" || ev.Reason == "reject" || ev.Reason == "declined":
			c.Outcome = CallRejected
		default:
			// Rang out or the caller gave up. Had the phone picked up, its
			// accept would have arrived before this terminate.
			c.Outcome = CallMissed
		}
	case CallEventLog:
		if ev.Outcome != "" && ev.Outcome != CallUnknown {
			c.Outcome = ev.Outcome
		}
		if ev.Duration > 0 {
			c.DurationSeconds = sql.NullInt64{Int64: int64(ev.Duration.Seconds()), Valid: true}
		}
	case CallEventMissed:
		if c.Outcome == CallUnknown {
			c.Outcome = CallMissed
		}
	}
	return c
}

const callColumns = `call_id, chat_jid, caller_jid, direction, is_video, is_group, started_at, accepted_at, ended_at,
	duration_seconds, outcome, COALESCE(end_reason, ''), source`

func scanCall(row interface{ Scan(...any) error }) (Call, error) {
	var c Call
	err := row.Scan(&c.CallID, &c.ChatJID, &c.CallerJID, &c.Direction, &c.IsVideo, &c.IsGroup, &c.StartedAt,
		&c.AcceptedAt, &c.EndedAt, &c.DurationSeconds, &c.Outcome, &c.EndReason, &c.Source)
	return c, err
}

// callMatchWindow is how far apart a call-log entry or history stub may be
// from a live-recorded call and still be taken for the same call. Those carry
// their own message ID, not the call ID, so time and chat are all there is.
const callMatchWindow = 2 * time.Minute

// RecordCallEvent folds ev into the calls table and returns the resulting row.
//
// Live events are keyed on the call ID. A call-log entry or history stub has
// no call ID, so it first looks for a call in the same chat that started
// within callMatchWindow (before the entry, allowing for the call's duration)
// and only creates its own row, keyed on ev.CallID, when there is none.
func (store *MessageStore) RecordCallEvent(ev CallEvent) (Call, error) {
	if ev.CallID == "" {
		return Call{}, errors.New("call event without an ID")
	}
	tx, err := store.db.Begin()
	if err != nil {
		return Call{}, fmt.Errorf("begin call event: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	cur, err := scanCall(tx.QueryRow(`SELECT `+callColumns+` FROM calls WHERE call_id = ?`, ev.CallID))
	if errors.Is(err, sql.ErrNoRows) {
		cur = Call{}
		if (ev.Kind == CallEventLog || ev.Kind == CallEventMissed) && ev.ChatJID != "" {
			if cur, err = nearbyCall(tx, ev); err != nil {
				return Call{}, err
			}
		}
	} else if err != nil {
		return Call{}, fmt.Errorf("read call %s: %w", ev.CallID, err)
	}

	next := applyCallEvent(cur, ev)
	if _, err := tx.Exec(
		`INSERT INTO calls (call_id, chat_jid, caller_jid, direction, is_video, is_group, started_at, accepted_at, ended_at,
		                    duration_seconds, outcome, end_reason, source, updated_at)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULLIF(?, ''), ?, CURRENT_TIMESTAMP)
		 ON CONFLICT(call_id) DO UPDATE SET
			chat_jid = excluded.chat_jid, caller_jid = excluded.caller_jid, direction = excluded.direction,
			is_video = excluded.is_video, is_group = excluded.is_group, started_at = excluded.started_at,
			accepted_at = excluded.accepted_at, ended_at = excluded.ended_at,
			duration_seconds = excluded.duration_seconds, outcome = excluded.outcome,
			end_reason = excluded.end_reason, updated_at = CURRENT_TIMESTAMP`,
		next.CallID, next.ChatJID, next.CallerJID, next.Direction, next.IsVideo, next.IsGroup, next.StartedAt,
		next.AcceptedAt, next.EndedAt, next.DurationSeconds, next.Outcome, next.EndReason, next.Source,
	); err != nil {
		return Call{}, fmt.Errorf("store call %s: %w", next.CallID, err)
	}
	return next, tx.Commit()
}

// nearbyCall finds the call a call-log entry or missed-call stub describes.
// Compared through datetime() because started_at mixes offsets across rows.
func nearbyCall(tx *sql.Tx, ev CallEvent) (Call, error) {
	at := ev.At.UTC()
	from := at.Add(-ev.Duration - callMatchWindow).Format("2006-01-02 15:04:05")
	to := at.Add(callMatchWindow).Format("2006-01-02 15:04:05")
	rows, err := tx.Query(
		`SELECT `+callColumns+` FROM calls
		  WHERE chat_jid = ? AND datetime(started_at) BETWEEN datetime(?) AND datetime(?)`,
		ev.ChatJID, from, to,
	)
	if err != nil {
		return Call{}, fmt.Errorf("match call log entry: %w", err)
	}
	defer func() { _ = rows.Close() }()
	var best Call
	var bestGap time.Duration
	for rows.Next() {
		c, err := scanCall(rows)
		if err != nil {
			return Call{}, fmt.Errorf("scan call: %w", err)
		}
		gap := at.Sub(c.StartedAt)
		if gap < 0 {
			gap = -gap
		}
		if best.CallID == "" || gap < bestGap {
			best, bestGap = c, gap
		}
	}
	return best, rows.Err()
}

// GetCall returns one call, or sql.ErrNoRows.
func (store *MessageStore) GetCall(callID string) (Call, error) {
	return scanCall(store.db.QueryRow(`SELECT `+callColumns+` FROM calls WHERE call_id = ?`, callID))
}

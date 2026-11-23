package database

import (
	"database/sql"
	"fmt"
	"time"
)

// Receipt statuses stored in message_receipts.status.
const (
	ReceiptDelivered = 1
	ReceiptRead      = 2
	ReceiptPlayed    = 3
)

// createMessageReceiptsTable mirrors migrations/014_message_receipts.sql.
func createMessageReceiptsTable(db *sql.DB) error {
	_, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS message_receipts (
			message_id  TEXT NOT NULL,
			chat_jid    TEXT NOT NULL,
			participant TEXT NOT NULL,
			status      INTEGER NOT NULL,
			at          TIMESTAMP,
			PRIMARY KEY (message_id, chat_jid, participant)
		);
		CREATE INDEX IF NOT EXISTS idx_message_receipts_chat ON message_receipts (chat_jid, message_id);
	`)
	if err != nil {
		return fmt.Errorf("create message_receipts: %w", err)
	}
	return nil
}

// RecordMessageReceipts upserts one receipt per message id. A status only ever
// rises (a late delivered receipt never lowers read); the time is kept from the
// event that last raised it.
func (store *MessageStore) RecordMessageReceipts(chatJID, participant string, messageIDs []string, status int, at time.Time) error {
	if len(messageIDs) == 0 {
		return nil
	}
	tx, err := store.db.Begin()
	if err != nil {
		return fmt.Errorf("record receipts: %w", err)
	}
	defer func() { _ = tx.Rollback() }()
	stmt, err := tx.Prepare(`
		INSERT INTO message_receipts (message_id, chat_jid, participant, status, at) VALUES (?, ?, ?, ?, ?)
		ON CONFLICT(message_id, chat_jid, participant) DO UPDATE SET
			status = excluded.status, at = excluded.at
		WHERE excluded.status > message_receipts.status`)
	if err != nil {
		return fmt.Errorf("record receipts: %w", err)
	}
	defer func() { _ = stmt.Close() }()
	for _, id := range messageIDs {
		if id == "" {
			continue
		}
		if _, err := stmt.Exec(id, chatJID, participant, status, at.UTC()); err != nil {
			return fmt.Errorf("record receipt %s: %w", id, err)
		}
	}
	return tx.Commit()
}

// MessageReceiptStatus returns the highest receipt status any participant has
// for a message in a chat (0 if none).
func (store *MessageStore) MessageReceiptStatus(chatJID, messageID string) (int, error) {
	var status sql.NullInt64
	err := store.db.QueryRow(`SELECT MAX(status) FROM message_receipts WHERE chat_jid = ? AND message_id = ?`,
		chatJID, messageID).Scan(&status)
	if err != nil {
		return 0, fmt.Errorf("get receipt status: %w", err)
	}
	return int(status.Int64), nil
}

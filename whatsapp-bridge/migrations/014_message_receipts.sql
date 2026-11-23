-- Migration 014: Delivery, read and played receipts from other people
-- Target: store/messages.db
-- Date: 2026-11-23
-- Backward compatible: YES (only adds a new table)
--
-- Lets a chat app draw delivery / read ticks on our own messages. One row per
-- recipient per message. status only ever rises: 1 delivered, 2 read,
-- 3 played. chat_jid is the normalised chat key, participant the recipient's
-- non-AD JID. Mirrors createMessageReceiptsTable in
-- internal/database/receipts.go, which the bridge also runs at startup.

CREATE TABLE IF NOT EXISTS message_receipts (
    message_id  TEXT NOT NULL,
    chat_jid    TEXT NOT NULL,
    participant TEXT NOT NULL,
    status      INTEGER NOT NULL,
    at          TIMESTAMP,
    PRIMARY KEY (message_id, chat_jid, participant)
);

CREATE INDEX IF NOT EXISTS idx_message_receipts_chat ON message_receipts (chat_jid, message_id);

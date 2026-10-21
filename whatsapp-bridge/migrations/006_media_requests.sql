-- Migration 005: Persistent media retry queue
-- Target: store/messages.db
-- Date: 2026-10-22
-- Backward compatible: YES (only adds a new table)
--
-- When WhatsApp's servers have purged a file, only the sender's phone can bring
-- it back, by answering a media retry receipt. That answer used to be awaited
-- only inside the /api/download request, so a bridge restart or an offline
-- phone lost it. Each row here is one such ask, worked by a paced background
-- loop (internal/whatsapp/mediaretry_queue.go): every receipt makes the phone
-- show "Finished syncing", so sends are capped per hour.
--
-- state: pending -> sent -> succeeded | pending (retryable) | failed (NOT_FOUND
-- or attempts used up). Mirrors createMediaRequestTable in
-- internal/database/media_requests.go, which the bridge also runs at startup.

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

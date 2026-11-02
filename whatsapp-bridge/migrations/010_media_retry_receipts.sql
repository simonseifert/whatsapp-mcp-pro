-- Migration 010: Log of media retry receipts
-- Target: store/messages.db
-- Date: 2026-11-02
-- Backward compatible: YES (only adds a new table)
--
-- MEDIA_RETRY_MAX_PER_HOUR limits phone notifications, so it has to count
-- receipts. media_requests keeps only the latest attempt per message, so a file
-- asked about three times in an hour counted once. Rows older than a day are
-- pruned on insert. Mirrors createMediaRequestTable in
-- internal/database/media_requests.go, which the bridge also runs at startup.

CREATE TABLE IF NOT EXISTS media_retry_receipts (
    id      INTEGER PRIMARY KEY,
    sent_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_retry_receipts_sent ON media_retry_receipts(sent_at);

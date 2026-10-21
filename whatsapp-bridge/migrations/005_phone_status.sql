-- Migration 005: Phone-offline tracking
-- Target: store/messages.db
-- Date: 2026-10-22
-- Backward compatible: YES (only adds a new table)
--
-- A linked device keeps working only while the primary phone comes online now and then;
-- WhatsApp unlinks companions after ~14 days without it. The bridge records the last time it
-- saw something only the phone sends (its own receipts/messages, media-retry answers) so the
-- outage survives restarts and /api/connection can report it. One row, id = 1.
--
-- Times are epoch milliseconds; NULL means never recorded. tracking_since_ms is set once, the
-- first time the bridge tracks, so a never-seen phone still has a reference point.
--
-- The bridge also creates this table itself on start (runMigrations in
-- internal/database/store.go); keep the two definitions identical.

CREATE TABLE IF NOT EXISTS phone_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_seen_ms INTEGER,
    last_pinged_ms INTEGER,
    tracking_since_ms INTEGER,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

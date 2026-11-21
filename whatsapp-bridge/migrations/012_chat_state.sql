-- Migration 012: Pinned, muted, archived and read state per chat
-- Target: store/messages.db
-- Date: 2026-11-21
-- Backward compatible: YES (only adds a new table)
--
-- Lets a chat app read pin / mute / archive / read state straight from the
-- database. Every field carries its own <field>_changed_at so events that
-- arrive out of order (history sync replays old state) never overwrite a newer
-- value. History-sync seeds leave changed_at NULL, which any real event beats.
-- Timestamps are stored in UTC. muted_until is unix seconds (-1 = forever,
-- 0 = not muted). Mirrors createChatStateTable in
-- internal/database/chat_state.go, which the bridge also runs at startup.

CREATE TABLE IF NOT EXISTS chat_state (
    chat_jid                 TEXT PRIMARY KEY,
    pinned                   INTEGER NOT NULL DEFAULT 0,
    pinned_changed_at        TIMESTAMP,
    muted_until              INTEGER NOT NULL DEFAULT 0,
    muted_until_changed_at   TIMESTAMP,
    archived                 INTEGER NOT NULL DEFAULT 0,
    archived_changed_at      TIMESTAMP,
    marked_unread            INTEGER NOT NULL DEFAULT 0,
    marked_unread_changed_at TIMESTAMP,
    last_read_at             TIMESTAMP,
    history_unread_count     INTEGER
);

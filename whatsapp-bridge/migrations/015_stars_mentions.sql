-- Migration 015: Starred messages and stored mentions
-- Target: store/messages.db
-- Date: 2026-11-27
-- Backward compatible: YES (adds a table and a nullable column)
--
-- starred_messages holds one row per starred message, keyed on the normalised
-- chat key; unstarring deletes the row. messages.mentioned_jids is a JSON
-- array of bare user JIDs (a LID stays a LID), NULL when nobody is mentioned.
-- Mirrors createStarredMessagesTable in internal/database/stars.go and the
-- mentioned_jids entry in runMigrations, which the bridge runs at startup.

CREATE TABLE IF NOT EXISTS starred_messages (
    chat_jid   TEXT NOT NULL,
    message_id TEXT NOT NULL,
    starred_at TIMESTAMP,
    PRIMARY KEY (chat_jid, message_id)
);

ALTER TABLE messages ADD COLUMN mentioned_jids TEXT;

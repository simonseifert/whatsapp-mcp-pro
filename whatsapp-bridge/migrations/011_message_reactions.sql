-- Migration 011: Reactions linked to the message they react to
-- Target: store/messages.db
-- Date: 2026-11-21
-- Backward compatible: YES (only adds a new table)
--
-- Reactions used to be stored only as "[Reaction: X]" rows, which cannot say
-- which message was reacted to. This table keeps one row per reactor per
-- message, so a change of emoji replaces the old one and a removal is
-- kept as a row with an empty emoji (a tombstone, so an older add arriving later
-- cannot revive the reaction) that readers skip. The "[Reaction: X]" rows are still written. The message does not
-- have to be stored yet (history can deliver reactions first), so there is no
-- foreign key. Mirrors createReactionsTable in internal/database/reactions.go,
-- which the bridge also runs at startup.

CREATE TABLE IF NOT EXISTS message_reactions (
    chat_jid   TEXT NOT NULL,
    message_id TEXT NOT NULL,      -- the message reacted to
    sender     TEXT NOT NULL,      -- StoredSender form
    emoji      TEXT NOT NULL,
    reacted_at TIMESTAMP NOT NULL,
    PRIMARY KEY (chat_jid, message_id, sender)
);
CREATE INDEX IF NOT EXISTS idx_message_reactions_msg ON message_reactions(chat_jid, message_id);

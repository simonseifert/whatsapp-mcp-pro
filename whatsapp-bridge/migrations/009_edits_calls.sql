-- Migration 009: Edit history, revokes and a call log
-- Target: store/messages.db
-- Date: 2026-10-23
-- Backward compatible: YES (adds columns and tables, rewrites nothing)
--
-- Incoming edits and "deleted for everyone" revokes used to be dropped: the
-- store kept the original text with no sign that it had changed or been
-- withdrawn. Now an edit replaces messages.content and the previous text goes
-- to message_edits; a revoke stamps deleted_at/deleted_by and KEEPS the content,
-- because the store is the user's own archive.
--
-- Columns added to messages. SQLite has no ADD COLUMN IF NOT EXISTS, so the
-- bridge applies these through runMigrations, which ignores "duplicate column
-- name". Run by hand, each fails harmlessly on a store that already has it
-- (the sqlite3 shell carries on past the error unless started with -bail):
--
--   ALTER TABLE messages ADD COLUMN edited_at TIMESTAMP;
--   ALTER TABLE messages ADD COLUMN deleted_at TIMESTAMP;
--   ALTER TABLE messages ADD COLUMN deleted_by TEXT;
--
-- edited_at is the time of the edit whose text messages.content holds. An edit
-- older than that (a history-sync replay) is recorded in message_edits but does
-- not overwrite the newer text; see MessageStore.ApplyEdit.
--
-- message_edits.status: applied (became the text), superseded (genuine but
-- older than the text in place), rejected (not sent by the message's author:
-- anyone in a chat can send an edit naming any message ID), pending (the
-- message is not stored yet; checked against its author when it arrives).
--
-- The statements below are idempotent. The bridge runs them on start
-- (editsCallsSQL in internal/database/edits_calls.go, kept identical to this
-- file by TestEditsCallsSQLMatchesMigration).

CREATE TABLE IF NOT EXISTS message_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    chat_jid TEXT NOT NULL,
    edit_id TEXT NOT NULL,
    old_content TEXT,
    new_content TEXT NOT NULL,
    edited_at TIMESTAMP NOT NULL,
    editor_jid TEXT,
    source TEXT NOT NULL DEFAULT 'live',
    status TEXT NOT NULL DEFAULT 'applied',
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (chat_jid, message_id, edit_id)
);

CREATE INDEX IF NOT EXISTS idx_message_edits_message ON message_edits(message_id, chat_jid);

CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY,
    chat_jid TEXT NOT NULL DEFAULT '',
    caller_jid TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT 'unknown',
    is_video INTEGER NOT NULL DEFAULT 0,
    is_group INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMP NOT NULL,
    accepted_at TIMESTAMP,
    ended_at TIMESTAMP,
    duration_seconds INTEGER,
    outcome TEXT NOT NULL DEFAULT 'unknown',
    end_reason TEXT,
    source TEXT NOT NULL DEFAULT 'live',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_calls_chat_started ON calls(chat_jid, started_at);

CREATE INDEX IF NOT EXISTS idx_calls_started ON calls(started_at);

-- Calls the bridge logged before this table existed live on as "call-<id>"
-- pseudo-messages. Their text cannot say whether a call was answered (the old
-- terminate handler wrote "Call with X (M:SS)" for answered and unanswered
-- alike), so only an explicit "Missed call" becomes missed; the rest stay unknown.
-- Those rows stored the caller as a bare number; in a 1:1 chat that is the chat.
INSERT OR IGNORE INTO calls (call_id, chat_jid, caller_jid, direction, is_video, is_group, started_at, outcome, source)
SELECT substr(id, 6), chat_jid,
       CASE WHEN is_from_me THEN ''
            WHEN instr(COALESCE(sender, ''), '@') = 0 AND chat_jid NOT LIKE '%@g.us' THEN chat_jid
            ELSE COALESCE(sender, '') END,
       CASE WHEN is_from_me THEN 'outgoing' ELSE 'incoming' END,
       CASE WHEN content LIKE '%video call%' THEN 1 ELSE 0 END,
       CASE WHEN chat_jid LIKE '%@g.us' THEN 1 ELSE 0 END,
       timestamp,
       CASE WHEN content LIKE '%Missed call%' THEN 'missed' ELSE 'unknown' END,
       'message'
FROM messages
WHERE id LIKE 'call-%' AND media_type = 'call' AND timestamp IS NOT NULL;

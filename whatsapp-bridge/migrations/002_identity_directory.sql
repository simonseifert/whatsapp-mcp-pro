-- Migration 002: unified identity directory
-- Target: store/messages.db
-- Date: 2026-08-20
-- Backward compatible: YES (only adds)
--
-- The bridge creates this table itself on startup (see internal/database/identities.go).
-- This file exists so an existing store can be brought up to date without restarting
-- the bridge — e.g. so the MCP server can read the new table first.
--
-- Populating it needs the whatsmeow contact store and LID map, which only the bridge
-- can read, so the table stays empty until the bridge next runs its identity sync.

-- ============================================================================
-- PART 1: CHAT MERGE BREADCRUMB
-- ============================================================================
-- Set when a chat has been folded into another identity's chat (a LID chat into
-- its phone-JID twin). The row is kept rather than deleted so an old LID still
-- resolves. Readers listing chats should skip rows where this is set.
--
-- NOTE: SQLite has no "ADD COLUMN IF NOT EXISTS", so re-running this file on a
-- store that already has the column errors with "duplicate column name". That
-- is harmless — the bridge's own startup migration path (runMigrations in
-- internal/database/store.go) swallows that specific error, so live startup is
-- always idempotent. Only a manual re-run of this .sql will surface it; skip
-- this statement if the column already exists.
ALTER TABLE chats ADD COLUMN merged_into TEXT;

-- ============================================================================
-- PART 2: IDENTITY DIRECTORY
-- ============================================================================
-- One row per person, group, newsletter or broadcast list, keyed on a canonical
-- JID (the phone JID whenever it is known). Collapses whatsmeow's two rows per
-- person — one under @lid with the push name, one under @s.whatsapp.net with the
-- address-book name — onto a single record.

CREATE TABLE IF NOT EXISTS identities (
    jid            TEXT PRIMARY KEY,  -- canonical JID
    lid            TEXT,              -- {id}@lid, if known
    phone_jid      TEXT,              -- {phone}@s.whatsapp.net, if known
    phone          TEXT,              -- bare digits
    redacted_phone TEXT,              -- "+1********80" for LID-only group members
    kind           TEXT NOT NULL DEFAULT 'user',  -- user | group | newsletter | broadcast
    first_name     TEXT,
    full_name      TEXT,
    push_name      TEXT,
    business_name  TEXT,
    nickname       TEXT,              -- user-defined, mirrors contact_nicknames
    display_name   TEXT,              -- resolved best name
    chat_name      TEXT,              -- name on the chat row / group subject
    is_business    BOOLEAN DEFAULT 0,
    is_contact     BOOLEAN DEFAULT 0, -- present in the phone's address book
    has_chat       BOOLEAN DEFAULT 0,
    message_count  INTEGER DEFAULT 0,
    participant_count INTEGER,        -- groups only
    is_announce    BOOLEAN DEFAULT 0, -- groups only: admins-only posting
    owner_jid      TEXT,              -- groups only: creator
    first_seen     TIMESTAMP,
    last_seen      TIMESTAMP,
    sources        TEXT,              -- contacts,lid_map,chats,messages,nicknames,groups
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Deliberately non-unique: a duplicate lid means the LID map disagrees with itself,
-- which is worth logging, not worth failing every upsert in a long-running daemon.
CREATE INDEX IF NOT EXISTS idx_identities_lid ON identities(lid);
CREATE INDEX IF NOT EXISTS idx_identities_phone ON identities(phone);
CREATE INDEX IF NOT EXISTS idx_identities_phone_jid ON identities(phone_jid);
CREATE INDEX IF NOT EXISTS idx_identities_display_name ON identities(display_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_identities_kind_last_seen ON identities(kind, last_seen DESC);

-- Migration 013: Community links and own admin status on identities
-- Target: store/messages.db
-- Date: 2026-11-21
-- Backward compatible: YES (only adds columns with defaults)
--
-- Lets a chat app group subgroups under their Community and know whether the
-- linked account may admin a group. community_jid is the parent community of a
-- subgroup (NULL when none); is_community marks the parent itself;
-- is_default_subgroup marks the community announcement subgroup; i_am_admin is
-- set when the linked account is an admin or super admin. Mirrors the ALTER
-- entries in runMigrations (internal/database/store.go), which the bridge runs
-- at startup; the duplicate-column check there makes them idempotent.

ALTER TABLE identities ADD COLUMN is_community INTEGER NOT NULL DEFAULT 0;
ALTER TABLE identities ADD COLUMN community_jid TEXT;
ALTER TABLE identities ADD COLUMN is_default_subgroup INTEGER NOT NULL DEFAULT 0;
ALTER TABLE identities ADD COLUMN i_am_admin INTEGER NOT NULL DEFAULT 0;

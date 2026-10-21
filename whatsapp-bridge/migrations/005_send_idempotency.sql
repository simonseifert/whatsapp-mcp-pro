-- Migration 005: Send idempotency keys + small bridge key/value state
-- Target: store/messages.db
-- Date: 2026-10-22
-- Backward compatible: YES (only adds new tables)
--
-- A send that is slow or times out on the client side used to be retried by the
-- caller, and the retry sent the message a second time. Callers can now pass an
-- idempotency key; the bridge records it here and answers a repeat with the
-- stored response instead of sending again. See internal/database/send_state.go.
--
-- Keys are scoped by endpoint + recipient (scope), so the same key string used
-- for two different recipients is two different keys. payload_hash detects a key
-- reused for a different payload (rejected with 422). message_id is kept even on
-- failure so a retry after an ambiguous timeout re-sends with the SAME WhatsApp
-- message ID, which recipients' devices de-duplicate. Rows expire after 24h.
--
-- The bridge applies this itself on start (createSendStateTables); the file is
-- here for running against a store by hand.

CREATE TABLE IF NOT EXISTS idempotency_keys (
    idem_key TEXT NOT NULL,
    scope TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    http_status INTEGER NOT NULL DEFAULT 0,
    response TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (idem_key, scope)
);

CREATE INDEX IF NOT EXISTS idx_idempotency_keys_created_at ON idempotency_keys(created_at);

-- Small persisted bridge state, e.g. reachout_timelock_until (unix seconds) so a
-- WhatsApp "reachout timelock" (error 463) survives a restart.
CREATE TABLE IF NOT EXISTS bridge_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

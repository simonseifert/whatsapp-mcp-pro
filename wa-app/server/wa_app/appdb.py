"""app.db: the app's own state. Never touches the bridge's messages.db."""

import sqlite3
import threading

from .config import Settings

# app.db is one shared connection used from the event loop and from worker threads: take this around its use there.
db_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    user_agent TEXT
);
CREATE TABLE IF NOT EXISTS login_failures (ip TEXT NOT NULL, at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS login_failures_ip_at ON login_failures (ip, at);
CREATE TABLE IF NOT EXISTS read_state (chat_jid TEXT PRIMARY KEY, read_at INTEGER, opened_at INTEGER);
CREATE TABLE IF NOT EXISTS transcripts (
    message_id TEXT NOT NULL,
    chat_jid TEXT NOT NULL,
    text TEXT,
    language TEXT,
    created_at INTEGER,
    PRIMARY KEY (message_id, chat_jid)
);
CREATE TABLE IF NOT EXISTS send_keys (client_key TEXT PRIMARY KEY, recipient TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS media_retries (message_id TEXT PRIMARY KEY, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ai_backends (id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('openai','anthropic','claude_cli')),
    label TEXT NOT NULL, base_url TEXT, model TEXT NOT NULL, key_enc BLOB);
CREATE TABLE IF NOT EXISTS chat_ai (chat_jid TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, backend_id INTEGER,
    local_only INTEGER NOT NULL DEFAULT 0, note TEXT, rules TEXT, rules_edited_at INTEGER,
    outcomes_since_distill INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS drafts (id INTEGER PRIMARY KEY, chat_jid TEXT NOT NULL, trigger_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','ready','failed','dismissed','superseded','expired','done')),
    alternatives TEXT, error TEXT, backend_id INTEGER, created_at INTEGER NOT NULL, ready_at INTEGER,
    source TEXT NOT NULL DEFAULT 'ai');
CREATE INDEX IF NOT EXISTS drafts_chat ON drafts(chat_jid, created_at);
CREATE TABLE IF NOT EXISTS draft_outcomes (draft_id INTEGER PRIMARY KEY, chat_jid TEXT NOT NULL, sent_id TEXT NOT NULL,
    sent_text TEXT NOT NULL, best_alternative TEXT, ratio REAL NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('used','edited','ignored')), thumb INTEGER, feedback TEXT, at INTEGER NOT NULL);
"""


def migrate(conn: sqlite3.Connection) -> None:
    """Columns added after a table first shipped (CREATE TABLE IF NOT EXISTS does not add them)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(read_state)")}
    if columns and "opened_at" not in columns:
        conn.execute("ALTER TABLE read_state ADD COLUMN opened_at INTEGER")
    draft_columns = {row[1] for row in conn.execute("PRAGMA table_info(drafts)")}
    if draft_columns and "source" not in draft_columns:  # 'ai' (written by the worker) or 'agent' (pushed by Claude)
        conn.execute("ALTER TABLE drafts ADD COLUMN source TEXT NOT NULL DEFAULT 'ai'")


def connect(settings: Settings) -> sqlite3.Connection:
    """Open (creating if needed) app.db in WAL mode with the schema applied."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.data_dir / "app.db", check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn

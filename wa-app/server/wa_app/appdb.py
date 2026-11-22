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
CREATE TABLE IF NOT EXISTS read_state (chat_jid TEXT PRIMARY KEY, read_at INTEGER);
CREATE TABLE IF NOT EXISTS transcripts (
    message_id TEXT NOT NULL,
    chat_jid TEXT NOT NULL,
    text TEXT,
    language TEXT,
    created_at INTEGER,
    PRIMARY KEY (message_id, chat_jid)
);
CREATE TABLE IF NOT EXISTS media_retries (message_id TEXT PRIMARY KEY, at INTEGER NOT NULL);
"""


def connect(settings: Settings) -> sqlite3.Connection:
    """Open (creating if needed) app.db in WAL mode with the schema applied."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.data_dir / "app.db", check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn

"""AI backends, the global default and per-chat settings in app.db. Keys go in encrypted and never come out."""

import sqlite3
import time
from dataclasses import dataclass

from ..config import Settings
from . import secrets

KINDS = ("openai", "anthropic", "claude_cli")
MAX_NOTE = 500
MAX_RULES = 2000
DEFAULT_KEY = "ai_default_backend"


class ConfigError(ValueError):
    """A request the user can fix: shown as a 422. Messages never contain key text."""


class NotFound(LookupError):
    pass


@dataclass
class BackendCfg:
    id: int
    kind: str
    label: str
    base_url: str | None
    model: str
    has_key: bool


@dataclass
class ChatAi:
    chat_jid: str
    enabled: bool
    backend_id: int | None
    local_only: bool
    note: str | None
    rules: str | None


_BACKEND_COLS = "id, kind, label, base_url, model, key_enc IS NOT NULL"


def _backend(row: tuple) -> BackendCfg:
    return BackendCfg(row[0], row[1], row[2], row[3], row[4], bool(row[5]))


def list_backends(conn: sqlite3.Connection) -> list[BackendCfg]:
    return [_backend(r) for r in conn.execute(f"SELECT {_BACKEND_COLS} FROM ai_backends ORDER BY id")]


def get_backend(conn: sqlite3.Connection, backend_id: int) -> BackendCfg | None:
    row = conn.execute(f"SELECT {_BACKEND_COLS} FROM ai_backends WHERE id=?", (backend_id,)).fetchone()
    return _backend(row) if row else None


def backend_secret(conn: sqlite3.Connection, settings: Settings, backend_id: int) -> str | None:
    row = conn.execute("SELECT key_enc FROM ai_backends WHERE id=?", (backend_id,)).fetchone()
    return secrets.decrypt(settings, row[0]) if row and row[0] is not None else None


def _text(body: dict, field: str, *, required: bool = False, limit: int = 200) -> str | None:
    value = body.get(field)
    if value is None or value == "":
        if required:
            raise ConfigError(f"'{field}' is required")
        return None
    if not isinstance(value, str):
        raise ConfigError(f"'{field}' must be text")
    value = value.strip()
    if len(value) > limit:
        raise ConfigError(f"'{field}' is too long")
    if required and not value:
        raise ConfigError(f"'{field}' is required")
    return value or None


def save_backend(conn: sqlite3.Connection, settings: Settings, body: dict) -> BackendCfg:
    """Create, or update when `id` is given. `api_key` omitted keeps the stored key, "" clears it.
    Changing `kind` or `base_url` without a new `api_key` also clears it."""
    kind = body.get("kind")
    if kind not in KINDS:
        raise ConfigError("Unknown backend kind")
    label = _text(body, "label", required=True)
    model = _text(body, "model", required=True)
    base_url = _text(body, "base_url", limit=500)
    if kind == "openai" and not base_url:
        raise ConfigError("'base_url' is required for an OpenAI-compatible backend")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ConfigError("'base_url' must start with http:// or https://")
    api_key = body.get("api_key")
    if api_key is not None and not isinstance(api_key, str):
        raise ConfigError("'api_key' must be text")
    backend_id = body.get("id")
    if backend_id is not None:
        if not isinstance(backend_id, int) or isinstance(backend_id, bool):
            raise ConfigError("'id' must be a number")
        current = get_backend(conn, backend_id)
        if current is None:
            raise NotFound("Backend not found")
        conn.execute(
            "UPDATE ai_backends SET kind=?, label=?, base_url=?, model=? WHERE id=?",
            (kind, label, base_url, model, backend_id),
        )
        if api_key is not None:
            blob = secrets.encrypt(settings, api_key.strip()) if api_key.strip() else None
            conn.execute("UPDATE ai_backends SET key_enc=? WHERE id=?", (blob, backend_id))
        elif kind != current.kind or base_url != current.base_url:
            # A saved key must not follow a new address: it would be sent to whoever runs it.
            conn.execute("UPDATE ai_backends SET key_enc=NULL WHERE id=?", (backend_id,))
    else:
        blob = secrets.encrypt(settings, api_key.strip()) if api_key and api_key.strip() else None
        cur = conn.execute(
            "INSERT INTO ai_backends (kind, label, base_url, model, key_enc) VALUES (?,?,?,?,?)",
            (kind, label, base_url, model, blob),
        )
        backend_id = cur.lastrowid
    saved = get_backend(conn, backend_id)  # type: ignore[arg-type]
    assert saved is not None
    return saved


def delete_backend(conn: sqlite3.Connection, backend_id: int) -> None:
    """Chats that used it fall back to the default; if it was the default, there is none."""
    conn.execute("DELETE FROM ai_backends WHERE id=?", (backend_id,))
    conn.execute("UPDATE chat_ai SET backend_id=NULL WHERE backend_id=?", (backend_id,))
    if default_backend_id(conn) is None:
        conn.execute("DELETE FROM meta WHERE key=?", (DEFAULT_KEY,))


def default_backend_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT b.id FROM meta m JOIN ai_backends b ON b.id = CAST(m.value AS INTEGER) WHERE m.key=?", (DEFAULT_KEY,)
    ).fetchone()
    return row[0] if row else None


def set_default_backend(conn: sqlite3.Connection, backend_id: int) -> None:
    if get_backend(conn, backend_id) is None:
        raise NotFound("Backend not found")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (DEFAULT_KEY, str(backend_id)))


def chat_ai(conn: sqlite3.Connection, jid: str) -> ChatAi:
    row = conn.execute(
        "SELECT enabled, backend_id, local_only, note, rules FROM chat_ai WHERE chat_jid=?", (jid,)
    ).fetchone()
    if row is None:
        return ChatAi(jid, False, None, False, None, None)
    return ChatAi(jid, bool(row[0]), row[1], bool(row[2]), row[3], row[4])


def _flag(body: dict, field: str, current: bool) -> bool:
    if field not in body:
        return current
    if not isinstance(body[field], bool):
        raise ConfigError(f"'{field}' must be true or false")
    return body[field]


def save_chat_ai(conn: sqlite3.Connection, jid: str, body: dict) -> ChatAi:
    """Partial update: fields missing from `body` keep their value. A changed `rules` stamps rules_edited_at."""
    cur = chat_ai(conn, jid)
    enabled = _flag(body, "enabled", cur.enabled)
    local_only = _flag(body, "local_only", cur.local_only)
    backend_id = cur.backend_id
    if "backend_id" in body:
        backend_id = body["backend_id"]
        if backend_id is not None and (
            not isinstance(backend_id, int) or isinstance(backend_id, bool) or get_backend(conn, backend_id) is None
        ):
            raise ConfigError("Unknown backend")
    note = _text(body, "note", limit=MAX_NOTE) if "note" in body else cur.note
    rules = _text(body, "rules", limit=MAX_RULES) if "rules" in body else cur.rules
    edited_at = int(time.time()) if rules != cur.rules else None
    conn.execute("INSERT OR IGNORE INTO chat_ai (chat_jid) VALUES (?)", (jid,))
    conn.execute(
        "UPDATE chat_ai SET enabled=?, backend_id=?, local_only=?, note=?, rules=?,"
        " rules_edited_at=COALESCE(?, rules_edited_at) WHERE chat_jid=?",
        (int(enabled), backend_id, int(local_only), note, rules, edited_at, jid),
    )
    return chat_ai(conn, jid)


def effective_backend(conn: sqlite3.Connection, jid: str) -> BackendCfg | None:
    """The chat's own backend if it still exists, else the global default."""
    chosen = chat_ai(conn, jid).backend_id
    if chosen is not None and (own := get_backend(conn, chosen)) is not None:
        return own
    default = default_backend_id(conn)
    return get_backend(conn, default) if default is not None else None

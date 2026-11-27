"""App-wide draft context: how much chat a draft reads, how many own replies it learns from, and About me."""

import sqlite3
from dataclasses import dataclass

from .config import ConfigError

MESSAGES_RANGE = (10, 100)
EXAMPLES_RANGE = (0, 100)
MAX_ABOUT = 1500
_KEYS = {
    "context_messages": "ai_context_messages",
    "own_examples": "ai_own_examples",
    "about_me": "ai_about_me",
}


@dataclass(frozen=True)
class DraftContext:
    context_messages: int = 30
    own_examples: int = 30
    about_me: str = ""


def get_context(conn: sqlite3.Connection) -> DraftContext:
    stored = dict(conn.execute("SELECT key, value FROM meta WHERE key IN (?,?,?)", tuple(_KEYS.values())))
    base = DraftContext()
    try:
        return DraftContext(
            int(stored.get(_KEYS["context_messages"], base.context_messages)),
            int(stored.get(_KEYS["own_examples"], base.own_examples)),
            stored.get(_KEYS["about_me"]) or base.about_me,
        )
    except ValueError:
        return base


def _number(body: dict, field: str, low: int, high: int) -> int | None:
    if field not in body:
        return None
    value = body[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"'{field}' must be a number")
    if not low <= value <= high:
        raise ConfigError(f"'{field}' must be between {low} and {high}")
    return value


def save_context(conn: sqlite3.Connection, body: dict) -> DraftContext:
    """Partial update: fields missing from `body` keep their value. Nothing is stored when any field is bad."""
    updates: dict[str, str] = {}
    if (n := _number(body, "context_messages", *MESSAGES_RANGE)) is not None:
        updates["context_messages"] = str(n)
    if (n := _number(body, "own_examples", *EXAMPLES_RANGE)) is not None:
        updates["own_examples"] = str(n)
    if "about_me" in body:
        about = body["about_me"]
        if not isinstance(about, str):
            raise ConfigError("'about_me' must be text")
        about = about.strip()
        if len(about) > MAX_ABOUT:
            raise ConfigError(f"'about_me' can be at most {MAX_ABOUT} characters")
        updates["about_me"] = about
    for field, value in updates.items():
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (_KEYS[field], value))
    return get_context(conn)

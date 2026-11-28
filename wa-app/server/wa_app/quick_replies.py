"""Quick replies: a short name that expands to a saved text in the composer. They never send by themselves."""

import re
import sqlite3
from typing import Any

MAX_TEXT = 4096
_SHORTCUT = re.compile(r"[a-z0-9_-]{1,24}")


class QuickReplyError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _clean_text(raw: object) -> str:
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise QuickReplyError(422, "A quick reply needs some text")
    if len(text) > MAX_TEXT:
        raise QuickReplyError(422, f"A quick reply can be at most {MAX_TEXT} characters")
    return text


def list_all(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT shortcut, text, used FROM quick_replies ORDER BY used DESC, shortcut")
    return [{"shortcut": s, "text": t, "used": u} for s, t, u in rows]


def create(conn: sqlite3.Connection, shortcut: object, text: object) -> dict[str, Any]:
    if not isinstance(shortcut, str) or not _SHORTCUT.fullmatch(shortcut):
        raise QuickReplyError(422, "A shortcut is 1 to 24 characters: a-z, 0-9, _ or -")
    clean = _clean_text(text)
    if conn.execute("SELECT 1 FROM quick_replies WHERE shortcut = ?", (shortcut,)).fetchone():
        raise QuickReplyError(409, "You already have a quick reply with that shortcut")
    conn.execute("INSERT INTO quick_replies (shortcut, text) VALUES (?, ?)", (shortcut, clean))
    return {"shortcut": shortcut, "text": clean, "used": 0}


def update(conn: sqlite3.Connection, shortcut: str, text: object) -> None:
    clean = _clean_text(text)
    if conn.execute("UPDATE quick_replies SET text = ? WHERE shortcut = ?", (clean, shortcut)).rowcount == 0:
        raise QuickReplyError(404, "Quick reply not found")


def delete(conn: sqlite3.Connection, shortcut: str) -> None:
    if conn.execute("DELETE FROM quick_replies WHERE shortcut = ?", (shortcut,)).rowcount == 0:
        raise QuickReplyError(404, "Quick reply not found")


def mark_used(conn: sqlite3.Connection, shortcut: str) -> None:
    if conn.execute("UPDATE quick_replies SET used = used + 1 WHERE shortcut = ?", (shortcut,)).rowcount == 0:
        raise QuickReplyError(404, "Quick reply not found")

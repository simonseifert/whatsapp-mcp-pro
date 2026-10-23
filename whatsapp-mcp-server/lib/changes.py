"""Edits, revokes and the call log, as the bridge records them (migration 009).

The bridge writes message_edits, calls and the messages.edited_at /
deleted_at / deleted_by columns; this module only reads them. Every reader
degrades on a store from before that migration: the markers fall back to
"never edited, never deleted" and the two lookups return empty with a note,
because a store can be older than the MCP server reading it.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .naming import resolve_display_names
from .utils import MESSAGES_DB_PATH, canonical_chat_jid, to_sql_utc

EDITED_SUFFIX = " (edited)"
DELETED_PREFIX = "(deleted)"
MAX_CALLS_LIMIT = 500


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def change_columns(conn: sqlite3.Connection, alias: str = "messages") -> str:
    """SQL select fragment yielding ``is_edited, deleted_at`` for a messages row.

    Append it at the end of a positional SELECT so existing indexes stay put.
    Constants stand in for columns the store does not have yet.
    """
    cols = _columns(conn, "messages")
    edited = f"COALESCE({alias}.is_edited, 0)" if "is_edited" in cols else "0"
    deleted = f"{alias}.deleted_at" if "deleted_at" in cols else "NULL"
    return f"{edited}, {deleted}"


def mark_content(content: str | None, edited: Any, deleted_at: Any) -> str | None:
    """Content as an agent should read it: "(deleted) ..." and "... (edited)".

    A revoked message keeps its text in the store (it is the user's archive),
    so the marker is what stops an agent from quoting it as if it still stood.
    A row the bridge created from an edit or revoke alone has no text.
    """
    text = content
    if edited and text:
        text = f"{text}{EDITED_SUFFIX}"
    if deleted_at:
        text = f"{DELETED_PREFIX} {text}" if text else DELETED_PREFIX
    return text


def change_flags(edited: Any, deleted_at: Any) -> dict[str, bool]:
    """``edited`` / ``deleted`` keys to merge into a message dict, only when set."""
    flags: dict[str, bool] = {}
    if edited:
        flags["edited"] = True
    if deleted_at:
        flags["deleted"] = True
    return flags


def chat_forms(conn: sqlite3.Connection, chat_jid: str) -> list[str]:
    """Every chat_jid a conversation may be stored under: merged twin, LID and phone JID."""
    forms = {chat_jid, canonical_chat_jid(conn, chat_jid)}
    try:
        row = conn.execute(
            "SELECT jid, lid, phone_jid FROM identities WHERE ? IN (jid, lid, phone_jid) LIMIT 1",
            (canonical_chat_jid(conn, chat_jid),),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row:
        forms.update(v for v in row if v)
    return sorted(forms)


def get_message_edits(message_id: str, chat_jid: str | None = None, db_path: str | None = None) -> dict[str, Any]:
    """The recorded edit history of one message, oldest edit first.

    Raises ValueError when no such message is stored.
    """
    if not message_id:
        raise ValueError("message_id is required")
    conn = sqlite3.connect(db_path or MESSAGES_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cols = _columns(conn, "messages")
        extra = ", ".join(
            f"m.{c}" if c in cols else f"NULL AS {c}" for c in ("is_edited", "edit_count", "deleted_at", "deleted_by")
        )
        sql = f"SELECT m.id, m.chat_jid, m.content, {extra} FROM messages m WHERE m.id = ?"
        args: list[Any] = [message_id]
        if chat_jid:
            forms = chat_forms(conn, chat_jid)
            sql += f" AND m.chat_jid IN ({','.join('?' * len(forms))})"
            args.extend(forms)
        rows = conn.execute(sql, args).fetchall()
        if not rows:
            raise ValueError(f"Message {message_id} not found" + (f" in {chat_jid}" if chat_jid else ""))
        if len(rows) > 1:
            # IDs are random per sender, so this is a collision between chats.
            chats = ", ".join(r["chat_jid"] for r in rows)
            raise ValueError(f"Message ID {message_id} exists in several chats ({chats}); pass chat_jid")
        msg = rows[0]
        result: dict[str, Any] = {
            "message_id": msg["id"],
            "chat_jid": msg["chat_jid"],
            "content": msg["content"],
            "edited": bool(msg["is_edited"]),
            "edit_count": int(msg["edit_count"] or 0),
            "deleted": bool(msg["deleted_at"]),
            "deleted_at": msg["deleted_at"],
            "deleted_by": msg["deleted_by"],
            "edits": [],
        }
        if not _has_table(conn, "message_edits"):
            result["note"] = "This store predates edit history (bridge migration 009); no edits are recorded."
            return result
        # status arrived after the first cut of migration 009; a store
        # migrated before then has only applied edits.
        status = "status" if "status" in _columns(conn, "message_edits") else "'applied' AS status"
        edits = conn.execute(
            f"SELECT edited_at, old_content, new_content, editor_jid, source, {status} FROM message_edits "
            "WHERE message_id = ? AND chat_jid = ? ORDER BY datetime(edited_at), id",
            (msg["id"], msg["chat_jid"]),
        ).fetchall()
        result["edits"] = [
            {
                "edited_at": e["edited_at"],
                # NULL when the edit arrived after a newer one, so what it
                # replaced is not known.
                "old_content": e["old_content"],
                "new_content": e["new_content"],
                "editor": e["editor_jid"],
                "source": e["source"],
                # rejected: sent by someone other than the author, so never
                # applied; pending: the original is not stored yet.
                "status": e["status"],
            }
            for e in edits
        ]
        rejected = sum(1 for e in result["edits"] if e["status"] == "rejected")
        if rejected:
            result["note"] = (
                f"{rejected} edit(s) were sent by someone other than the author and were not applied; "
                "their text is shown only as a record of the attempt."
            )
        return result
    finally:
        conn.close()


def list_calls(
    chat_jid: str | None = None,
    since: str | None = None,
    limit: int = 50,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Calls the bridge has logged, newest first.

    Raises ValueError on a bad limit or ``since``.
    """
    if limit < 1:
        raise ValueError("limit must be a positive integer")
    limit = min(limit, MAX_CALLS_LIMIT)
    try:
        since_utc = to_sql_utc(since)
    except ValueError as exc:
        raise ValueError(f"since must be an ISO-8601 datetime: {exc}") from exc

    conn = sqlite3.connect(db_path or MESSAGES_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if not _has_table(conn, "calls"):
            return {
                "calls": [],
                "count": 0,
                "note": "This store predates the call log (bridge migration 009).",
            }
        where: list[str] = []
        args: list[Any] = []
        if chat_jid:
            forms = chat_forms(conn, chat_jid)
            where.append(f"k.chat_jid IN ({','.join('?' * len(forms))})")
            args.extend(forms)
        if since_utc:
            where.append("datetime(k.started_at) >= ?")
            args.append(since_utc)
        sql = (
            "SELECT k.call_id, k.chat_jid, k.caller_jid, k.direction, k.is_video, k.is_group, k.started_at, "
            "k.accepted_at, k.ended_at, k.duration_seconds, k.outcome, k.end_reason, k.source, c.name AS chat_name "
            "FROM calls k LEFT JOIN chats c ON c.jid = k.chat_jid"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY datetime(k.started_at) DESC, k.call_id LIMIT ?"
        rows = conn.execute(sql, [*args, limit]).fetchall()
        names = resolve_display_names(conn, {r["chat_jid"] for r in rows} | {r["caller_jid"] for r in rows})
    finally:
        conn.close()

    calls = []
    for r in rows:
        chat_name = names[r["chat_jid"]].name if r["chat_jid"] in names else r["chat_name"]
        call: dict[str, Any] = {
            "call_id": r["call_id"],
            "chat_jid": r["chat_jid"],
            "chat_name": chat_name,
            "caller_jid": r["caller_jid"] or None,
            "direction": r["direction"],
            "video": bool(r["is_video"]),
            "group": bool(r["is_group"]),
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "duration_seconds": r["duration_seconds"],
            "outcome": r["outcome"],
        }
        if r["caller_jid"] in names and r["caller_jid"] != r["chat_jid"]:
            call["caller_name"] = names[r["caller_jid"]].name
        if r["end_reason"]:
            call["end_reason"] = r["end_reason"]
        if r["source"] == "message":
            # Seeded from the old call messages, which could not say whether a
            # call was answered.
            call["note"] = "imported from the pre-call-log history; outcome may be unknown"
        calls.append(call)
    return {"calls": calls, "count": len(calls)}

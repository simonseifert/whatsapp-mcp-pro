"""Outward actions through the bridge. Task 6: read receipts. (Task 7 adds sending and reactions here.)

A read receipt is visible to the other person (blue ticks), so only messages that are actually unread by our own
marker get one: incoming, not system, not deleted, never an `import-` id, at most RECEIPT_BATCH ids per call.
"""

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass

from . import appdb, mcp_lib, store, unread
from .config import Settings

logger = logging.getLogger("wa_app.sending")

RECEIPT_BATCH = 50
RECEIPT_CAP = 200  # per read: only the newest this many get a receipt (a long-unread chat is not a flood)
_RECEIPT_SUFFIXES = ("@s.whatsapp.net", "@lid", "@g.us")  # no receipts for newsletters or status

# Held while a read is decided and recorded, so two quick calls cannot claim the same messages twice.
_claim_lock = threading.Lock()

# (chat jid the messages are stored under, sender jid for groups, message ids)
Batch = tuple[str, str | None, list[str]]


@dataclass
class ReadResult:
    chat: store.ChatSummary | None  # None: no such chat, or it vanished right after the read
    receipts_sent: int = 0
    found: bool = True


def _unread_rows(
    ro: sqlite3.Connection, forms: list[str], marker_bound: str | None, history_unread: int | None
) -> list[tuple[str, str, str | None]]:
    """(id, chat_jid, sender) of the incoming unread messages, oldest first."""
    cols = store._columns(ro, "messages")
    deleted = "AND deleted_at IS NULL" if "deleted_at" in cols else ""
    marks = ",".join("?" * len(forms))
    where = (
        f"chat_jid IN ({marks}) AND COALESCE(is_from_me, 0) = 0 AND COALESCE(is_system_message, 0) = 0 {deleted}"
        " AND substr(id, 1, 7) <> 'import-' AND julianday(timestamp) IS NOT NULL"
        " AND (content IS NULL OR content NOT LIKE '[Poll Vote:%')"
    )
    args: list[object] = list(forms)
    if marker_bound is not None:
        sql = (
            f"SELECT id, chat_jid, sender FROM messages WHERE {where}"
            " AND timestamp >= date(?, '-1 day') AND datetime(timestamp) > datetime(?) ORDER BY julianday(timestamp)"
        )
        args += [marker_bound, marker_bound]
    else:
        # No marker anywhere: history sync said how many are unread; they are the newest incoming ones.
        sql = f"SELECT id, chat_jid, sender FROM messages WHERE {where} ORDER BY julianday(timestamp) DESC LIMIT ?"
        args.append(max(history_unread or 0, 0))
    rows = ro.execute(sql, args).fetchall()
    return rows if marker_bound is not None else rows[::-1]


def _batches(rows: list[tuple[str, str, str | None]]) -> list[Batch]:
    grouped: dict[tuple[str, str | None], list[str]] = {}
    for mid, chat, sender in rows:
        if not chat.endswith(_RECEIPT_SUFFIXES):
            continue
        if chat.endswith("@g.us"):
            if not sender:
                logger.info("no receipt for %s: group message without a sender", mid)
                continue
            key: tuple[str, str | None] = (chat, sender)
        else:
            key = (chat, None)
        grouped.setdefault(key, []).append(mid)
    return [
        (chat, sender, ids[i : i + RECEIPT_BATCH])
        for (chat, sender), ids in grouped.items()
        for i in range(0, len(ids), RECEIPT_BATCH)
    ]


def _claim(ro: sqlite3.Connection, appconn: sqlite3.Connection, jid: str) -> list[Batch] | None:
    """Record the chat as read in app.db and return the receipts to send; None when the chat is unknown."""
    with _claim_lock, appdb.db_lock:
        found = unread.conversation(ro, jid)
        if found is None:
            return None
        conv, state = found
        forms = [conv.jid, *conv.forms]
        newest = ro.execute(
            f"SELECT MAX(CAST(strftime('%s', timestamp) AS INTEGER)) FROM messages"
            f" WHERE chat_jid IN ({','.join('?' * len(forms))})",
            forms,
        ).fetchone()[0]
        if newest is None:
            return None
        marker = unread.UnreadCounter(ro, appconn).marker(forms, state.last_read_at)
        rows = _unread_rows(ro, conv.forms, marker.isoformat(sep=" ") if marker else None, state.history_unread)
        # Local state first: a bridge that is down must not keep the chat unread.
        appconn.execute(
            "INSERT INTO read_state (chat_jid, read_at, opened_at) VALUES (?, ?, ?)"
            " ON CONFLICT(chat_jid) DO UPDATE SET read_at = MAX(COALESCE(read_at, 0), excluded.read_at),"
            " opened_at = excluded.opened_at",
            (conv.jid, int(newest), int(time.time())),
        )
        if len(rows) > RECEIPT_CAP:
            logger.info("read of %s: %d unread, receipts for the newest %d only", conv.jid, len(rows), RECEIPT_CAP)
            rows = rows[-RECEIPT_CAP:]
        return _batches(rows)


def _send(batches: list[Batch]) -> int:
    sent = 0
    for chat, sender, ids in batches:
        try:
            result = mcp_lib.mark_messages_read(chat, ids, sender)
        except Exception:
            logger.exception("read receipts for %s failed", chat)
            continue
        if result.get("success"):
            sent += len(ids)
        else:
            logger.warning("read receipts for %s refused: %s", chat, result.get("error"))
    return sent


def mark_read(settings: Settings, appconn: sqlite3.Connection, jid: str) -> ReadResult:
    """Blocking: mark the chat read in the app, tell the bridge, return the chat as the list now shows it."""
    ro = store.open_ro(settings)
    try:
        batches = _claim(ro, appconn, jid)
        if batches is None:
            return ReadResult(None, found=False)
        sent = _send(batches)
        with appdb.db_lock:
            return ReadResult(store.get_chat(ro, appconn, jid), sent)
    finally:
        ro.close()

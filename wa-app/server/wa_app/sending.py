"""Outward actions through the bridge: read receipts, text sends (with reply quotes) and reactions.

A send never reports success unless the bridge did. Bridge failures are mapped from the structured result
(http_status / code / retryable) to fixed copy, never by matching English error text where a code exists.

A read receipt is visible to the other person (blue ticks), so only messages that are actually unread by our own
marker get one: incoming, not system, not deleted, never an `import-` id, at most RECEIPT_BATCH ids per call.
"""

import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from . import appdb, mcp_lib, store, unread
from .ai import learning
from .config import Settings
from .logsafe import log_safe

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
    ro: sqlite3.Connection, forms: list[str], marker_bound: str | None, history_unread: int | None, up_to: datetime
) -> list[tuple[str, str, str | None]]:
    """(id, chat_jid, sender) of the incoming unread messages, oldest first."""
    cols = store._columns(ro, "messages")
    deleted = "AND deleted_at IS NULL" if "deleted_at" in cols else ""
    marks = ",".join("?" * len(forms))
    where = (
        f"chat_jid IN ({marks}) AND COALESCE(is_from_me, 0) = 0 AND COALESCE(is_system_message, 0) = 0 {deleted}"
        " AND substr(id, 1, 7) <> 'import-' AND julianday(timestamp) IS NOT NULL"
        f" AND {unread.NOT_POLL_VOTE} AND julianday(timestamp) <= julianday(?)"
    )
    args: list[object] = [*forms, up_to.isoformat(sep=" ")]
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
                logger.info("no receipt for %s: group message without a sender", log_safe(mid))
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


def _claim(ro: sqlite3.Connection, appconn: sqlite3.Connection, jid: str, up_to: datetime) -> list[Batch] | None:
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
        newest = min(int(newest), int(up_to.timestamp()))  # never past what the client showed
        marker = unread.UnreadCounter(ro, appconn).marker(forms, state.last_read_at)
        rows = _unread_rows(ro, conv.forms, marker.isoformat(sep=" ") if marker else None, state.history_unread, up_to)
        # Local state first: a bridge that is down must not keep the chat unread.
        appconn.execute(
            "INSERT INTO read_state (chat_jid, read_at, opened_at) VALUES (?, ?, ?)"
            " ON CONFLICT(chat_jid) DO UPDATE SET read_at = MAX(COALESCE(read_at, 0), excluded.read_at),"
            " opened_at = excluded.opened_at",
            (conv.jid, newest, int(time.time())),
        )
        if len(rows) > RECEIPT_CAP:
            logger.info(
                "read of %s: %d unread, receipts for the newest %d only", log_safe(conv.jid), len(rows), RECEIPT_CAP
            )
            rows = rows[-RECEIPT_CAP:]
        return _batches(rows)


def _send(batches: list[Batch]) -> int:
    sent = 0
    for chat, sender, ids in batches:
        try:
            result = mcp_lib.mark_messages_read(chat, ids, sender)
        except Exception:
            logger.exception("read receipts for %s failed", log_safe(chat))
            continue
        if result.get("success"):
            sent += len(ids)
        else:
            logger.warning("read receipts for %s refused: %s", log_safe(chat), log_safe(result.get("error")))
    return sent


def mark_read(settings: Settings, appconn: sqlite3.Connection, jid: str, up_to: datetime) -> ReadResult:
    """Blocking: mark the chat read in the app up to `up_to` (the newest message the client showed), tell the bridge, return the chat as the list now shows it."""
    ro = store.open_ro(settings)
    try:
        batches = _claim(ro, appconn, jid, up_to)
        if batches is None:
            return ReadResult(None, found=False)
        sent = _send(batches)
        with appdb.db_lock:
            return ReadResult(store.get_chat(ro, appconn, jid), sent)
    finally:
        ro.close()


# --- sending ------------------------------------------------------------------------------------------------------

MAX_TEXT = 4096
MAX_EMOJI = 16  # code points: room for a ZWJ family or a flag, not for a sentence
MSG_LIMIT = "Not sent: limit"
MSG_PAUSED = "Not sent: new chats are paused for now"
MSG_FORBIDDEN = "Not sent: this chat isn't allowed"
MSG_NO_ANSWER = "Not sent: bridge didn't answer"
_SHORT = 120


@dataclass
class Outcome:
    status: int
    body: dict


def _fail(status: int, message: str, retryable: bool = False) -> Outcome:
    return Outcome(status, {"ok": False, "error": message, "retryable": retryable})


def _bad(message: str, status: int = 422) -> Outcome:
    return Outcome(status, {"error": message})


def valid_key(value: object) -> str | None:
    """The client's compose-action key if it is a UUID, as a canonical string."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _short(result: dict) -> str:
    text = str(result.get("error") or "unknown error")
    if text.startswith("HTTP ") and " - " in text:
        text = text.split(" - ", 1)[1]
    text = " ".join(text.split())
    return text if len(text) <= _SHORT else text[: _SHORT - 1] + "\u2026"


def map_failure(result: dict) -> Outcome:
    """A bridge result with success false -> the line the user sees, from the structured fields."""
    code = result.get("code")
    http = result.get("http_status")
    retryable = bool(result.get("retryable", False))
    if code == "reachout_timelock":
        return _fail(502, MSG_PAUSED)  # waiting is the only fix; a retry would only poke the lock
    if code in ("rate_limited", "send_cap") or http == 429:
        return _fail(502, MSG_LIMIT, retryable)
    if code == "forbidden" or http == 403:
        return _fail(502, MSG_FORBIDDEN)
    if code in ("timeout", "not_connected") or http in (409, 503, 504):  # 409: the same key is still being sent
        return _fail(502, MSG_NO_ANSWER, True)
    if http is None and retryable and code is None:  # _send_request's timeout / dropped connection result
        return _fail(502, MSG_NO_ANSWER, True)
    return _fail(502, f"Not sent: {_short(result)}", retryable)


def _call(fn, *args, **kwargs) -> dict:  # noqa: ANN001
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.exception("bridge call failed")
        return {"success": False, "retryable": True}  # no structured result: shown as "bridge didn't answer"


KEY_TTL = 24 * 3600  # the bridge forgets a key after 24 h


def _recipient_for(appconn: sqlite3.Connection, key: str, recipient: str) -> str:
    now = int(time.time())
    with appdb.db_lock:
        appconn.execute("DELETE FROM send_keys WHERE created_at < ?", (now - KEY_TTL,))
        appconn.execute(
            "INSERT OR IGNORE INTO send_keys (client_key, recipient, created_at) VALUES (?, ?, ?)",
            (key, recipient, now),
        )
        return str(appconn.execute("SELECT recipient FROM send_keys WHERE client_key = ?", (key,)).fetchone()[0])


def send_text(settings: Settings, appconn: sqlite3.Connection, jid: str, body: object) -> Outcome:
    """Blocking: validate, resolve the recipient, send through the bridge with the client's key."""
    if not isinstance(body, dict):
        return _bad("Bad request", 400)
    text, reply_to = body.get("text"), body.get("reply_to")
    key = valid_key(body.get("client_key"))
    if key is None:
        return _bad("client_key must be a UUID")
    if isinstance(text, str):
        text = text.lstrip("\r\n").rstrip()
    if not isinstance(text, str) or not text.strip():
        return _bad("Message is empty")
    if len(text) > MAX_TEXT:
        return _bad("Message too long")
    if reply_to is not None and not isinstance(reply_to, str):
        return _bad("Bad reply_to")
    draft_id = body.get("draft_id")  # a hint for outcome learning; it never changes what is sent
    if draft_id is not None and (not isinstance(draft_id, int) or isinstance(draft_id, bool)):
        return _bad("Bad draft_id")
    ro = store.open_ro(settings)
    try:
        with appdb.db_lock:
            chat = store.get_chat(ro, appconn, jid)
            if chat is None:
                return _bad("Chat not found", 404)
            quoted = None
            if reply_to:
                quoted = store.get_message(ro, chat.jid, reply_to)
                if quoted is None or not quoted.actionable:
                    return _bad("That message can't be replied to")
    finally:
        ro.close()
    # The canonical jid the list uses (the phone form when known), never a bare LID that may be unmapped.
    # A retry keeps the recipient of the first attempt: the bridge scopes the key to endpoint + recipient.
    if draft_id is not None:
        learning.set_hint(chat.jid, draft_id)  # set before the send: the poller may report the message first
    result = _call(
        mcp_lib.send_message,
        _recipient_for(appconn, key, chat.jid),
        text,
        quoted_message_id=quoted.id if quoted else None,
        idempotency_key=key,
    )
    if not result.get("success"):
        if draft_id is not None:
            learning.clear_hint(chat.jid, draft_id)
        return map_failure(result)
    return Outcome(200, {"ok": True, "message_id": result.get("message_id")})


def send_reaction(settings: Settings, appconn: sqlite3.Connection, jid: str, body: object) -> Outcome:
    """Blocking: react to (or with "" un-react) a message of this chat."""
    if not isinstance(body, dict):
        return _bad("Bad request", 400)
    message_id, emoji = body.get("message_id"), body.get("emoji")
    key = valid_key(body.get("client_key"))
    if key is None:
        return _bad("client_key must be a UUID")
    if not isinstance(message_id, str) or not message_id:
        return _bad("message_id required")
    if not isinstance(emoji, str) or len(emoji) > MAX_EMOJI:
        return _bad("Bad emoji")
    ro = store.open_ro(settings)
    try:
        with appdb.db_lock:
            chat = store.get_chat(ro, appconn, jid)
            if chat is None:
                return _bad("Chat not found", 404)
            message = store.get_message(ro, chat.jid, message_id)
    finally:
        ro.close()
    if message is None:
        return _bad("Message not found", 404)
    if not message.actionable:
        return _bad("That message can't be reacted to")
    result = _call(mcp_lib.send_reaction, message.chat_jid, message.id, emoji, idempotency_key=key)
    if not result.get("success"):
        return map_failure(result)
    return Outcome(200, {"ok": True})

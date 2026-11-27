"""Outward actions through the bridge: read receipts, text sends (with reply quotes) and reactions.

A send never reports success unless the bridge did. Bridge failures are mapped from the structured result
(http_status / code / retryable) to fixed copy, never by matching English error text where a code exists.

A read receipt is visible to the other person (blue ticks), so only messages that are actually unread by our own
marker get one: incoming, not system, not deleted, never an `import-` id, at most RECEIPT_BATCH ids per call.
"""

import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from . import appdb, live, mcp_lib, media, store, unread
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
MSG_NOT_PRIVATE = "That message can't be replied to privately"
MSG_NO_DIRECT = "Can't reply privately to this person yet"
MAX_JID_ENCODED = 200  # the percent-encoded X-Reply-Chat header
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


def map_failure(result: dict, private_reply: bool = False) -> Outcome:
    """A bridge result with success false -> the line the user sees, from the structured fields.

    For a private reply the bridge refuses a quote it cannot find in the group (invalid_request, "cannot quote
    message"): that is the 422 copy, never retryable.
    """
    code = result.get("code")
    http = result.get("http_status")
    retryable = bool(result.get("retryable", False))
    if (
        private_reply
        and (code == "invalid_request" or http == 400)
        and "cannot quote message" in str(result.get("error"))
    ):
        return Outcome(422, {"ok": False, "error": MSG_NOT_PRIVATE, "retryable": False})
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


def _target(
    ro: sqlite3.Connection,
    appconn: sqlite3.Connection,
    jid: str,
    reply_to: str | None,
    reply_chat: str | None,
) -> tuple[store.ChatSummary, store.MessageView | None, str | None] | Outcome:
    """The chat to send to, the message to quote and the chat that message lives in when it is not the target's.

    Without `reply_chat` the quote is a message of the target chat. With it (a private reply) the quote is another
    person's message in that group and the target must be that person's direct chat. Call with appdb.db_lock held.
    """
    if reply_chat is None:
        chat = store.get_chat(ro, appconn, jid)
        if chat is None:
            return _bad("Chat not found", 404)
        quoted = None
        if reply_to:
            quoted = store.get_message(ro, chat.jid, reply_to)
            if quoted is None or not quoted.actionable:
                return _bad("That message can't be replied to")
        return chat, quoted, None
    if not reply_chat or not reply_to:
        return _bad(MSG_NOT_PRIVATE)
    group = store.get_chat(ro, appconn, reply_chat)
    if group is None or group.kind not in store.GROUP_KINDS:
        return _bad(MSG_NOT_PRIVATE)
    quoted = store.get_message(ro, group.jid, reply_to)
    if quoted is None or not quoted.actionable or quoted.from_me or quoted.system or not quoted.sender_jid:
        return _bad(MSG_NOT_PRIVATE)
    direct = store.direct_chat_for(ro, appconn, quoted.sender_jid)
    if direct is None:
        return _bad(MSG_NO_DIRECT)
    target = store.direct_chat_for(ro, appconn, jid)
    if target is None or target.jid != direct.jid:
        return _bad(MSG_NOT_PRIVATE)
    return direct, quoted, group.jid


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
    reply_chat = body.get("reply_chat")
    if reply_chat is not None and not isinstance(reply_chat, str):
        return _bad("Bad reply_chat")
    ro = store.open_ro(settings)
    try:
        with appdb.db_lock:
            target = _target(ro, appconn, jid, reply_to, reply_chat)
    finally:
        ro.close()
    if isinstance(target, Outcome):
        return target
    chat, quoted, quoted_chat = target
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
        **({"quoted_chat_jid": quoted_chat} if quoted_chat else {}),
    )
    if not result.get("success"):
        if draft_id is not None:
            learning.clear_hint(chat.jid, draft_id)
        return map_failure(result, private_reply=quoted_chat is not None)
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


# --- photos, files and voice notes --------------------------------------------------------------------------------

MAX_FILENAME = 120  # bytes of UTF-8, extension included
MAX_CAPTION = 1024  # characters
MAX_CAPTION_ENCODED = 4096  # the percent-encoded header, so no proxy limit is hit
UPLOAD_PREFIX = "wa-app-up-"
STALE_UPLOAD_SECONDS = 3600
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_BIDI = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069]")  # can make a name or caption read backwards


def _clip(text: str, limit: int) -> str:
    """At most `limit` bytes of UTF-8, never cutting a character in half."""
    return text.encode()[:limit].decode(errors="ignore")


def clean_caption(caption: str) -> str:
    """Line breaks kept; other control and bidi characters dropped."""
    return _BIDI.sub("", re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", caption)).strip()


def safe_filename(name: str) -> str:
    """A name that is safe on disk and in WhatsApp: no path parts, control or bidi characters, letters kept, 120 bytes."""
    last = name.replace("\\", "/").split("/")[-1]
    cleaned = _BIDI.sub("", _CONTROL.sub("", last)).strip().strip(".")
    if len(cleaned.encode()) > MAX_FILENAME:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and 0 < len(ext.encode()) <= 10:
            cleaned = _clip(stem, MAX_FILENAME - len(ext.encode()) - 1) + "." + ext
        else:
            cleaned = _clip(cleaned, MAX_FILENAME)
    return cleaned or "file"


class StageError(Exception):
    """The upload could not be staged on disk (/tmp full or unwritable)."""


def new_stage(filename: str) -> tuple[str, str]:
    """A private (0700) temp dir and the path inside it the upload is written to, named safe_filename(filename).

    The bridge names a document after the file's basename, so the file must carry the name it should show.
    """
    try:
        directory = tempfile.mkdtemp(prefix=UPLOAD_PREFIX, dir="/tmp")
    except OSError as exc:
        logger.error("no temp dir for an upload: %s", exc.strerror)
        raise StageError from exc
    try:
        os.chmod(directory, 0o700)
    except OSError:
        shutil.rmtree(directory, ignore_errors=True)
        raise StageError from None
    return directory, os.path.join(directory, safe_filename(filename))


def drop_stage(directory: str | None) -> None:
    if directory:
        shutil.rmtree(directory, ignore_errors=True)


def sweep_stale_uploads(now: float | None = None) -> int:
    """Remove upload dirs a crash left behind: ours (this user), older than an hour. Returns how many went."""
    cutoff = (time.time() if now is None else now) - STALE_UPLOAD_SECONDS
    removed = 0
    try:
        entries = list(os.scandir("/tmp"))
    except OSError:
        return 0
    for entry in entries:
        try:
            if not entry.name.startswith(UPLOAD_PREFIX) or entry.is_symlink() or not entry.is_dir():
                continue
            info = entry.stat(follow_symlinks=False)
            if info.st_uid == os.getuid() and info.st_mtime < cutoff:
                shutil.rmtree(entry.path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def send_media(
    settings: Settings,
    appconn: sqlite3.Connection,
    jid: str,
    *,
    path: str,
    caption: str | None,
    reply_to: str | None,
    client_key: str,
    kind: Literal["file", "voice"],
    reply_chat: str | None = None,
) -> Outcome:
    """Blocking: send the already staged file at `path` through the bridge with the client's key.

    The caller owns the staging dir and removes it afterwards, whatever this returns.
    """
    key = valid_key(client_key)
    if key is None:
        return _bad("client_key must be a UUID")
    if caption is not None:
        caption = clean_caption(caption)
        if len(caption) > MAX_CAPTION:
            return _bad("Caption too long")
    ro = store.open_ro(settings)
    try:
        with appdb.db_lock:
            target = _target(ro, appconn, jid, reply_to, reply_chat)
    finally:
        ro.close()
    if isinstance(target, Outcome):
        return target
    chat, quoted, quoted_chat = target
    recipient = _recipient_for(appconn, key, chat.jid)
    quoted_id = quoted.id if quoted else None
    if kind == "voice":
        result = _call(
            mcp_lib.send_audio_message,
            recipient,
            file_path=path,
            quoted_message_id=quoted_id,
            quoted_chat_jid=quoted_chat,
            idempotency_key=key,
        )
    else:
        result = _call(
            mcp_lib.send_file,
            recipient,
            file_path=path,
            filename=os.path.basename(path),
            caption=caption or None,
            quoted_message_id=quoted_id,
            quoted_chat_jid=quoted_chat,
            idempotency_key=key,
        )
    if not result.get("success"):
        return map_failure(result, private_reply=quoted_chat is not None)
    return Outcome(200, {"ok": True, "message_id": result.get("message_id")})


# --- message actions: edit, delete for everyone, star, forward -----------------------------------------------------

EDIT_WINDOW = 15 * 60  # WhatsApp lets you edit a sent text for 15 minutes
REVOKE_WINDOW = 48 * 3600  # and delete it for everyone for 48 hours
MAX_FORWARD_TARGETS = 5
MAX_FORWARD_MESSAGES = 10
TOO_LATE = Outcome(409, {"error": "too_late"})
MSG_NO_DOWNLOAD = "Couldn't download the file to forward"


def _age_seconds(message: store.MessageView) -> float | None:
    stamp = store.parse_ts(message.timestamp_iso)
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return (datetime.now(UTC) - stamp).total_seconds()


def _lookup(
    settings: Settings, appconn: sqlite3.Connection, jid: str, message_id: str
) -> tuple[store.ChatSummary, store.MessageView] | Outcome:
    """The chat and the message in whichever JID form it is stored under (the form the bridge must be given)."""
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
    return chat, message


def _announce_change(message: store.MessageView, chat: store.ChatSummary) -> None:
    live.current_hub().publish("message_changed", {"chat_jid": chat.jid, "id": message.id})


def edit_message(settings: Settings, appconn: sqlite3.Connection, jid: str, message_id: str, body: object) -> Outcome:
    """Blocking: edit one of our own plain texts, at most EDIT_WINDOW seconds after it was sent."""
    if not isinstance(body, dict):
        return _bad("Bad request", 400)
    if valid_key(body.get("client_key")) is None:
        return _bad("client_key must be a UUID")
    text = body.get("text")
    if isinstance(text, str):
        text = text.strip()
    if not isinstance(text, str) or not text:
        return _bad("Message is empty")
    if len(text) > MAX_TEXT:
        return _bad("Message too long")
    found = _lookup(settings, appconn, jid, message_id)
    if isinstance(found, Outcome):
        return found
    chat, message = found
    plain = message.text and not (message.media or message.poll or message.contact_cards)
    if not (message.from_me and message.actionable and not message.system and not message.deleted and plain):
        return _bad("That message can't be edited")
    age = _age_seconds(message)
    if age is None or age > EDIT_WINDOW:
        return TOO_LATE
    result = _call(mcp_lib.edit_message, message.chat_jid, message.id, text)
    if not result.get("success"):
        return map_failure(result)
    _announce_change(message, chat)
    return Outcome(200, {"ok": True})


def revoke_message(settings: Settings, appconn: sqlite3.Connection, jid: str, message_id: str, body: object) -> Outcome:
    """Blocking: delete one of our own messages for everyone, at most REVOKE_WINDOW seconds after it was sent."""
    if not isinstance(body, dict):
        return _bad("Bad request", 400)
    if valid_key(body.get("client_key")) is None:
        return _bad("client_key must be a UUID")
    found = _lookup(settings, appconn, jid, message_id)
    if isinstance(found, Outcome):
        return found
    chat, message = found
    if not (message.from_me and message.actionable and not message.system and not message.deleted):
        return _bad("That message can't be deleted for everyone")
    age = _age_seconds(message)
    if age is None or age > REVOKE_WINDOW:
        return TOO_LATE
    result = _call(mcp_lib.delete_message, message.chat_jid, message.id)
    if not result.get("success"):
        return map_failure(result)
    _announce_change(message, chat)
    return Outcome(200, {"ok": True})


def star_message(settings: Settings, appconn: sqlite3.Connection, jid: str, message_id: str, body: object) -> Outcome:
    """Blocking: star or unstar a message of this chat. The bridge gets the chat and sender forms as stored."""
    if not isinstance(body, dict):
        return _bad("Bad request", 400)
    starred = body.get("star")
    if not isinstance(starred, bool):
        return _bad("star must be true or false")
    found = _lookup(settings, appconn, jid, message_id)
    if isinstance(found, Outcome):
        return found
    chat, message = found
    if not message.actionable or message.system or message.deleted:
        return _bad("That message can't be starred")
    result = _call(
        mcp_lib.star_message,
        message.chat_jid,
        message.id,
        star=starred,
        sender_jid=None if message.from_me else message.sender_jid,
        from_me=message.from_me,
    )
    if not result.get("success"):
        return map_failure(result)
    _announce_change(message, chat)
    return Outcome(200, {"ok": True})


def _forward_key(client_key: str, target: str, message_id: str) -> str:
    """One stable idempotency key per (client key, target chat, message): a retry of the whole forward dedupes."""
    return str(uuid.uuid5(uuid.UUID(client_key), f"{target}|{message_id}"))


def _forward_one(
    settings: Settings,
    appconn: sqlite3.Connection,
    target: store.ChatSummary,
    message: store.MessageView,
    key: str,
    path: str | None,
) -> str | None:
    """Send one message to one chat forwarded; the error line, or None when it went out."""
    recipient = _recipient_for(appconn, key, target.jid)
    if message.media is not None:
        if path is None:
            return MSG_NO_DOWNLOAD
        result = _call(
            mcp_lib.send_file,
            recipient,
            file_path=path,
            filename=os.path.basename(path),
            caption=message.text or None,
            quoted_message_id=None,
            quoted_chat_jid=None,
            idempotency_key=key,
            forwarded=True,
        )
    else:
        result = _call(mcp_lib.send_message, recipient, message.text or "", idempotency_key=key, forwarded=True)
    if result.get("success"):
        return None
    return str(map_failure(result).body["error"])


def forward_messages(settings: Settings, appconn: sqlite3.Connection, body: object) -> Outcome:
    """Blocking: forward messages of one chat to up to MAX_FORWARD_TARGETS chats; one result per target chat.

    Media is downloaded first, once, through the same path as viewing (it never asks the sender's phone). A target
    stops at its first failure so the order is never scrambled, and the error says how far it got.
    """
    if not isinstance(body, dict):
        return _bad("Bad request", 400)
    key = valid_key(body.get("client_key"))
    if key is None:
        return _bad("client_key must be a UUID")
    from_chat, ids, targets = body.get("from_chat"), body.get("message_ids"), body.get("to")
    if not isinstance(from_chat, str) or not from_chat:
        return _bad("from_chat required")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
        return _bad("message_ids required")
    if not isinstance(targets, list) or not targets or not all(isinstance(t, str) and t for t in targets):
        return _bad("to required")
    ids, targets = list(dict.fromkeys(ids)), list(dict.fromkeys(targets))
    if len(ids) > MAX_FORWARD_MESSAGES:
        return _bad(f"At most {MAX_FORWARD_MESSAGES} messages at once")
    if len(targets) > MAX_FORWARD_TARGETS:
        return _bad(f"At most {MAX_FORWARD_TARGETS} chats at once")
    ro = store.open_ro(settings)
    try:
        with appdb.db_lock:
            source = store.get_chat(ro, appconn, from_chat)
            if source is None:
                return _bad("Chat not found", 404)
            messages = [store.get_message(ro, source.jid, mid) for mid in ids]
            chats = {t: store.get_chat(ro, appconn, t) for t in targets}
    finally:
        ro.close()
    if any(m is None for m in messages):
        return _bad("Message not found", 404)
    found = [m for m in messages if m is not None]
    found.sort(key=lambda m: store.parse_ts(m.timestamp_iso) or datetime.min.replace(tzinfo=UTC))
    refused = {m.id for m in found if not m.actionable or m.system or m.deleted or m.poll is not None}
    paths: dict[str, str | None] = {}
    for m in found:  # one download per message, before anything goes out
        if m.id not in refused and m.media is not None:
            try:
                path = media.file_for(settings, m.id, m.chat_jid)
            except Exception:
                logger.exception("download for a forward failed")
                path = None
            paths[m.id] = str(path) if path else None
    results: list[dict] = []
    for to in targets:
        chat = chats[to]
        if chat is None:
            results.append({"to": to, "ok": False, "error": "Chat not found"})
            continue
        error: str | None = None
        sent = 0
        for m in found:
            if m.id in refused:
                error = "This message can't be forwarded"
            else:
                error = _forward_one(settings, appconn, chat, m, _forward_key(key, chat.jid, m.id), paths.get(m.id))
            if error:
                break
            sent += 1
        if error is None:
            results.append({"to": to, "ok": True})
        else:
            if sent:
                error = f"Sent {sent} of {len(found)}, then: {error}"
            results.append({"to": to, "ok": False, "error": error})
    return Outcome(200, {"results": results})

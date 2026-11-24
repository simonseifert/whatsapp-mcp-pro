"""On-demand voice-note transcripts, kept in app.db. Never written to the bridge's messages.content."""

import logging
import sqlite3
import threading
import time
from typing import Any

from . import appdb, mcp_lib
from .config import Settings
from .media import file_for
from .store import open_ro

logger = logging.getLogger("wa_app.transcripts")

NO_BACKEND = "No transcription backend"
NO_AUDIO = "Audio not available"
FAILED = "Transcription failed"
LOCAL_UNAVAILABLE = "Local transcription unavailable"

_db_lock = appdb.db_lock
_transcribe_slot = threading.Semaphore(1)  # whisper is heavy: one transcription at a time
_key_locks: dict[tuple[str, str], threading.Lock] = {}


def _stored(conn: sqlite3.Connection, message_id: str, chat_jid: str) -> dict[str, Any] | None:
    with _db_lock:
        row = conn.execute(
            "SELECT text, language FROM transcripts WHERE message_id=? AND chat_jid=?", (message_id, chat_jid)
        ).fetchone()
    return {"text": row[0] or "", "language": row[1]} if row else None


def get_or_make(conn: sqlite3.Connection, settings: Settings, message_id: str, chat_jid: str) -> dict[str, Any]:
    """{text, language} (stored after the first run) or {error}. Concurrent requests share one transcription."""
    with _db_lock:
        lock = _key_locks.setdefault((message_id, chat_jid), threading.Lock())
    with lock:
        found = _stored(conn, message_id, chat_jid)
        if found is not None:
            return found
        path = file_for(settings, message_id, chat_jid)
        if path is None:
            return {"error": NO_AUDIO}
        try:
            ro = open_ro(settings)
        except sqlite3.Error as exc:
            logger.warning(
                "cannot open the message store to resolve chat aliases (%s): treating as local only", type(exc).__name__
            )
            local_only = True
        else:
            try:
                with _db_lock:  # the flag counts under any form of the chat: LID, phone JID, merged twin
                    local_only = mcp_lib.is_local_only_chat(ro, conn, chat_jid)
            finally:
                ro.close()
        with _transcribe_slot:
            # A chat marked local only never sends its audio to a cloud transcriber, and never falls back to one.
            result = mcp_lib.transcribe_file(str(path), None, local_only=local_only)
        if not result.get("success"):
            logger.warning("transcription failed (%s): %s", result.get("backend"), result.get("message"))
            if local_only:
                return {"error": LOCAL_UNAVAILABLE}
            return {"error": NO_BACKEND if result.get("backend") == "none" else FAILED}
        text = str(result.get("text") or "").strip()
        language = result.get("language")
        with _db_lock:
            conn.execute(
                "INSERT OR REPLACE INTO transcripts (message_id, chat_jid, text, language, created_at) VALUES (?,?,?,?,?)",
                (message_id, chat_jid, text, language, int(time.time())),
            )
        return {"text": text, "language": language}

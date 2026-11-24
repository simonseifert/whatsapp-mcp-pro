"""On-demand voice-note transcripts, kept in app.db. Never written to the bridge's messages.content."""

import logging
import sqlite3
import threading
import time
from typing import Any

from . import appdb, mcp_lib
from .ai.config import chat_ai
from .config import Settings
from .media import file_for

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
        with _db_lock:
            local_only = chat_ai(conn, chat_jid).local_only
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

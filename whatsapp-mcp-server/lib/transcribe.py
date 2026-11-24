"""Pluggable speech-to-text backends for transcription tools + auto pipeline.

Backend selection via WHISPER_BACKEND (default "auto"):
    whisper-server  a whisper.cpp server at WHISPER_SERVER_URL (e.g. on a local
                    GPU box) — fully local, model stays resident, no RAM here
    mlx             mlx-whisper — Apple Silicon only, fully local (~1.5 GB model)
    faster-whisper  CTranslate2 — cross-platform CPU/GPU, fully local
    groq            Groq API (whisper-large-v3-turbo) — needs GROQ_API_KEY,
                    near-zero RAM, ideal for small always-on boxes
    auto            first available in the order above

If the chosen local backend fails and GROQ_API_KEY is set, the file is retried
on Groq, so a stopped GPU server degrades to the cloud instead of to nothing.
TRANSCRIBE_CLOUD_FALLBACK=0 turns that off: Groq is then used only when
WHISPER_BACKEND=groq names it.

Voice notes from a chat marked "local only" in wa-app never go to a cloud
backend, whatever the settings above say (see is_local_only_chat). If no local
backend can handle such a file the call fails with LOCAL_UNAVAILABLE.

The auto-transcribe pipeline (start_auto_transcribe, opt-in via
AUTO_TRANSCRIBE_VOICE=true in the shared server) transcribes incoming voice
notes in the background and writes the text into messages.content, which makes
voice searchable by `recall` and readable in wa-app.

Pro tier — whatsapp-mcp-pro.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .utils import MESSAGES_DB_PATH, log_safe, logger

MLX_MODEL = os.environ.get("MLX_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
FW_MODEL = os.environ.get("FASTER_WHISPER_MODEL", "large-v3-turbo")
GROQ_MODEL = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WHISPER_SERVER_URL = os.environ.get("WHISPER_SERVER_URL", "").rstrip("/")
VOICE_PREFIX = "\U0001f3a4 "  # 🎤 marks auto-transcribed content
LOCAL_UNAVAILABLE = "Local transcription unavailable"

_fw_model = None
_fw_lock = threading.Lock()
_auto_thread: threading.Thread | None = None
_auto_lock = threading.Lock()
_failed: set[tuple[str, str]] = set()
_retry_at: dict[tuple[str, str], float] = {}  # local-only notes that failed: tried again after LOCAL_RETRY_SECONDS
LOCAL_RETRY_SECONDS = 600
LOCAL_MAX_ATTEMPTS = 6  # then the note is given up on, like any other failure
_attempts: dict[tuple[str, str], int] = {}


def cloud_fallback_enabled() -> bool:
    """False when TRANSCRIBE_CLOUD_FALLBACK is 0/false/no/off. Default on, as before."""
    return os.environ.get("TRANSCRIBE_CLOUD_FALLBACK", "1").strip().lower() not in ("0", "false", "no", "off")


def _wa_app_in_use() -> bool:
    """wa-app is set up when WA_APP_AGENT_TOKEN or WA_APP_DATA is set. Then a missing app.db means this process looks
    in the wrong place (set WA_APP_DATA in the shared .env to wa-app's data dir), not that no chat is local only."""
    return bool((os.environ.get("WA_APP_AGENT_TOKEN") or "").strip() or os.environ.get("WA_APP_DATA"))


def _wa_app_db_path() -> Path:
    data = os.environ.get("WA_APP_DATA")
    return (Path(data).expanduser() if data else Path.home() / ".local" / "share" / "wa-app") / "app.db"


def _optional(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Rows, or none when the store simply predates the table or column. Any other error propagates."""
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError as exc:
        if str(exc).startswith("no such"):
            return []
        raise


def chat_forms_with_shells(conn: sqlite3.Connection, chat_jid: str) -> set[str]:
    """Every form the conversation may be filed under: the JID itself, its canonical (merged) chat,
    its LID and phone twins, and chats that were merged into it.

    Raises sqlite3.Error when messages.db or its identity or merge mappings cannot be read: the caller must
    not guess a smaller set."""
    forms = {chat_jid}
    current = chat_jid
    for _ in range(8):  # follow chats.merged_into like canonical_chat_jid
        rows = _optional(conn, "SELECT merged_into FROM chats WHERE jid = ?", (current,))
        nxt = rows[0][0] if rows and rows[0][0] else None
        if not nxt or nxt in forms:
            break
        forms.add(nxt)
        current = nxt
    for row in _optional(
        conn,
        "SELECT jid, lid, phone_jid FROM identities WHERE ? IN (jid, lid, phone_jid)",
        (current,),
    ):
        forms.update(v for v in row if v)
    marks = ",".join("?" * len(forms))
    forms.update(
        r[0] for r in _optional(conn, f"SELECT jid FROM chats WHERE merged_into IN ({marks})", tuple(sorted(forms)))
    )
    return forms


def is_local_only_chat(
    chat_jid: str,
    *,
    messages_conn: sqlite3.Connection | None = None,
    app_conn: sqlite3.Connection | None = None,
) -> bool:
    """True when wa-app marks the chat, under ANY of its forms, local only (its app.db, read-only).

    The JID is canonicalised against messages.db first, as download_media does, and the flag counts
    when it sits on any alias or twin (LID, phone JID, merged chat): a note arriving under one form
    is not a way around a flag set on another.

    If the aliases cannot be resolved (messages.db, identities or merge mappings unreadable) the chat counts as
    local only: a smaller guess could miss a flag stored under another form.

    No app.db means no wa-app and so no such setting: False. An app.db that exists but cannot be
    read fails closed (True), because a wrong "no" would send a private voice note to a cloud service.
    Callers that already hold connections (wa-app) pass them in.
    """
    own_messages = None
    try:
        if messages_conn is None:
            own_messages = sqlite3.connect(f"file:{MESSAGES_DB_PATH}?mode=ro", uri=True, timeout=5)
            messages_conn = own_messages
        forms = chat_forms_with_shells(messages_conn, chat_jid)
    except Exception as exc:
        # Aliases unknown, so a flag under another form of this chat could be missed: fail closed.
        logger.warning(
            "[transcribe] cannot resolve the aliases of %s (%s): treating it as local only",
            log_safe(chat_jid),
            type(exc).__name__,
        )
        return True
    finally:
        if own_messages is not None:
            own_messages.close()
    marks = ",".join("?" * len(forms))
    query = f"SELECT 1 FROM chat_ai WHERE local_only != 0 AND chat_jid IN ({marks}) LIMIT 1"
    if app_conn is not None:
        return app_conn.execute(query, sorted(forms)).fetchone() is not None
    path = _wa_app_db_path()
    if not path.exists():
        if _wa_app_in_use():
            logger.warning(
                "[transcribe] wa-app is configured but %s is missing: treating %s as local only", path, chat_jid
            )
            return True
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            return conn.execute(query, sorted(forms)).fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning("[transcribe] cannot read wa-app local-only flag, treating %s as local only", chat_jid)
        return True


def available_backend(local_only: bool = False) -> str:
    """Resolve WHISPER_BACKEND, probing availability for "auto".

    Auto never picks Groq when the chat is local only or TRANSCRIBE_CLOUD_FALLBACK is off.
    """
    b = os.environ.get("WHISPER_BACKEND", "auto").lower()
    if b != "auto":
        return b
    if WHISPER_SERVER_URL:
        return "whisper-server"
    try:
        import mlx_whisper  # noqa: F401

        return "mlx"
    except ImportError:
        pass
    try:
        import faster_whisper  # noqa: F401

        return "faster-whisper"
    except ImportError:
        pass
    if os.environ.get("GROQ_API_KEY") and not local_only and cloud_fallback_enabled():
        return "groq"
    return "none"


def _local_unavailable(backend: str, detail: object = None) -> dict[str, Any]:
    if detail:
        logger.warning("[transcribe] local-only chat: %s failed (%s)", backend, detail)
    return {"success": False, "backend": "local", "message": LOCAL_UNAVAILABLE}


def transcribe_file(file_path: str, language: str | None = None, *, local_only: bool = False) -> dict[str, Any]:
    """Transcribe an audio file with whichever backend is available.

    local_only=True never touches a cloud backend: no Groq, not as the chosen
    backend and not as a fallback. Any failure becomes LOCAL_UNAVAILABLE.
    """
    backend = available_backend(local_only)
    if local_only:
        if backend in ("groq", "none"):
            return _local_unavailable(backend)
        result = _run_backend(backend, file_path, language)
        if result is None or not result.get("success"):
            return _local_unavailable(backend, result.get("message") if result else "unknown backend")
        return result
    result = _run_backend(backend, file_path, language)
    if result is None:
        return _no_backend()
    if not result.get("success") and backend != "groq" and os.environ.get("GROQ_API_KEY") and cloud_fallback_enabled():
        logger.warning("[transcribe] %s failed (%s), retrying on groq", backend, result.get("message"))
        fallback = _run_backend("groq", file_path, language)
        if fallback is not None and fallback.get("success"):
            return fallback
    return result


def _run_backend(backend: str, file_path: str, language: str | None) -> dict[str, Any] | None:
    try:
        if backend == "whisper-server":
            return _whisper_server(file_path, language)
        if backend == "mlx":
            return _mlx(file_path, language)
        if backend == "faster-whisper":
            return _faster_whisper(file_path, language)
        if backend == "groq":
            return _groq(file_path, language)
    except Exception as exc:
        return {"success": False, "backend": backend, "message": f"Transcription failed: {exc}"}
    return None


def _no_backend() -> dict[str, Any]:
    return {
        "success": False,
        "backend": "none",
        "message": (
            "No transcription backend available. Install mlx-whisper (Apple Silicon) "
            "or faster-whisper (any OS), or set GROQ_API_KEY for the Groq API. "
            "Override with WHISPER_BACKEND."
        ),
    }


def _whisper_server(file_path: str, language: str | None) -> dict[str, Any]:
    """POST the file to a whisper.cpp server started with --convert, which
    accepts WhatsApp's ogg/opus as-is."""
    import requests

    if not WHISPER_SERVER_URL:
        return {"success": False, "backend": "whisper-server", "message": "WHISPER_SERVER_URL not set"}
    with open(file_path, "rb") as f:
        r = requests.post(
            f"{WHISPER_SERVER_URL}/inference",
            files={"file": (os.path.basename(file_path), f)},
            data={"response_format": "verbose_json", "language": language or "auto"},
            timeout=300,
        )
    if r.status_code != 200:
        return {
            "success": False,
            "backend": "whisper-server",
            "message": f"whisper-server HTTP {r.status_code}: {r.text[:200]}",
        }
    body = r.json()
    if "error" in body:
        return {"success": False, "backend": "whisper-server", "message": str(body["error"])[:200]}
    return {
        "success": True,
        "backend": "whisper-server",
        "text": (body.get("text") or "").strip(),
        "language": body.get("detected_language") or body.get("language"),
    }


def _mlx(file_path: str, language: str | None) -> dict[str, Any]:
    import mlx_whisper

    result = mlx_whisper.transcribe(file_path, path_or_hf_repo=MLX_MODEL, language=language)
    return {
        "success": True,
        "backend": "mlx",
        "text": (result.get("text") or "").strip(),
        "language": result.get("language"),
    }


def _faster_whisper(file_path: str, language: str | None) -> dict[str, Any]:
    global _fw_model
    from faster_whisper import WhisperModel

    with _fw_lock:
        if _fw_model is None:
            logger.info("[transcribe] loading faster-whisper model %s", FW_MODEL)
            _fw_model = WhisperModel(FW_MODEL, compute_type="auto")
    segments, info = _fw_model.transcribe(file_path, language=language)
    text = " ".join(s.text.strip() for s in segments)
    return {"success": True, "backend": "faster-whisper", "text": text.strip(), "language": info.language}


def _groq(file_path: str, language: str | None) -> dict[str, Any]:
    import requests

    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return {"success": False, "backend": "groq", "message": "GROQ_API_KEY not set"}
    data: dict[str, str] = {"model": GROQ_MODEL, "response_format": "verbose_json"}
    if language:
        data["language"] = language
    with open(file_path, "rb") as f:
        r = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (os.path.basename(file_path), f)},
            data=data,
            timeout=120,
        )
    if r.status_code != 200:
        return {"success": False, "backend": "groq", "message": f"Groq HTTP {r.status_code}: {r.text[:200]}"}
    body = r.json()
    return {
        "success": True,
        "backend": "groq",
        "text": (body.get("text") or "").strip(),
        "language": body.get("language"),
    }


# ---------- auto-transcribe pipeline ----------


def _pending_voice_notes(limit: int, max_age_days: int) -> list[tuple[str, str]]:
    conn = sqlite3.connect(f"file:{MESSAGES_DB_PATH}?mode=ro", uri=True, timeout=10)
    try:
        # datetime(timestamp), not the raw text: datetime('now') is UTC with no
        # offset, the stored text is local "+02:00", so a string compare moved
        # the cutoff by the UTC offset.
        rows = conn.execute(
            """
            SELECT id, chat_jid FROM messages
            WHERE media_type IN ('audio', 'ptt')
              AND (content IS NULL OR TRIM(content) = '')
              AND datetime(timestamp) >= datetime('now', ?)
            ORDER BY datetime(timestamp) DESC LIMIT ?
            """,
            (f"-{max_age_days} days", limit),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    finally:
        conn.close()


def _store_transcript(message_id: str, chat_jid: str, text: str) -> None:
    conn = sqlite3.connect(MESSAGES_DB_PATH, timeout=10)
    try:
        conn.execute(
            "UPDATE messages SET content = ? WHERE id = ? AND chat_jid = ? AND (content IS NULL OR TRIM(content) = '')",
            (VOICE_PREFIX + text, message_id, chat_jid),
        )
        conn.commit()
    finally:
        conn.close()


def _auto_tick(limit: int, max_age_days: int) -> None:
    import whatsapp  # lazy: avoid import cycle at module load

    for message_id, chat_jid in _pending_voice_notes(limit, max_age_days):
        if (message_id, chat_jid) in _failed or time.time() < _retry_at.get((message_id, chat_jid), 0.0):
            continue
        path = whatsapp.download_media(message_id, chat_jid)
        if not path:
            _failed.add((message_id, chat_jid))
            continue
        result = transcribe_file(path, local_only=is_local_only_chat(chat_jid))
        if not result.get("success") or not result.get("text"):
            logger.warning("[transcribe] auto: %s failed: %s", log_safe(message_id), log_safe(result.get("message")))
            key = (message_id, chat_jid)
            _attempts[key] = _attempts.get(key, 0) + 1
            if result.get("message") == LOCAL_UNAVAILABLE and _attempts[key] < LOCAL_MAX_ATTEMPTS:
                # Local whisper may be down for a while: try this note again later (it used to fall back to Groq).
                _retry_at[key] = time.time() + LOCAL_RETRY_SECONDS
            else:
                _failed.add(key)
                _retry_at.pop(key, None)
                _attempts.pop(key, None)
            continue
        _retry_at.pop((message_id, chat_jid), None)
        _attempts.pop((message_id, chat_jid), None)
        _store_transcript(message_id, chat_jid, result["text"])
        logger.info(
            "[transcribe] auto: %s via %s (%d chars)",
            log_safe(message_id),
            result.get("backend"),
            len(result["text"]),
        )


def start_auto_transcribe(interval_seconds: int = 180) -> None:
    """Opt-in background loop: transcribe fresh voice notes into content.

    Called by serve_http.py when AUTO_TRANSCRIBE_VOICE is truthy — never at
    import time. Transcripts land in messages.content, so the recall indexer
    picks them up on its next tick and voice becomes semantically searchable.
    """
    global _auto_thread
    backend = available_backend()
    if backend == "none":
        logger.warning("[transcribe] auto-transcribe requested but no backend available")
        return
    limit = int(os.environ.get("AUTO_TRANSCRIBE_BATCH", "3"))
    max_age = int(os.environ.get("AUTO_TRANSCRIBE_MAX_AGE_DAYS", "3"))

    def _loop() -> None:
        logger.info("[transcribe] auto-transcribe on (backend=%s, every %ss)", backend, interval_seconds)
        while True:
            try:
                _auto_tick(limit, max_age)
            except Exception:
                logger.exception("[transcribe] auto tick failed")
            time.sleep(interval_seconds)

    with _auto_lock:
        if _auto_thread is not None and _auto_thread.is_alive():
            return
        _auto_thread = threading.Thread(target=_loop, daemon=True, name="auto-transcribe")
        _auto_thread.start()

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

from .utils import MESSAGES_DB_PATH, logger

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


def cloud_fallback_enabled() -> bool:
    """False when TRANSCRIBE_CLOUD_FALLBACK is 0/false/no/off. Default on, as before."""
    return os.environ.get("TRANSCRIBE_CLOUD_FALLBACK", "1").strip().lower() not in ("0", "false", "no", "off")


def _wa_app_db_path() -> Path:
    data = os.environ.get("WA_APP_DATA")
    return (Path(data) if data else Path.home() / ".local" / "share" / "wa-app") / "app.db"


def is_local_only_chat(chat_jid: str) -> bool:
    """True when wa-app marks the chat local only (its app.db, read-only).

    No app.db means no wa-app and so no such setting: False. An app.db that
    exists but cannot be read fails closed (True), because a wrong "no" would
    send a private voice note to a cloud service.
    """
    path = _wa_app_db_path()
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute("SELECT local_only FROM chat_ai WHERE chat_jid=?", (chat_jid,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning("[transcribe] cannot read wa-app local-only flag, treating %s as local only", chat_jid)
        return True
    return bool(row and row[0])


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
        if (message_id, chat_jid) in _failed:
            continue
        path = whatsapp.download_media(message_id, chat_jid)
        if not path:
            _failed.add((message_id, chat_jid))
            continue
        result = transcribe_file(path, local_only=is_local_only_chat(chat_jid))
        if not result.get("success") or not result.get("text"):
            logger.warning("[transcribe] auto: %s failed: %s", message_id, result.get("message"))
            _failed.add((message_id, chat_jid))
            continue
        _store_transcript(message_id, chat_jid, result["text"])
        logger.info(
            "[transcribe] auto: %s via %s (%d chars)",
            message_id,
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

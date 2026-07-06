"""Pluggable speech-to-text backends for transcription tools + auto pipeline.

Backend selection via WHISPER_BACKEND (default "auto"):
    mlx             mlx-whisper — Apple Silicon only, fully local (~1.5 GB model)
    faster-whisper  CTranslate2 — cross-platform CPU/GPU, fully local
    groq            Groq API (whisper-large-v3-turbo) — needs GROQ_API_KEY,
                    near-zero RAM, ideal for small always-on boxes
    auto            first available in the order above

The auto-transcribe pipeline (start_auto_transcribe, opt-in via
AUTO_TRANSCRIBE_VOICE=true in the shared server) transcribes incoming voice
notes in the background and writes the text into messages.content, which makes
voice searchable by `recall` and readable in wa-client.

Pro tier — whatsapp-mcp-pro.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any

from .utils import MESSAGES_DB_PATH, logger

MLX_MODEL = os.environ.get("MLX_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
FW_MODEL = os.environ.get("FASTER_WHISPER_MODEL", "large-v3-turbo")
GROQ_MODEL = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
VOICE_PREFIX = "\U0001f3a4 "  # 🎤 marks auto-transcribed content

_fw_model = None
_fw_lock = threading.Lock()
_auto_thread: threading.Thread | None = None
_auto_lock = threading.Lock()
_failed: set[tuple[str, str]] = set()


def available_backend() -> str:
    """Resolve WHISPER_BACKEND, probing availability for "auto"."""
    b = os.environ.get("WHISPER_BACKEND", "auto").lower()
    if b != "auto":
        return b
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
    if os.environ.get("GROQ_API_KEY"):
        return "groq"
    return "none"


def transcribe_file(file_path: str, language: str | None = None) -> dict[str, Any]:
    """Transcribe an audio file with whichever backend is available."""
    backend = available_backend()
    try:
        if backend == "mlx":
            return _mlx(file_path, language)
        if backend == "faster-whisper":
            return _faster_whisper(file_path, language)
        if backend == "groq":
            return _groq(file_path, language)
    except Exception as exc:
        return {"success": False, "backend": backend, "message": f"Transcription failed: {exc}"}
    return {
        "success": False,
        "backend": "none",
        "message": (
            "No transcription backend available. Install mlx-whisper (Apple Silicon) "
            "or faster-whisper (any OS), or set GROQ_API_KEY for the Groq API. "
            "Override with WHISPER_BACKEND."
        ),
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
        rows = conn.execute(
            """
            SELECT id, chat_jid FROM messages
            WHERE media_type IN ('audio', 'ptt')
              AND (content IS NULL OR TRIM(content) = '')
              AND timestamp >= datetime('now', ?)
            ORDER BY timestamp DESC LIMIT ?
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
        result = transcribe_file(path)
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

"""Bridge helpers from whatsapp-mcp-server, which is a flat set of modules and not an installable package.

The server directory is put on sys.path (default: <repo>/whatsapp-mcp-server next to wa-app; env
WA_MCP_SERVER_DIR overrides) before anything is imported from it. lib.utils resolves a store path from
WA_STORE_PATH at import time and raises if no messages.db is there; the app always hands the helpers its own
connections, so that check is switched off (WA_SKIP_DB_CHECK) and the resolved path is never used.
"""

import os
import sys
from pathlib import Path


def server_dir() -> Path:
    override = os.getenv("WA_MCP_SERVER_DIR")
    return Path(override) if override else Path(__file__).resolve().parents[3] / "whatsapp-mcp-server"


def _ensure_path() -> None:
    path = str(server_dir())
    if path not in sys.path:
        sys.path.insert(0, path)
    os.environ.setdefault("WA_SKIP_DB_CHECK", "1")


_ensure_path()

from lib.changes import chat_forms  # noqa: E402
from lib.naming import resolve_display_names  # noqa: E402
from lib.utils import canonical_chat_jid  # noqa: E402


def download_media(message_id: str, chat_jid: str, *, no_phone_retry: bool) -> str | None:
    """The bridge's download (local copy first, then CDN). `no_phone_retry=False` may ping the sender's phone."""
    import whatsapp

    return whatsapp.download_media(message_id, chat_jid, no_phone_retry=no_phone_retry)


def get_profile_picture(jid: str, preview: bool = True) -> dict:
    import whatsapp

    return whatsapp.get_profile_picture(jid, preview=preview)


def transcribe_file(path: str, language: str | None = None) -> dict:
    from lib.transcribe import transcribe_file as transcribe

    return transcribe(path, language)


__all__ = [
    "canonical_chat_jid",
    "chat_forms",
    "download_media",
    "get_profile_picture",
    "resolve_display_names",
    "server_dir",
    "transcribe_file",
]

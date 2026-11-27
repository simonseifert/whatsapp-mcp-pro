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


def start_history_backfill(chat_jid: str, until: str | None, max_batches: int) -> dict:
    """Start the bridge's background backfill for one chat (blocking HTTP: call from a worker thread)."""
    import whatsapp

    return whatsapp.start_history_backfill(chat_jid, until=until, max_batches=max_batches)


def history_backfill_status() -> dict:
    """The bridge's backfill jobs since it came up (blocking HTTP: call from a worker thread)."""
    import whatsapp

    return whatsapp.history_backfill_status()


def get_profile_picture(jid: str, preview: bool = True) -> dict:
    import whatsapp

    return whatsapp.get_profile_picture(jid, preview=preview)


def mark_messages_read(chat_jid: str, message_ids: list[str], sender_jid: str | None = None) -> dict:
    """Send read receipts through the bridge (blocking HTTP: call from a worker thread). Others see blue ticks."""
    import whatsapp

    return whatsapp.mark_messages_read(chat_jid, message_ids, sender_jid=sender_jid)


def send_message(
    recipient: str, message: str, quoted_message_id: str | None = None, idempotency_key: str | None = None
) -> dict:
    """Send a text through the bridge (blocking HTTP: call from a worker thread). Every bridge cap and lock applies."""
    import whatsapp

    return whatsapp.send_message(
        recipient, message, quoted_message_id=quoted_message_id, idempotency_key=idempotency_key
    )


def send_reaction(chat_jid: str, message_id: str, emoji: str, idempotency_key: str | None = None) -> dict:
    """React to a message ("" removes the reaction). Blocking HTTP: call from a worker thread."""
    import whatsapp

    return whatsapp.send_reaction(chat_jid, message_id, emoji, idempotency_key=idempotency_key)


def transcribe_file(path: str, language: str | None = None, *, local_only: bool = False) -> dict:
    """Transcribe with the MCP server's backends. `local_only` keeps the audio off every cloud backend."""
    from lib.transcribe import transcribe_file as transcribe

    return transcribe(path, language, local_only=local_only)


def is_local_only_chat(messages_conn, app_conn, chat_jid: str) -> bool:
    """True when the chat is flagged local only under any of its forms (LID, phone JID, merged chats)."""
    from lib.transcribe import is_local_only_chat as check

    return check(chat_jid, messages_conn=messages_conn, app_conn=app_conn)


__all__ = [
    "canonical_chat_jid",
    "chat_forms",
    "download_media",
    "is_local_only_chat",
    "get_profile_picture",
    "history_backfill_status",
    "mark_messages_read",
    "resolve_display_names",
    "send_message",
    "send_reaction",
    "server_dir",
    "start_history_backfill",
    "transcribe_file",
]

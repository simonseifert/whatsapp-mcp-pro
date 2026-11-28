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
    recipient: str,
    message: str,
    quoted_message_id: str | None = None,
    idempotency_key: str | None = None,
    quoted_chat_jid: str | None = None,
    forwarded: bool = False,
) -> dict:
    """Send a text through the bridge (blocking HTTP: call from a worker thread). Every bridge cap and lock applies.

    `quoted_chat_jid` is the chat the quoted message lives in when it is not `recipient`'s (a private reply).
    `forwarded` sends it with the Forwarded label.
    """
    import whatsapp

    extra = {"forwarded": True} if forwarded else {}
    return whatsapp.send_message(
        recipient,
        message,
        quoted_message_id=quoted_message_id,
        idempotency_key=idempotency_key,
        quoted_chat_jid=quoted_chat_jid,
        **extra,
    )


def send_file(
    recipient: str,
    *,
    file_path: str,
    filename: str | None,
    caption: str | None,
    quoted_message_id: str | None,
    quoted_chat_jid: str | None,
    idempotency_key: str,
    forwarded: bool = False,
) -> dict:
    """Send a file through the bridge (blocking HTTP: call from a worker thread). Every bridge cap and lock applies.

    The bridge names the document after `file_path`'s basename, so stage the file under the name it should carry;
    `filename` is only accepted to keep the caller's call explicit.
    """
    import whatsapp

    extra = {"forwarded": True} if forwarded else {}
    return whatsapp.send_file(
        recipient,
        media_path=file_path,
        filename=filename,
        idempotency_key=idempotency_key,
        caption=caption,
        quoted_message_id=quoted_message_id,
        quoted_chat_jid=quoted_chat_jid,
        **extra,
    )


def send_audio_message(
    recipient: str,
    *,
    file_path: str,
    quoted_message_id: str | None,
    quoted_chat_jid: str | None = None,
    idempotency_key: str,
) -> dict:
    """Send a voice note through the bridge (blocking HTTP: call from a worker thread)."""
    import whatsapp

    return whatsapp.send_audio_message(
        recipient,
        file_path,
        idempotency_key=idempotency_key,
        quoted_message_id=quoted_message_id,
        quoted_chat_jid=quoted_chat_jid,
    )


def send_reaction(chat_jid: str, message_id: str, emoji: str, idempotency_key: str | None = None) -> dict:
    """React to a message ("" removes the reaction). Blocking HTTP: call from a worker thread."""
    import whatsapp

    return whatsapp.send_reaction(chat_jid, message_id, emoji, idempotency_key=idempotency_key)


def edit_message(chat_jid: str, message_id: str, new_content: str) -> dict:
    """Edit one of our own sent texts. Blocking HTTP: call from a worker thread."""
    import whatsapp

    return whatsapp.edit_message(chat_jid, message_id, new_content)


def delete_message(chat_jid: str, message_id: str, sender_jid: str | None = None) -> dict:
    """Delete a message for everyone. Blocking HTTP: call from a worker thread."""
    import whatsapp

    return whatsapp.delete_message(chat_jid, message_id, sender_jid=sender_jid)


def star_message(
    chat_jid: str, message_id: str, star: bool = True, sender_jid: str | None = None, from_me: bool = False
) -> dict:
    """Star or unstar a message. Blocking HTTP: call from a worker thread."""
    import whatsapp

    return whatsapp.star_message(chat_jid, message_id, star=star, sender_jid=sender_jid, from_me=from_me)


def pin_chat(chat_jid: str, pin: bool = True) -> dict:
    """Pin or unpin a chat (appstate, synced to the phone). Blocking HTTP: call from a worker thread."""
    import whatsapp

    return whatsapp.pin_chat(chat_jid, pin=pin)


def mute_chat(chat_jid: str, mute: bool = True, duration: str = "forever") -> dict:
    """Mute (`duration` "8h", "1w" or "forever") or unmute a chat. Blocking HTTP: call from a worker thread."""
    import whatsapp

    return whatsapp.mute_chat(chat_jid, mute=mute, duration=duration)


def archive_chat(chat_jid: str, archive: bool = True) -> dict:
    """Archive or unarchive a chat (the bridge unpins on archive). Blocking HTTP: call from a worker thread."""
    import whatsapp

    return whatsapp.archive_chat(chat_jid, archive=archive)


def mark_chat_unread(chat_jid: str, unread: bool = True) -> dict:
    """Mark a chat unread (the dot) or read again. Blocking HTTP: call from a worker thread."""
    import whatsapp

    return whatsapp.mark_chat_unread(chat_jid, unread=unread)


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
    "delete_message",
    "download_media",
    "edit_message",
    "is_local_only_chat",
    "archive_chat",
    "get_profile_picture",
    "history_backfill_status",
    "mark_chat_unread",
    "mark_messages_read",
    "mute_chat",
    "pin_chat",
    "resolve_display_names",
    "send_audio_message",
    "send_file",
    "send_message",
    "send_reaction",
    "server_dir",
    "star_message",
    "start_history_backfill",
    "transcribe_file",
]

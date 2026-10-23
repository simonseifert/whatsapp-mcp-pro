"""Idempotency keys for message-creating bridge calls.

Every send carries a key so a retry can never deliver a message twice: the
bridge records each key's outcome and replays it for a repeat instead of
sending again.

Keys are random per tool call unless the caller passes one. An earlier version
derived them from (tool, recipient, content) plus a 10-minute bucket, which
also swallowed deliberate repeats: a second "ok" to the same chat came back
deduplicated and was never sent. Now the retry that matters, a request that
timed out or lost its connection, is done here with the same key
(_send_request), and an agent that wants to retry a whole tool call safely
passes the idempotency_key it got back from the first attempt.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

# HTTP statuses where repeating the identical call is pointless or harmful.
_NOT_RETRYABLE = {400, 403, 409, 422}

# Codes of requests the bridge refuses before sending anything. WhatsApp itself
# would have accepted them and reported success: an edit or delete of someone
# else's message is relayed and ignored by every recipient, and an imported
# row's synthetic ID refers to nothing.
_CHANGE_REFUSALS = {
    "not_own_message": "Only messages sent from this account can be edited, or deleted outside a group "
    "you administer. Nothing was sent.",
    "not_group_admin": "That message is someone else's and this account is not an admin of the group, "
    "so it cannot be deleted for everyone. Nothing was sent.",
    "message_not_stored": "The message is not in the local store, so its author cannot be checked. "
    "Check message_id and chat_jid with list_messages. Nothing was sent.",
    "sender_unknown": "The stored message has no sender; pass sender_jid. Nothing was sent.",
    "imported_message": "That message came from a chat-export import and does not exist on WhatsApp, "
    "so nothing can be sent that refers to it. Nothing was sent.",
}


def auto_idempotency_key(tool: str) -> str:
    """A fresh random key for one tool call, e.g. ``mcp-send_message-<32 hex>``."""
    return f"mcp-{tool}-{uuid.uuid4().hex}"


def resolve_idempotency_key(explicit: str | None, tool: str, *_ignored: object) -> str:
    """Return the caller's key if given, else a fresh random one for this call.

    Extra positional arguments are accepted and ignored so call sites can keep
    passing recipient/content (they used to feed a derived key).
    """
    if explicit and explicit.strip():
        return explicit.strip()
    return auto_idempotency_key(tool)


def file_sha256(path: str) -> str:
    """Hex SHA-256 of a file, streamed so large media does not load into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def was_replayed(response: Any, body: dict[str, Any] | None = None) -> bool:
    """Whether the bridge answered from its idempotency store rather than sending."""
    headers = getattr(response, "headers", None) or {}
    try:
        if str(headers.get("Idempotent-Replay", "")).lower() == "true":
            return True
    except AttributeError:
        pass
    return bool(body and body.get("idempotent_replay"))


def bridge_error(response: Any) -> dict[str, Any]:
    """Turn a non-2xx bridge response into structured error fields.

    Keeps the old ``"HTTP <code> - <text>"`` wording in ``error`` so existing
    callers that pattern-match it keep working, and adds ``http_status``,
    ``code`` (the bridge's failure class), ``retry_after`` and ``retryable``.
    """
    status = int(getattr(response, "status_code", 0) or 0)
    text = getattr(response, "text", "") or ""
    result: dict[str, Any] = {
        "success": False,
        "error": f"HTTP {status} - {text}".strip(),
        "http_status": status,
        "retryable": status not in _NOT_RETRYABLE,
    }
    try:
        body = response.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        message = body.get("error") or body.get("message")
        if message:
            result["error"] = f"HTTP {status} - {message}"
        if body.get("code"):
            result["code"] = body["code"]
        if body.get("retry_after"):
            result["retry_after"] = body["retry_after"]
    if "retry_after" not in result:
        # Older bridges and some endpoints only set the header (seconds).
        seconds = _retry_after_header(response)
        if seconds is not None:
            result["retry_after_seconds"] = seconds
    if status == 409:
        result["hint"] = "The same send is still in progress; wait and check list_messages instead of retrying."
    elif result.get("code") in _CHANGE_REFUSALS:
        result["hint"] = _CHANGE_REFUSALS[result["code"]]
    elif status == 422 and not result.get("code"):
        result["hint"] = "This idempotency_key was already used for a different message; pass a new key."
    elif result.get("code") == "reachout_timelock":
        result["hint"] = (
            "WhatsApp is restricting this account from starting new chats. Do not message new "
            "contacts until retry_after; replies in existing chats still work."
        )
    elif result.get("code") == "send_cap":
        result["hint"] = (
            "The bridge's send cap was hit (too many messages to this chat, in total, or to new "
            "contacts). Nothing was sent. Wait until retry_after before retrying; do not loop."
        )
    elif result.get("code") == "rate_limited":
        result["hint"] = (
            "Antiban paused sending (warm-up limit or account health). Nothing was sent; back off "
            "and try again later rather than retrying immediately."
        )
    elif status == 504:
        result["hint"] = (
            "The send timed out and may or may not have gone out. Retrying the identical call "
            "(same idempotency_key) is safe: it will not be delivered twice."
        )
    return result


def _retry_after_header(response: Any) -> int | None:
    """The Retry-After header as whole seconds, or None if absent or not numeric."""
    try:
        raw = response.headers.get("Retry-After")
    except AttributeError:
        return None
    try:
        return int(str(raw).strip()) if raw is not None else None
    except ValueError:
        return None


TIMEOUT_ERROR = (
    "Request to the bridge timed out; the message may still have been sent. Retrying the identical "
    "call within 10 minutes (or with the same idempotency_key) is safe and will not send it twice."
)

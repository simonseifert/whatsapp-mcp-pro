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
    if status == 409:
        result["hint"] = "The same send is still in progress; wait and check list_messages instead of retrying."
    elif status == 422:
        result["hint"] = "This idempotency_key was already used for a different message; pass a new key."
    elif result.get("code") == "reachout_timelock":
        result["hint"] = (
            "WhatsApp is restricting this account from starting new chats. Do not message new "
            "contacts until retry_after; replies in existing chats still work."
        )
    elif status == 504:
        result["hint"] = (
            "The send timed out and may or may not have gone out. Retrying the identical call "
            "(same idempotency_key) is safe: it will not be delivered twice."
        )
    return result


TIMEOUT_ERROR = (
    "Request to the bridge timed out; the message may still have been sent. Retrying the identical "
    "call within 10 minutes (or with the same idempotency_key) is safe and will not send it twice."
)

"""Idempotency keys for message-creating bridge calls.

An agent that sees a slow or failed send tends to call the tool again. Before
idempotency keys, the bridge could not tell a retry from a new message, so a
send that had actually gone out was delivered twice. Every send now carries a
key: the caller's, or one derived here from (tool, recipient, content, quoted
id) plus a 10-minute time bucket. An identical call repeated inside the same
window maps to the same key, and the bridge replays the first result instead of
sending again. To deliberately send the same thing twice, pass a fresh key.

The bucket is a fixed window, not a sliding one: two identical calls that
straddle a bucket boundary get different keys. That is the price of a key that
needs no state on this side; it still catches the common case of a retry
seconds after a timeout.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

IDEMPOTENCY_BUCKET_SECONDS = 600

# HTTP statuses where repeating the identical call is pointless or harmful.
_NOT_RETRYABLE = {400, 403, 409, 422}


def auto_idempotency_key(tool: str, recipient: str, *parts: str | bytes | None, now: float | None = None) -> str:
    """Derive a deterministic idempotency key for a send.

    Args:
        tool: Tool name, so a message and a poll with the same text differ.
        recipient: Recipient / chat JID as given by the caller.
        *parts: Content that identifies the message (text, a media hash,
            quoted message id, ...). None and "" are distinct from each other
            only by position, which is what we want.
        now: Unix time override, for tests.

    Returns:
        A key like ``mcp-send_message-<32 hex>``.
    """
    bucket = int((time.time() if now is None else now) // IDEMPOTENCY_BUCKET_SECONDS)
    h = hashlib.sha256()
    for part in (tool, recipient, *parts, str(bucket)):
        raw = b"\x00" if part is None else (part if isinstance(part, bytes) else part.encode())
        # Length-prefix each part so ("ab", "c") and ("a", "bc") differ.
        h.update(len(raw).to_bytes(8, "big"))
        h.update(raw)
    return f"mcp-{tool}-{h.hexdigest()[:32]}"


def resolve_idempotency_key(explicit: str | None, tool: str, recipient: str, *parts: str | bytes | None) -> str:
    """Return the caller's key if given, else an auto-derived one."""
    if explicit and explicit.strip():
        return explicit.strip()
    return auto_idempotency_key(tool, recipient, *parts)


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

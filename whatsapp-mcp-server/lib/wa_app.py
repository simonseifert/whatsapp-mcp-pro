"""Client for wa-app's agent API: push a draft into the user's chat app, or flip a chat's automatic drafts.

Everything here ends in the user's own app. Nothing is sent to WhatsApp, and wa-app's agent routes have no path
to sending either: the user taps the draft, edits it and presses Send themselves.

Every function returns text for the model, including failures (it reads a sentence, not a stack trace). The bearer
token comes from `WA_APP_AGENT_TOKEN` and is never put in a message, a log line or an exception text.
"""

import os
from typing import Any
from urllib.parse import quote

import requests

from lib.utils import logger

DEFAULT_URL = "http://127.0.0.1:8085"
TIMEOUT_SECONDS = 10
NOT_SET_UP = "wa-app drafts aren't set up (WA_APP_AGENT_TOKEN)"
MAX_DETAIL = 300
BAD_JID = "chat_jid must be a chat JID such as 385910000001@s.whatsapp.net (resolve names with the search tools first)"
UNREACHABLE = "wa-app isn't reachable"


def _token() -> str | None:
    return (os.getenv("WA_APP_AGENT_TOKEN") or "").strip() or None


def _base_url() -> str:
    return (os.getenv("WA_APP_URL") or DEFAULT_URL).strip().rstrip("/")


def _call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """(JSON object, None) on success, (None, message) on any failure."""
    token = _token()
    if token is None:
        return None, NOT_SET_UP
    kwargs: dict[str, Any] = {"headers": {"Authorization": f"Bearer {token}"}, "timeout": TIMEOUT_SECONDS}
    if body is not None:
        kwargs["json"] = body
    try:
        response = requests.request(method, f"{_base_url()}{path}", **kwargs)
    except Exception as exc:  # never lets a transport surprise escape; the text can carry the URL, so log the type only
        logger.warning("wa-app agent API unreachable (%s)", type(exc).__name__)
        return None, UNREACHABLE
    try:
        data = response.json()
    except ValueError:
        data = None
    payload = data if isinstance(data, dict) else {}
    if 200 <= response.status_code < 300 and isinstance(data, dict):
        return data, None
    detail = payload.get("error") if isinstance(payload.get("error"), str) else None
    if response.status_code == 401:
        return None, "wa-app rejected the token: WA_APP_AGENT_TOKEN must be the same in both services"
    if response.status_code == 404 and detail == "Not found":
        return None, "wa-app has no agent API: set WA_APP_AGENT_TOKEN for wa-app too and restart it"
    if response.status_code == 404 and detail == "Unknown agent route":
        return None, "wa-app doesn't know this agent route: it is probably older than this MCP server"
    return None, (detail[:MAX_DETAIL] if detail else f"wa-app answered {response.status_code}")


def _bad_jid(chat_jid: str) -> bool:
    return not isinstance(chat_jid, str) or not chat_jid.strip() or "/" in chat_jid


def _chat_path(chat_jid: str) -> str:
    return f"/api/agent/chats/{quote(chat_jid, safe='@')}/ai"


def push_draft(chat_jid: str, text: str, reply_to: str | None = None) -> str:
    """Put `text` on the chat's draft card in wa-app. Never sends."""
    if _bad_jid(chat_jid):
        return BAD_JID
    body: dict[str, Any] = {"chat": chat_jid, "text": text}
    if reply_to is not None:
        body["reply_to"] = reply_to
    data, error = _call("POST", "/api/agent/drafts", body)
    if error is not None or data is None:
        return error or UNREACHABLE
    name = data.get("chat_name") if isinstance(data.get("chat_name"), str) else chat_jid
    return (
        f"Draft placed in wa-app for {name}. It was not sent: it shows on the chat's draft card "
        "(Draft · from Claude) and they review, edit and send it themselves."
    )


def _state(data: dict[str, Any]) -> bool:
    ai = data.get("ai")
    return isinstance(ai, dict) and ai.get("enabled") is True


def set_auto_drafts(chat_jid: str, enabled: bool) -> str:
    if _bad_jid(chat_jid):
        return BAD_JID
    data, error = _call("PUT", _chat_path(chat_jid), {"enabled": enabled})
    if error is not None or data is None:
        return error or UNREACHABLE
    return f"Automatic drafts for this chat are now {'on' if _state(data) else 'off'}."


def get_auto_drafts(chat_jid: str) -> str:
    if _bad_jid(chat_jid):
        return BAD_JID
    data, error = _call("GET", _chat_path(chat_jid))
    if error is not None or data is None:
        return error or UNREACHABLE
    return f"Automatic drafts for this chat are {'on' if _state(data) else 'off'}."

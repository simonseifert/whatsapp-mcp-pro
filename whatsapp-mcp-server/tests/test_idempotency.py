"""Idempotent sends: a retried call must not deliver the message twice.

The bridge does the deduplication; these tests pin down what the MCP side
sends it (a fresh key per tool call, reused for its own retry after a timeout,
or the caller's key) and how it reports the bridge's answers (replays,
distinct status codes, timeouts).
"""

import base64
from typing import Any

import pytest
import requests

import whatsapp
from lib.idempotency import auto_idempotency_key


class FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any], headers: dict[str, str] | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = str(body)

    def json(self) -> dict[str, Any]:
        return self._body


@pytest.fixture
def bridge(monkeypatch):
    """Records posted payloads/headers; replies via a settable responder."""
    state: dict[str, Any] = {"calls": [], "reply": None}

    def fake_post(url, json=None, headers=None, timeout=None):
        state["calls"].append({"url": url, "json": json, "headers": headers or {}, "timeout": timeout})
        reply = state["reply"]
        if isinstance(reply, list):  # a sequence of replies, one per call
            reply = reply.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(json)
        return reply or FakeResponse(200, {"success": True, "message_id": "MSG1", "recipient": json.get("recipient")})

    monkeypatch.setattr(whatsapp.requests, "post", fake_post)
    monkeypatch.setattr(whatsapp.time, "sleep", lambda _s: None)
    return state


def test_auto_keys_are_fresh_per_call():
    a, b = auto_idempotency_key("send_message"), auto_idempotency_key("send_message")
    assert a != b
    assert a.startswith("mcp-send_message-")


def test_repeating_the_same_text_on_purpose_sends_twice(bridge):
    """A derived key used to swallow a deliberate second "ok" for 10 minutes."""
    whatsapp.send_message("123", "ok")
    whatsapp.send_message("123", "ok")
    keys = [c["json"]["idempotency_key"] for c in bridge["calls"]]
    assert len(keys) == 2 and keys[0] != keys[1]
    # Sent both ways; the bridge accepts either.
    assert bridge["calls"][0]["headers"]["Idempotency-Key"] == keys[0]


def test_timeout_is_retried_once_with_the_same_key(bridge):
    bridge["reply"] = [requests.Timeout("read timed out"), None]
    result = whatsapp.send_message("123", "hello")
    assert result["success"] is True
    keys = [c["json"]["idempotency_key"] for c in bridge["calls"]]
    assert len(keys) == 2 and keys[0] == keys[1]


def test_explicit_key_is_passed_through(bridge):
    result = whatsapp.send_message("123", "hello", idempotency_key="my-key")
    assert bridge["calls"][0]["json"]["idempotency_key"] == "my-key"
    assert result["idempotency_key"] == "my-key"
    assert result["deduplicated"] is False


def test_replay_is_surfaced(bridge):
    bridge["reply"] = FakeResponse(
        200,
        {"success": True, "message_id": "MSG1", "recipient": "123", "idempotent_replay": True},
        {"Idempotent-Replay": "true"},
    )
    result = whatsapp.send_message("123", "hello")
    assert result["success"] is True
    assert result["deduplicated"] is True
    assert result["message_id"] == "MSG1"


@pytest.mark.parametrize(
    ("status", "body", "code", "retryable"),
    [
        (400, {"success": False, "error": "Recipient is required"}, None, False),
        (403, {"success": False, "error": "Recipient not in SEND_ALLOWED_JIDS allowlist"}, None, False),
        (409, {"success": False, "error": "still being sent"}, None, False),
        (422, {"success": False, "error": "different payload"}, None, False),
        (
            429,
            {
                "success": False,
                "message": "reachout timelock",
                "code": "reachout_timelock",
                "retry_after": "2026-10-23T10:00:00Z",
            },
            "reachout_timelock",
            True,
        ),
        (503, {"success": False, "message": "Not connected", "code": "not_connected"}, "not_connected", True),
        (504, {"success": False, "message": "timeout", "code": "timeout"}, "timeout", True),
    ],
)
def test_bridge_errors_are_structured(bridge, status, body, code, retryable):
    bridge["reply"] = FakeResponse(status, body)
    result = whatsapp.send_message("123", "hello")
    assert result["success"] is False
    assert result["http_status"] == status
    assert result["error"].startswith(f"HTTP {status} - ")
    assert result.get("code") == code
    assert result["retryable"] is retryable
    if code == "reachout_timelock":
        assert result["retry_after"] == "2026-10-23T10:00:00Z"
        assert "new chats" in result["hint"]


def test_client_timeout_says_retry_is_safe(bridge):
    bridge["reply"] = [requests.Timeout("read timed out"), requests.ConnectionError("reset")]
    result = whatsapp.send_message("123", "hello")
    assert len(bridge["calls"]) == 2  # one automatic retry, then give up
    assert result["success"] is False
    assert result["retryable"] is True
    assert "will not send it twice" in result["error"]
    assert result["idempotency_key"].startswith("mcp-send_message-")
    assert "idempotency_key" in result["hint"]


def test_send_file_gets_the_longer_media_timeout(bridge):
    png = base64.b64encode(b"\x89PNG fake bytes").decode()
    whatsapp.send_file("123", file_content_base64=png, filename="a.png")
    whatsapp.send_message("123", "text")
    assert bridge["calls"][0]["timeout"] == whatsapp.MEDIA_SEND_TIMEOUT
    assert bridge["calls"][1]["timeout"] == 30


def test_create_poll_sends_key_and_surfaces_replay(bridge):
    bridge["reply"] = FakeResponse(200, {"success": True, "message_id": "P1"}, {"Idempotent-Replay": "true"})
    result = whatsapp.create_poll("123@g.us", "Lunch?", ["a", "b"])
    assert bridge["calls"][0]["json"]["idempotency_key"].startswith("mcp-create_poll-")
    assert result["deduplicated"] is True


def test_reaction_has_no_automatic_key(bridge):
    bridge["reply"] = FakeResponse(200, {"success": True, "message": "Reaction sent"})
    whatsapp.send_reaction("123", "M1", "👍")
    assert "idempotency_key" not in bridge["calls"][0]["json"]
    whatsapp.send_reaction("123", "M1", "👍", idempotency_key="r1")
    assert bridge["calls"][1]["json"]["idempotency_key"] == "r1"


SEND_CAP_BODY = {
    "success": False,
    "error": "antiban: recipient_per_minute cap reached (8 per 1m0s); retry in 42s",
    "code": "send_cap",
    "retry_after": "2026-10-23T10:00:42Z",
}


@pytest.mark.parametrize(
    "call",
    [
        lambda: whatsapp.send_message("4915112345678@s.whatsapp.net", "hello"),
        lambda: whatsapp.send_reaction("4915112345678@s.whatsapp.net", "MSG1", "x"),
        lambda: whatsapp.edit_message("4915112345678@s.whatsapp.net", "MSG1", "fixed"),
        lambda: whatsapp.delete_message("123456789012345@lid", "MSG1"),
    ],
    ids=["send", "reaction", "edit", "delete"],
)
def test_send_cap_is_retryable_with_a_time(bridge, call):
    """A cap hit sent nothing: it must say retryable, when, and not to loop."""
    bridge["reply"] = FakeResponse(429, SEND_CAP_BODY, {"Retry-After": "42"})
    result = call()
    assert result["success"] is False
    assert result["http_status"] == 429
    assert result["code"] == "send_cap"
    assert result["retryable"] is True
    assert result["retry_after"] == "2026-10-23T10:00:42Z"
    assert "retry_after" in result["hint"]


def test_retry_after_header_is_used_when_body_lacks_it(bridge):
    body = {"success": False, "error": "paused", "code": "rate_limited"}
    bridge["reply"] = FakeResponse(429, body, {"Retry-After": "120"})
    result = whatsapp.send_message("4915112345678@s.whatsapp.net", "hello")
    assert result["retryable"] is True
    assert result["retry_after_seconds"] == 120
    assert "back off" in result["hint"]

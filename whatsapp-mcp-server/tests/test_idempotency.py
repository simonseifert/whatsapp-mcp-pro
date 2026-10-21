"""Idempotent sends: a retried call must not deliver the message twice.

The bridge does the deduplication; these tests pin down what the MCP side
sends it (a stable key per logical message, fresh per distinct message) and how
it reports the bridge's answers (replays, distinct status codes, timeouts).
"""

import base64
from typing import Any

import pytest
import requests

import whatsapp
from lib.idempotency import IDEMPOTENCY_BUCKET_SECONDS, auto_idempotency_key


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
        state["calls"].append({"url": url, "json": json, "headers": headers or {}})
        reply = state["reply"]
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(json)
        return reply or FakeResponse(200, {"success": True, "message_id": "MSG1", "recipient": json.get("recipient")})

    monkeypatch.setattr(whatsapp.requests, "post", fake_post)
    return state


def test_auto_key_is_stable_within_bucket_and_changes_across():
    t = 1_800_000_000 - (1_800_000_000 % IDEMPOTENCY_BUCKET_SECONDS)
    a = auto_idempotency_key("send_message", "123", "hi", None, now=t + 1)
    b = auto_idempotency_key("send_message", "123", "hi", None, now=t + IDEMPOTENCY_BUCKET_SECONDS - 1)
    c = auto_idempotency_key("send_message", "123", "hi", None, now=t + IDEMPOTENCY_BUCKET_SECONDS)
    assert a == b
    assert a != c


@pytest.mark.parametrize(
    "other",
    [
        ("send_message", "123", "hi!", None),  # different text
        ("send_message", "456", "hi", None),  # different recipient
        ("send_message", "123", "hi", "QUOTED"),  # replying to something
        ("create_poll", "123", "hi", None),  # different tool
        ("send_message", "123h", "i", None),  # boundary shift must not collide
    ],
)
def test_auto_key_differs_for_different_messages(other):
    now = 1_800_000_000
    assert auto_idempotency_key("send_message", "123", "hi", None, now=now) != auto_idempotency_key(*other, now=now)


def test_identical_calls_share_a_key_distinct_calls_do_not(bridge):
    whatsapp.send_message("123", "hello")
    whatsapp.send_message("123", "hello")
    whatsapp.send_message("123", "hello again")
    keys = [c["json"]["idempotency_key"] for c in bridge["calls"]]
    assert keys[0] == keys[1]
    assert keys[2] != keys[0]
    # Sent both ways; the bridge accepts either.
    assert bridge["calls"][0]["headers"]["Idempotency-Key"] == keys[0]


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
    bridge["reply"] = requests.Timeout("read timed out")
    result = whatsapp.send_message("123", "hello")
    assert result["success"] is False
    assert result["retryable"] is True
    assert "will not send it twice" in result["error"]
    assert result["idempotency_key"].startswith("mcp-send_message-")


def test_send_file_key_follows_content_not_temp_path(bridge):
    png = base64.b64encode(b"\x89PNG fake bytes").decode()
    whatsapp.send_file("123", file_content_base64=png, filename="a.png")
    whatsapp.send_file("123", file_content_base64=png, filename="a.png")
    other = base64.b64encode(b"\x89PNG other bytes").decode()
    whatsapp.send_file("123", file_content_base64=other, filename="a.png")
    calls = bridge["calls"]
    # Staged under different temp dirs, yet the same logical file.
    assert calls[0]["json"]["media_path"] != calls[1]["json"]["media_path"]
    assert calls[0]["json"]["idempotency_key"] == calls[1]["json"]["idempotency_key"]
    assert calls[2]["json"]["idempotency_key"] != calls[0]["json"]["idempotency_key"]


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

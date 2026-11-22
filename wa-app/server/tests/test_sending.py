"""Sending: text, replies and reactions through the bridge (always monkeypatched: no live bridge)."""

import sqlite3
import uuid

import pytest
from starlette.testclient import TestClient

from tests import conftest as fx
from wa_app import appdb, mcp_lib, sending
from wa_app.config import Settings

CLIENT_ID = "2f6f0c1e-8a53-4d0e-9a53-1b1c9a3a0001"


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict] = []
    answer: dict = {"success": True, "message_id": "NEWID1"}

    def send_message(recipient, message, quoted_message_id=None, idempotency_key=None):  # noqa: ANN001, ANN202
        calls.append(
            {"kind": "send", "to": recipient, "text": message, "quoted": quoted_message_id, "key": idempotency_key}
        )
        return dict(answer)

    def send_reaction(chat_jid, message_id, emoji, idempotency_key=None):  # noqa: ANN001, ANN202
        calls.append({"kind": "react", "chat": chat_jid, "id": message_id, "emoji": emoji, "key": idempotency_key})
        return dict(answer)

    monkeypatch.setattr(mcp_lib, "send_message", send_message)
    monkeypatch.setattr(mcp_lib, "send_reaction", send_reaction)
    return calls, answer


def first_incoming(client: TestClient, jid: str) -> str:
    msgs = client.get(f"/api/chats/{jid}/messages").json()["messages"]
    return next(m["id"] for m in msgs if not m["from_me"])


def post(client: TestClient, jid: str, **body: object):  # noqa: ANN201
    body.setdefault("client_key", CLIENT_ID)
    return client.post(f"/api/chats/{jid}/send", json=body)


def test_send_passes_client_key_as_idempotency_key(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    response = post(logged_in, fx.ANA, text="Vidimo se")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "message_id": "NEWID1"}
    assert calls == [{"kind": "send", "to": fx.ANA, "text": "Vidimo se", "quoted": None, "key": CLIENT_ID}]


def test_retry_reuses_key(logged_in: TestClient, bridge) -> None:
    calls, answer = bridge
    answer.clear()
    answer.update({"success": False, "error": sending_timeout(), "retryable": True, "idempotency_key": CLIENT_ID})
    first = post(logged_in, fx.ANA, text="Hej")
    assert first.json() == {"ok": False, "error": "Not sent: bridge didn't answer", "retryable": True}
    answer.clear()
    answer.update({"success": True, "message_id": "NEWID2", "deduplicated": True})
    second = post(logged_in, fx.ANA, text="Hej")
    assert second.json() == {"ok": True, "message_id": "NEWID2"}
    assert [c["key"] for c in calls] == [CLIENT_ID, CLIENT_ID]


def sending_timeout() -> str:
    return "Request to the bridge timed out; the message may still have been sent."


@pytest.mark.parametrize(
    ("result", "line", "retryable"),
    [
        (
            {"http_status": 429, "code": "rate_limited", "error": "HTTP 429 - x", "retryable": True},
            "Not sent: limit",
            True,
        ),
        ({"http_status": 429, "code": "send_cap", "retry_after": "x", "retryable": True}, "Not sent: limit", True),
        (
            {"http_status": 429, "code": "reachout_timelock", "error": "HTTP 429 - locked", "retryable": True},
            "Not sent: new chats are paused for now",
            False,
        ),
        (
            {"http_status": 403, "code": "forbidden", "error": "HTTP 403 - nope", "retryable": False},
            "Not sent: this chat isn't allowed",
            False,
        ),
        (
            {"error": sending_timeout(), "retryable": True, "idempotency_key": CLIENT_ID},
            "Not sent: bridge didn't answer",
            True,
        ),
        ({"http_status": 504, "code": "timeout", "retryable": True}, "Not sent: bridge didn't answer", True),
        (
            {"http_status": 502, "code": "whatsapp_error", "error": "HTTP 502 - server said no", "retryable": True},
            "Not sent: server said no",
            True,
        ),
        (
            {
                "http_status": 409,
                "error": "HTTP 409 - request with this idempotency_key is still being sent",
                "retryable": False,
            },
            "Not sent: bridge didn't answer",
            True,
        ),
        ({"http_status": 400, "error": "HTTP 400 - bad jid", "retryable": False}, "Not sent: bad jid", False),
    ],
)
def test_error_mapping(logged_in: TestClient, bridge, result: dict, line: str, retryable: bool) -> None:
    _, answer = bridge
    answer.clear()
    answer.update({"success": False, **result})
    response = post(logged_in, fx.ANA, text="Hej")
    assert response.json() == {"ok": False, "error": line, "retryable": retryable}
    assert response.status_code >= 400


def test_bridge_crash_reads_as_no_answer(logged_in: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp_lib, "send_message", boom)
    assert post(logged_in, fx.ANA, text="Hej").json()["error"] == "Not sent: bridge didn't answer"


def test_long_text_rejected_before_the_bridge(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    response = post(logged_in, fx.ANA, text="x" * 4097)
    assert response.status_code == 422 and response.json() == {"error": "Message too long"}
    assert post(logged_in, fx.ANA, text="x" * 4096).status_code == 200
    assert len(calls) == 1


@pytest.mark.parametrize("text", ["", "   \n", None, 5])
def test_empty_text_rejected(logged_in: TestClient, bridge, text: object) -> None:
    assert post(logged_in, fx.ANA, text=text).status_code == 422
    assert bridge[0] == []


@pytest.mark.parametrize("key", ["", "abc", None, 7, "2f6f0c1e-8a53-4d0e-9a53"])
def test_bad_client_key_rejected(logged_in: TestClient, bridge, key: object) -> None:
    assert post(logged_in, fx.ANA, text="Hej", client_key=key).status_code == 422
    assert bridge[0] == []


def test_non_object_body_rejected(logged_in: TestClient, bridge) -> None:
    assert logged_in.post(f"/api/chats/{fx.ANA}/send", json=["x"]).status_code == 400
    assert logged_in.post(f"/api/chats/{fx.ANA}/send", content=b"{nope").status_code == 400


def test_unknown_chat_404(logged_in: TestClient, bridge) -> None:
    assert post(logged_in, "385910000777@s.whatsapp.net", text="Hej").status_code == 404
    assert bridge[0] == []


def test_reply_quotes_a_message_of_this_chat(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    mid = first_incoming(logged_in, fx.ANA)
    assert post(logged_in, fx.ANA, text="Da", reply_to=mid).status_code == 200
    assert calls[0]["quoted"] == mid


def test_reply_to_other_chats_message_rejected(logged_in: TestClient, bridge) -> None:
    other = first_incoming(logged_in, fx.EKIPA)
    response = post(logged_in, fx.ANA, text="Da", reply_to=other)
    assert response.status_code == 422
    assert post(logged_in, fx.ANA, text="Da", reply_to="NOPE").status_code == 422
    assert bridge[0] == []


def test_reply_to_import_id_rejected(logged_in: TestClient, settings: Settings, bridge) -> None:
    conn = sqlite3.connect(settings.store_path / "messages.db")
    conn.execute(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?,?,?,?,?,0)",
        ("import-abc123", fx.ANA, fx.ANA, "Stara poruka", "2026-01-01 10:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    response = post(logged_in, fx.ANA, text="Da", reply_to="import-abc123")
    assert response.status_code == 422
    assert bridge[0] == []
    react = logged_in.post(
        f"/api/chats/{fx.ANA}/react", json={"message_id": "import-abc123", "emoji": "👍", "client_key": CLIENT_ID}
    )
    assert react.status_code == 422 and bridge[0] == []


def test_split_chat_sends_to_the_phone_form(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    assert post(logged_in, fx.TOMO_LID, text="Hej").status_code == 200
    assert calls[0]["to"] == fx.TOMO_PHONE


def test_requires_login_and_origin(client: TestClient, logged_in: TestClient, bridge) -> None:
    anon = TestClient(client.app, base_url="https://testserver")
    assert post(anon, fx.ANA, text="Hej").status_code in (401, 403)
    cross = logged_in.post(
        f"/api/chats/{fx.ANA}/send",
        json={"text": "Hej", "client_key": CLIENT_ID},
        headers={"Origin": "https://evil.example"},
    )
    assert cross.status_code == 403
    assert bridge[0] == []


def test_react_passes_key_and_chat(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    mid = first_incoming(logged_in, fx.ANA)
    response = logged_in.post(
        f"/api/chats/{fx.ANA}/react", json={"message_id": mid, "emoji": "👍", "client_key": CLIENT_ID}
    )
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert calls == [{"kind": "react", "chat": fx.ANA, "id": mid, "emoji": "👍", "key": CLIENT_ID}]


def test_react_remove_with_empty_emoji(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    mid = first_incoming(logged_in, fx.ANA)
    response = logged_in.post(
        f"/api/chats/{fx.ANA}/react", json={"message_id": mid, "emoji": "", "client_key": CLIENT_ID}
    )
    assert response.status_code == 200
    assert calls[0]["emoji"] == ""


def test_react_validation_and_failure(logged_in: TestClient, bridge) -> None:
    calls, answer = bridge
    mid = first_incoming(logged_in, fx.ANA)
    url = f"/api/chats/{fx.ANA}/react"
    assert logged_in.post(url, json={"message_id": mid, "emoji": "x" * 40, "client_key": CLIENT_ID}).status_code == 422
    assert logged_in.post(url, json={"message_id": mid, "emoji": None, "client_key": CLIENT_ID}).status_code == 422
    assert logged_in.post(url, json={"message_id": mid, "emoji": "👍", "client_key": "no"}).status_code == 422
    assert logged_in.post(url, json={"message_id": "NOPE", "emoji": "👍", "client_key": CLIENT_ID}).status_code == 404
    assert calls == []
    answer.clear()
    answer.update({"success": False, "http_status": 429, "code": "rate_limited", "retryable": True})
    response = logged_in.post(url, json={"message_id": mid, "emoji": "👍", "client_key": str(uuid.uuid4())})
    assert response.json() == {"ok": False, "error": "Not sent: limit", "retryable": True}


def test_map_failure_unknown_is_truncated() -> None:
    out = sending.map_failure({"http_status": 500, "error": "HTTP 500 - " + "y" * 500, "retryable": True})
    assert out.body["error"].startswith("Not sent: yyy") and len(out.body["error"]) <= 140


def test_text_is_trimmed_before_sending(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    assert post(logged_in, fx.ANA, text="\n\n  Hej\nsvima  \n\n").status_code == 200
    assert calls[0]["text"] == "  Hej\nsvima"
    assert post(logged_in, fx.ANA, text="\n\n \n").status_code == 422
    assert post(logged_in, fx.ANA, text="x" * 4096 + "\n\n").status_code == 200  # the limit counts the trimmed text


def test_recipient_is_remembered_per_key_and_pruned(logged_in: TestClient, settings: Settings, bridge) -> None:
    calls, _ = bridge
    assert post(logged_in, fx.ANA, text="Hej").status_code == 200
    app = appdb.connect(settings)
    assert app.execute("SELECT recipient FROM send_keys WHERE client_key = ?", (CLIENT_ID,)).fetchone() == (fx.ANA,)
    # The same key from another route form of the chat goes to the recipient it first used.
    app.execute("UPDATE send_keys SET recipient = ? WHERE client_key = ?", ("385910000555@s.whatsapp.net", CLIENT_ID))
    assert post(logged_in, fx.ANA, text="Hej").status_code == 200
    assert calls[1]["to"] == "385910000555@s.whatsapp.net"
    # Rows older than 24 h are dropped on the next send.
    app.execute("UPDATE send_keys SET created_at = created_at - 90000")
    other = str(uuid.uuid4())
    assert post(logged_in, fx.ANA, text="Hej", client_key=other).status_code == 200
    assert app.execute("SELECT client_key FROM send_keys").fetchall() == [(other,)]
    app.close()

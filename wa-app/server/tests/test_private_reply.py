"""Replying privately to a group message: the quote lives in the group, the send goes to the sender's direct chat."""

import sqlite3
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient

from tests import conftest as fx
from wa_app import mcp_lib
from wa_app.config import Settings

KEY = "2f6f0c1e-8a53-4d0e-9a53-1b1c9a3a0006"
PETRA = "385910000026@s.whatsapp.net"  # in EKIPA, no chat of her own
PETRA_LID = "777@lid"
GHOST_LID = "888@lid"  # a group sender the store has no phone number for


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict] = []
    answer: dict = {"success": True, "message_id": "PRIV1"}

    def send_message(recipient, message, quoted_message_id=None, idempotency_key=None, quoted_chat_jid=None):  # noqa: ANN001, ANN202
        calls.append(
            {
                "to": recipient,
                "text": message,
                "quoted": quoted_message_id,
                "chat": quoted_chat_jid,
                "key": idempotency_key,
            }
        )
        return dict(answer)

    def send_file(recipient, **kw):  # noqa: ANN001, ANN202
        calls.append({"to": recipient, "file": True, **{k: v for k, v in kw.items() if k != "file_path"}})
        return dict(answer)

    def send_audio_message(recipient, **kw):  # noqa: ANN001, ANN202
        calls.append({"to": recipient, "voice": True, **{k: v for k, v in kw.items() if k != "file_path"}})
        return dict(answer)

    monkeypatch.setattr(mcp_lib, "send_message", send_message)
    monkeypatch.setattr(mcp_lib, "send_file", send_file)
    monkeypatch.setattr(mcp_lib, "send_audio_message", send_audio_message)
    return calls, answer


def add_group_message(settings: Settings, mid: str, sender: str, *, from_me: bool = False) -> None:
    conn = sqlite3.connect(settings.store_path / "messages.db")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
    values = {
        "id": mid,
        "chat_jid": fx.EKIPA,
        "sender": sender,
        "content": "Ja dolazim",
        "timestamp": "2026-11-20 10:00:00+00:00",
        "is_from_me": 1 if from_me else 0,
    }
    conn.execute(
        f"INSERT INTO messages ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})", list(values.values())
    )
    assert set(values) <= set(cols)
    conn.commit()
    conn.close()


def link_identity(settings: Settings, lid: str, phone_jid: str) -> None:
    conn = sqlite3.connect(settings.store_path / "messages.db")
    conn.execute("INSERT INTO identities (jid, lid, phone_jid) VALUES (?, ?, ?)", (phone_jid, lid, phone_jid))
    conn.commit()
    conn.close()


def send(client: TestClient, to: str, **body: object):  # noqa: ANN201
    return client.post(f"/api/chats/{to}/send", json={"text": "Hej", "client_key": KEY, **body})


def test_private_reply_sends_quote_with_group(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    response = send(logged_in, fx.NIKOLA, reply_to="E01", reply_chat=fx.EKIPA)
    assert response.status_code == 200
    assert calls == [{"to": fx.NIKOLA, "text": "Hej", "quoted": "E01", "chat": fx.EKIPA, "key": KEY}]


def test_private_reply_rejects_own_message(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    response = send(logged_in, fx.NIKOLA, reply_to="E02", reply_chat=fx.EKIPA)
    assert response.status_code == 422
    assert response.json() == {"error": "That message can't be replied to privately"}
    assert calls == []


def test_private_reply_rejects_other_recipient(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    # E01 is Nikola's: sending the quote to Ana would leak the group message to the wrong person.
    response = send(logged_in, fx.ANA, reply_to="E01", reply_chat=fx.EKIPA)
    assert response.status_code == 422
    assert response.json() == {"error": "That message can't be replied to privately"}
    assert calls == []


@pytest.mark.parametrize(
    ("reply_to", "reply_chat"),
    [("E01", fx.ANA), ("E01", "999@g.us"), ("E15", fx.EKIPA), ("nope", fx.EKIPA), (None, fx.EKIPA), ("E01", 5)],
)
def test_private_reply_rejects_bad_quotes(logged_in: TestClient, bridge, reply_to, reply_chat) -> None:  # noqa: ANN001
    calls, _ = bridge
    body = {"reply_chat": reply_chat, **({"reply_to": reply_to} if reply_to else {})}
    assert send(logged_in, fx.NIKOLA, **body).status_code == 422
    assert calls == []


def test_direct_chat_placeholder_for_known_person_without_chat(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    detail = logged_in.get(f"/api/chats/{PETRA}")
    assert detail.status_code == 200
    chat = detail.json()["chat"]
    assert chat["jid"] == PETRA
    assert chat["kind"] == "private"
    assert chat["name"] == "+385910000026"
    assert chat["last_message"]["id"] == ""
    assert logged_in.get(f"/api/chats/{PETRA}/messages").json() == {"messages": [], "has_more": False}
    # The first send creates the chat.
    response = send(logged_in, PETRA, reply_to="E03", reply_chat=fx.EKIPA)
    assert response.status_code == 200
    assert calls[0]["to"] == PETRA
    assert calls[0]["chat"] == fx.EKIPA


def test_unknown_phone_has_no_placeholder(logged_in: TestClient) -> None:
    assert logged_in.get("/api/chats/385910000555@s.whatsapp.net").status_code == 404


def test_lid_sender_with_known_phone_goes_to_the_phone_chat(logged_in: TestClient, settings: Settings, bridge) -> None:
    calls, _ = bridge
    link_identity(settings, PETRA_LID, PETRA)
    add_group_message(settings, "L1", PETRA_LID)
    response = send(logged_in, PETRA, reply_to="L1", reply_chat=fx.EKIPA)
    assert response.status_code == 200
    assert calls[0]["to"] == PETRA


def test_unmapped_lid_has_no_direct_chat(logged_in: TestClient, settings: Settings, bridge) -> None:
    calls, _ = bridge
    add_group_message(settings, "L2", GHOST_LID)
    response = send(logged_in, GHOST_LID, reply_to="L2", reply_chat=fx.EKIPA)
    assert response.status_code == 422
    assert response.json() == {"error": "Can't reply privately to this person yet"}
    assert logged_in.get(f"/api/chats/{GHOST_LID}").status_code == 404
    assert calls == []


def test_bridge_refusing_the_quote_is_a_422_and_not_retryable(logged_in: TestClient, bridge) -> None:
    calls, answer = bridge
    answer.clear()
    answer.update(
        {
            "success": False,
            "http_status": 400,
            "code": "invalid_request",
            "error": "HTTP 400 - cannot quote message E01: message not found in chat x",
            "retryable": False,
        }
    )
    response = send(logged_in, fx.NIKOLA, reply_to="E01", reply_chat=fx.EKIPA)
    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": "That message can't be replied to privately", "retryable": False}
    assert len(calls) == 1  # nothing was retried


def upload(client: TestClient, to: str, **headers: str):  # noqa: ANN201
    base = {"X-Filename": "a.pdf", "X-Client-Key": KEY, "Content-Type": "application/pdf"}
    return client.post(f"/api/chats/{to}/media", content=b"data", headers={**base, **headers})


def test_media_private_reply_carries_the_group(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    response = upload(logged_in, fx.NIKOLA, **{"X-Reply-To": "E01", "X-Reply-Chat": quote(fx.EKIPA)})
    assert response.status_code == 200
    assert calls[0]["quoted_message_id"] == "E01"
    assert calls[0]["quoted_chat_jid"] == fx.EKIPA


def test_voice_private_reply_carries_the_group(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    response = upload(logged_in, fx.NIKOLA, **{"X-Kind": "voice", "X-Reply-To": "E01", "X-Reply-Chat": quote(fx.EKIPA)})
    assert response.status_code == 200
    assert calls[0]["voice"] is True
    assert calls[0]["quoted_chat_jid"] == fx.EKIPA


def test_media_private_reply_rejects_other_recipient(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    response = upload(logged_in, fx.ANA, **{"X-Reply-To": "E01", "X-Reply-Chat": quote(fx.EKIPA)})
    assert response.status_code == 422
    assert calls == []


def test_media_bad_reply_chat_header(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    assert upload(logged_in, fx.NIKOLA, **{"X-Reply-To": "E01", "X-Reply-Chat": "%ff"}).status_code == 422
    assert upload(logged_in, fx.NIKOLA, **{"X-Reply-To": "E01", "X-Reply-Chat": "x" * 300}).status_code == 422
    assert calls == []

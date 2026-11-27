"""Chat actions: pin, mute, archive and mark unread (the bridge is always patched)."""

import sqlite3

import pytest
from starlette.testclient import TestClient

from tests import conftest as fx
from wa_app import mcp_lib


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict] = []
    answers: dict[str, dict] = {}

    def make(kind: str):  # noqa: ANN202
        def fn(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            calls.append({"kind": kind, "args": args, **kwargs})
            return dict(answers.get(kind, {"success": True}))

        return fn

    for name in ("pin_chat", "mute_chat", "archive_chat", "mark_chat_unread"):
        monkeypatch.setattr(mcp_lib, name, make(name), raising=False)
    return calls, answers


def put(client: TestClient, chat: str, **body: object):  # noqa: ANN201
    return client.post(f"/api/chats/{chat}/state", json=body)


def test_pin_calls_the_wrapper_and_returns_the_optimistic_summary(logged_in, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    response = put(logged_in, fx.ACCOUNTANT, pin=True)
    assert response.status_code == 200
    assert response.json()["chat"]["pinned"] is True
    assert calls == [{"kind": "pin_chat", "args": (fx.ACCOUNTANT,), "pin": True}]
    unpin = put(logged_in, fx.ANA, pin=False)
    assert unpin.json()["chat"]["pinned"] is False
    assert calls[-1] == {"kind": "pin_chat", "args": (fx.ANA,), "pin": False}


def test_pin_beyond_three_is_refused(logged_in, settings, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    conn = sqlite3.connect(settings.store_path / "messages.db")
    conn.execute(
        "INSERT INTO chat_state (chat_jid, pinned) VALUES (?, 1) ON CONFLICT (chat_jid) DO UPDATE SET pinned = 1",
        (fx.ACCOUNTANT,),
    )
    conn.commit()
    conn.close()
    response = put(logged_in, fx.GYM, pin=True)
    assert response.status_code == 409 and "3" in response.json()["error"]
    assert calls == []
    assert put(logged_in, fx.ANA, pin=True).status_code == 200  # already pinned: not a new pin


@pytest.mark.parametrize(("choice", "duration"), [("8h", "8h"), ("1w", "1w"), ("forever", "forever")])
def test_mute_options_map_to_bridge_durations(logged_in, bridge, choice: str, duration: str) -> None:  # noqa: ANN001
    calls, _ = bridge
    response = put(logged_in, fx.ACCOUNTANT, mute=choice)
    assert response.status_code == 200 and response.json()["chat"]["muted"] is True
    assert calls == [{"kind": "mute_chat", "args": (fx.ACCOUNTANT,), "mute": True, "duration": duration}]


def test_unmute_and_bad_mute_values(logged_in, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    response = put(logged_in, fx.FAMILY, mute=False)
    assert response.status_code == 200 and response.json()["chat"]["muted"] is False
    assert calls == [{"kind": "mute_chat", "args": (fx.FAMILY,), "mute": False, "duration": "forever"}]
    calls.clear()
    for bad in ("2h", True, 5, None):
        assert put(logged_in, fx.ACCOUNTANT, mute=bad).status_code == 422
    assert calls == []


def test_archive_and_unarchive(logged_in, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    response = put(logged_in, fx.ANA, archive=True)
    chat = response.json()["chat"]
    assert chat["archived"] is True and chat["pinned"] is False  # archiving unpins, like WhatsApp
    assert calls == [{"kind": "archive_chat", "args": (fx.ANA,), "archive": True}]
    assert put(logged_in, fx.OLD_ARCHIVED, archive=False).json()["chat"]["archived"] is False


def test_mark_unread_shows_the_dot_with_zero_unread_messages(logged_in, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    response = put(logged_in, fx.ACCOUNTANT, unread=True)
    chat = response.json()["chat"]
    assert chat["marked_unread"] is True
    assert calls == [{"kind": "mark_chat_unread", "args": (fx.ACCOUNTANT,), "unread": True}]
    assert put(logged_in, fx.NIKOLA, unread=False).json()["chat"]["marked_unread"] is False


def test_the_dot_comes_from_the_bridge_flag_in_the_list(logged_in, settings) -> None:  # noqa: ANN001
    conn = sqlite3.connect(settings.store_path / "messages.db")
    conn.execute(
        "INSERT INTO chat_state (chat_jid, marked_unread, marked_unread_changed_at)"
        " VALUES (?, 1, '2999-01-01 00:00:00+00:00')"
        " ON CONFLICT (chat_jid) DO UPDATE SET marked_unread = 1, marked_unread_changed_at = excluded.marked_unread_changed_at",
        (fx.GYM,),
    )
    conn.commit()
    conn.close()
    gym = next(c for c in logged_in.get("/api/chats").json()["chats"] if c["jid"] == fx.GYM)
    assert gym["marked_unread"] is True


def test_validation_and_failures(logged_in, bridge) -> None:  # noqa: ANN001
    calls, answers = bridge
    assert put(logged_in, "nobody@s.whatsapp.net", pin=True).status_code == 404
    assert put(logged_in, fx.ANA).status_code == 422  # nothing to do
    assert put(logged_in, fx.ANA, pin="yes").status_code == 422
    assert put(logged_in, fx.ANA, archive="yes").status_code == 422
    assert put(logged_in, fx.ANA, unread=1).status_code == 422
    assert put(logged_in, fx.ANA, pin=True, archive=True).status_code == 422
    assert logged_in.post(f"/api/chats/{fx.ANA}/state", content=b"nope").status_code == 400
    assert calls == []
    answers["mute_chat"] = {"success": False, "error": "HTTP 500 - boom"}
    response = put(logged_in, fx.ACCOUNTANT, mute="8h")
    assert response.status_code == 502 and response.json()["ok"] is False


def test_route_needs_a_session(client: TestClient) -> None:
    assert client.post(f"/api/chats/{fx.ANA}/state", json={"pin": True}).status_code == 401

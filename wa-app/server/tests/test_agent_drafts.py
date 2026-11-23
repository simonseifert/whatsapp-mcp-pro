"""Agent drafts: bearer-token routes, the worker's push, learning. Fictional data; the token is a fake."""

import asyncio
import subprocess
import sys
import time
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from tests.conftest import ANA, EKIPA, LUKAS
from tests.test_ai_drafts import _event, _insert, _ts, feed, run
from tests.test_ai_learning import outcome
from wa_app import live
from wa_app.config import Settings
from wa_app.main import create_app

TOKEN = "test-agent-token"
BEARER = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def agent(settings: Settings):
    with TestClient(create_app(replace(settings, agent_token=TOKEN)), base_url="https://testserver") as c:
        yield c


def push(agent, chat=ANA, text="Vidimo se u 7", **extra):
    return agent.post("/api/agent/drafts", json={"chat": chat, "text": text, **extra}, headers=BEARER)


# --- auth ---------------------------------------------------------------------------------------------------------


def test_404_without_a_configured_token(client: TestClient) -> None:
    for method, path in [("post", "/api/agent/drafts"), ("get", f"/api/agent/chats/{ANA}/ai")]:
        r = getattr(client, method)(path, headers=BEARER)
        assert r.status_code == 404


def test_wrong_or_missing_token_is_401(agent: TestClient) -> None:
    body = {"chat": ANA, "text": "x"}
    assert agent.post("/api/agent/drafts", json=body).status_code == 401
    for header in ["Bearer wrong-token", "Basic " + TOKEN, "Bearer", TOKEN, "Bearer "]:
        r = agent.post("/api/agent/drafts", json=body, headers={"Authorization": header})
        assert r.status_code == 401
        assert TOKEN not in r.text


def test_agent_route_needs_no_cookie_and_no_origin(agent: TestClient) -> None:
    assert not agent.cookies
    assert push(agent).status_code == 201
    # a browser-style foreign Origin is not checked either: a bearer token is not sent by browsers on their own
    r = agent.post(
        "/api/agent/drafts", json={"chat": ANA, "text": "x"}, headers={**BEARER, "Origin": "https://evil.example"}
    )
    assert r.status_code == 201


def test_normal_routes_still_need_a_cookie_even_with_the_token(agent: TestClient) -> None:
    assert agent.get("/api/chats", headers=BEARER).status_code == 401
    assert agent.get(f"/api/chats/{ANA}/draft", headers=BEARER).status_code == 401
    assert agent.get(f"/api/chats/{ANA}/ai", headers=BEARER).status_code == 401
    bad = agent.post(
        "/api/auth/setup", json={"passphrase": "x" * 12}, headers={"Origin": "https://evil.example", **BEARER}
    )
    assert bad.status_code == 403  # the Origin check on normal routes is unchanged


def test_token_is_not_echoed(agent: TestClient) -> None:
    r = push(agent)
    assert TOKEN not in r.text and TOKEN not in str(r.headers)


# --- POST /api/agent/drafts -----------------------------------------------------------------------------------------


def test_push_creates_a_ready_agent_draft(agent: TestClient) -> None:
    r = push(agent, text="  Vidimo se u 7  ")
    assert r.status_code == 201
    body = r.json()
    assert body["chat_name"] == "Ana Kovačević"
    draft = body["draft"]
    assert draft["status"] == "ready" and draft["source"] == "agent"
    assert draft["alternatives"] == ["Vidimo se u 7"] and draft["chat_jid"] == ANA
    # the web app's own route sees it
    agent.post("/api/auth/setup", json={"passphrase": "correct horse battery"})
    agent.post("/api/auth/login", json={"passphrase": "correct horse battery"})
    assert agent.get(f"/api/chats/{ANA}/draft").json()["draft"]["id"] == draft["id"]


def test_ai_drafts_are_labelled_ai(logged_in: TestClient) -> None:
    got = logged_in.post(f"/api/chats/{ANA}/draft", json={}).json()["draft"]
    assert got["source"] == "ai"


def test_default_trigger_is_the_latest_incoming_message(agent: TestClient) -> None:
    assert push(agent).json()["draft"]["trigger_id"] == "M0004"  # Ana's last incoming message
    own_last = push(agent, chat=LUKAS).json()["draft"]
    assert own_last["trigger_id"] != ""  # Lukas has one incoming message (the long one)


def test_reply_to_rules(agent: TestClient) -> None:
    assert push(agent, chat=EKIPA, reply_to="E09").json()["draft"]["trigger_id"] == "E09"
    assert push(agent, chat=EKIPA, reply_to="E09").json()["draft"]["quote"]["id"] == "E09"  # a group draft quotes it
    for bad in ["E02", "E04", "E15", "nope", "M0001"]:  # own, deleted, system, unknown, another chat's
        r = push(agent, chat=EKIPA, reply_to=bad)
        assert r.status_code == 422, bad
    assert push(agent, chat=EKIPA, reply_to=5).status_code == 422


def test_no_incoming_message_means_an_empty_trigger(agent: TestClient, settings: Settings) -> None:
    import sqlite3

    db = sqlite3.connect(settings.store_path / "messages.db")
    db.execute(
        "INSERT INTO chats (jid, name, last_message_time) VALUES ('385910000077@s.whatsapp.net', 'Nova', '2026-11-22 10:00:00')"
    )
    db.execute(
        "INSERT INTO messages (id, chat_jid, content, timestamp, is_from_me) VALUES"
        " ('Z1', '385910000077@s.whatsapp.net', 'Bok', '2026-11-22 10:00:00', 1)"
    )
    db.commit()
    db.close()
    r = push(agent, chat="385910000077@s.whatsapp.net")
    assert r.status_code == 201 and r.json()["draft"]["trigger_id"] == ""


def test_validation_and_unknown_chat(agent: TestClient) -> None:
    unknown = push(agent, chat="385910000555@s.whatsapp.net")
    assert unknown.status_code == 404 and unknown.json() == {"error": "Unknown chat"}
    for text in ["", "   ", "x" * 4097, None, 5]:
        assert push(agent, text=text).status_code == 422
    assert push(agent, text="x" * 4096).status_code == 201
    assert agent.post("/api/agent/drafts", json=["no"], headers=BEARER).status_code == 422
    assert agent.post("/api/agent/drafts", json={"text": "x"}, headers=BEARER).status_code == 422
    assert agent.post("/api/agent/drafts", json={"chat": 5, "text": "x"}, headers=BEARER).status_code == 422
    assert agent.post("/api/agent/drafts", content=b"not json", headers=BEARER).status_code == 422


def test_second_push_supersedes_the_first(agent: TestClient) -> None:
    first = push(agent, text="Prvi").json()["draft"]
    second = push(agent, text="Drugi").json()["draft"]
    assert second["id"] != first["id"]
    agent.post("/api/auth/setup", json={"passphrase": "correct horse battery"})
    agent.post("/api/auth/login", json={"passphrase": "correct horse battery"})
    assert agent.get(f"/api/chats/{ANA}/draft").json()["draft"]["alternatives"] == ["Drugi"]


# --- the worker -----------------------------------------------------------------------------------------------------


def test_push_publishes_the_draft_event_and_supersedes_ai_work(settings) -> None:
    async def scenario(rig):
        rig.setup()
        rig.fake.gate = asyncio.Event()
        _insert(settings, ANA, "N1")
        rig.worker.on_events([_event(ANA, "N1")])
        await asyncio.sleep(0.05)
        assert rig.rows() == [("pending", None)]  # an auto draft is running
        rig.events()
        view = rig.worker.push(ANA, "Evo me u 7", None)
        assert view.source == "agent" and view.status == "ready" and view.alternatives == ["Evo me u 7"]
        rig.fake.gate.set()
        await rig.worker.idle()
        assert rig.rows() == [("superseded", None), ("ready", None)]  # the running auto job was cancelled
        seen = rig.events()
        assert seen[-1]["draft"]["source"] == "agent" and seen[-1]["draft"]["status"] == "ready"
        assert seen[-1]["chat_jid"] == ANA and seen[-1]["draft"]["id"] == view.id
        assert len(rig.fake.calls) == 1  # the push made no AI call itself

    run(settings, scenario)


def test_push_works_with_ai_switched_off_and_no_backend(settings) -> None:
    async def scenario(rig):
        view = rig.worker.push(ANA, "Bok", None)  # no backend configured, chat_ai off
        assert view.status == "ready"
        assert rig.fake.calls == []

    run(settings, scenario)


def test_agent_draft_links_to_the_users_next_own_message(settings) -> None:
    async def scenario(rig):
        view = rig.worker.push(ANA, "Može, vidimo se u 7", None)
        rig.events()
        own = live.Event(
            "message",
            {
                "chat_jid": ANA,
                "message": {"id": "OWN1", "from_me": True, "text": "Može, vidimo se u 7", "timestamp_iso": _ts(0)},
            },
        )
        await feed(rig, own)
        assert rig.rows() == [("done", None)]
        kind, _, best, sent_id = outcome(rig.appconn, view.id)
        assert (kind, best, sent_id) == ("used", "Može, vidimo se u 7", "OWN1")
        counter = rig.appconn.execute("SELECT outcomes_since_distill FROM chat_ai WHERE chat_jid=?", (ANA,)).fetchone()
        assert counter == (1,)  # counts toward the distill like any ready draft
        assert rig.events()[-1] == {"chat_jid": ANA, "draft": None}

    run(settings, scenario)


def test_agent_draft_edited_and_hinted(settings) -> None:
    from wa_app.ai import learning

    async def scenario(rig):
        view = rig.worker.push(ANA, "Dolazim oko osam navečer", None)
        learning.set_hint(ANA, view.id)
        kind = learning.record_own_message(rig.appconn, ANA, "OWN2", "Dolazim oko devet navečer", time.time())
        assert kind in ("used", "edited")
        assert outcome(rig.appconn, view.id) is not None

    run(settings, scenario)


# --- ai toggle ------------------------------------------------------------------------------------------------------


def test_ai_toggle(agent: TestClient) -> None:
    path = f"/api/agent/chats/{ANA}/ai"
    assert agent.get(path, headers=BEARER).json()["ai"]["enabled"] is False
    on = agent.put(path, json={"enabled": True}, headers=BEARER)
    assert on.status_code == 200 and on.json()["ai"]["enabled"] is True and on.json()["ai"]["chat_jid"] == ANA
    assert agent.get(path, headers=BEARER).json()["ai"]["enabled"] is True
    assert agent.put(path, json={"enabled": False}, headers=BEARER).json()["ai"]["enabled"] is False


def test_ai_toggle_rules(agent: TestClient) -> None:
    path = f"/api/agent/chats/{ANA}/ai"
    assert agent.put(path, json={"enabled": "yes"}, headers=BEARER).status_code == 422
    assert agent.put(path, json={}, headers=BEARER).status_code == 422
    assert agent.put(path, json=[True], headers=BEARER).status_code == 422
    # only `enabled` can be changed here; other chat_ai fields in the body are ignored
    agent.put(path, json={"enabled": True, "local_only": True, "note": "x"}, headers=BEARER)
    got = agent.get(path, headers=BEARER).json()["ai"]
    assert got["local_only"] is False and got["note"] is None
    assert agent.get("/api/agent/chats/385910000555@s.whatsapp.net/ai", headers=BEARER).status_code == 404
    assert (
        agent.put("/api/agent/chats/385910000555@s.whatsapp.net/ai", json={"enabled": True}, headers=BEARER).status_code
        == 404
    )
    assert agent.get(path).status_code == 401
    assert "key" not in agent.get(path, headers=BEARER).text.lower()


# --- no send path ---------------------------------------------------------------------------------------------------


def test_agent_module_has_no_send_path() -> None:
    code = "import sys, wa_app.agent_api\nprint('wa_app.sending' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_agent_routes_never_send(agent: TestClient, monkeypatch) -> None:
    from wa_app import sending

    calls: list = []
    for name in ("send_text", "send_reaction", "mark_read"):
        monkeypatch.setattr(sending, name, lambda *a, **k: calls.append(a))
    push(agent)
    agent.put(f"/api/agent/chats/{ANA}/ai", json={"enabled": True}, headers=BEARER)
    agent.get(f"/api/agent/chats/{ANA}/ai", headers=BEARER)
    assert calls == []

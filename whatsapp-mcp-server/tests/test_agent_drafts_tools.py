"""The `drafts` toolset: pushes drafts into wa-app and flips its automatic drafts. Nothing here can send."""

import importlib
import json

import pytest
import requests

from lib import wa_app
from tests.test_main_tools import reload_main, tool_names

TOKEN = "test-agent-token"
ANA = "385910000001@s.whatsapp.net"


class FakeResponse:
    def __init__(self, status: int, body: object) -> None:
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> object:
        return self._body


@pytest.fixture
def app_env(monkeypatch):
    monkeypatch.setenv("WA_APP_AGENT_TOKEN", TOKEN)
    monkeypatch.delenv("WA_APP_URL", raising=False)


@pytest.fixture
def http(monkeypatch):
    """Capture requests.request; set `http.reply` (or `http.error`) per test."""

    class Rig:
        calls: list = []
        reply: FakeResponse | None = FakeResponse(200, {})
        error: Exception | None = None

    def fake(method, url, **kwargs):
        Rig.calls.append((method, url, kwargs))
        if Rig.error:
            raise Rig.error
        return Rig.reply

    Rig.calls = []
    monkeypatch.setattr(wa_app.requests, "request", fake)
    return Rig


# --- registration -------------------------------------------------------------------------------------------------


def test_drafts_toolset_is_opt_in(monkeypatch):
    assert not {"push_draft", "set_auto_drafts", "get_auto_drafts"} & tool_names(reload_main(monkeypatch))
    names = tool_names(reload_main(monkeypatch, "core,drafts"))
    assert {"push_draft", "set_auto_drafts", "get_auto_drafts"} <= names
    assert "send_message" not in names


def test_all_includes_drafts(monkeypatch):
    assert {"push_draft", "set_auto_drafts", "get_auto_drafts"} <= tool_names(reload_main(monkeypatch, "all"))


def test_annotations(monkeypatch):
    main = reload_main(monkeypatch, "drafts")
    tools = {t.name: t for t in main.mcp._tool_manager.list_tools()}
    assert set(tools) == {"push_draft", "set_auto_drafts", "get_auto_drafts"}
    push = tools["push_draft"].annotations
    assert (push.read_only_hint, push.idempotent_hint, push.open_world_hint) == (False, False, False)
    assert push.destructive_hint is False
    setter = tools["set_auto_drafts"].annotations
    assert setter.read_only_hint is False and setter.open_world_hint is False
    getter = tools["get_auto_drafts"].annotations
    assert getter.read_only_hint is True and getter.open_world_hint is False
    for t in tools.values():
        assert t.title and t.annotations.title == t.title


def test_push_draft_docstring_says_it_never_sends(monkeypatch):
    main = reload_main(monkeypatch, "drafts")
    doc = main.push_draft.__doc__ or ""
    assert "never sends" in doc and "JID" in doc


def test_tool_source_is_wrapper_only():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1].joinpath("main.py").read_text()
    assert "@mcp.tool(" not in src


# --- HTTP ---------------------------------------------------------------------------------------------------------


def test_push_draft_posts_with_bearer(app_env, http, monkeypatch):
    http.reply = FakeResponse(201, {"draft": {"id": 7, "alternatives": ["Vidimo se u 7"]}, "chat_name": "Ana K."})
    out = reload_main(monkeypatch, "drafts").push_draft(ANA, "Vidimo se u 7")
    ((method, url, kw),) = http.calls
    assert (method, url) == ("POST", "http://127.0.0.1:8085/api/agent/drafts")
    assert kw["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert kw["json"] == {"chat": ANA, "text": "Vidimo se u 7"}
    assert kw["timeout"] > 0
    assert "Ana K." in out and "not sent" in out.lower()
    assert TOKEN not in out


def test_push_draft_passes_reply_to_and_custom_url(app_env, http, monkeypatch):
    monkeypatch.setenv("WA_APP_URL", "http://wa-app.invalid:9000/")
    http.reply = FakeResponse(201, {"draft": {"id": 8}, "chat_name": "Ekipa"})
    reload_main(monkeypatch, "drafts").push_draft(ANA, "Hej", reply_to="E09")
    ((_, url, kw),) = http.calls
    assert url == "http://wa-app.invalid:9000/api/agent/drafts"
    assert kw["json"] == {"chat": ANA, "text": "Hej", "reply_to": "E09"}


def test_set_auto_drafts(app_env, http, monkeypatch):
    http.reply = FakeResponse(200, {"ai": {"chat_jid": ANA, "enabled": True}})
    main = reload_main(monkeypatch, "drafts")
    out = main.set_auto_drafts(ANA, True)
    ((method, url, kw),) = http.calls
    assert (method, url) == ("PUT", f"http://127.0.0.1:8085/api/agent/chats/{ANA}/ai")
    assert kw["json"] == {"enabled": True} and kw["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert "on" in out.lower()
    http.reply = FakeResponse(200, {"ai": {"chat_jid": ANA, "enabled": False}})
    assert "off" in main.set_auto_drafts(ANA, False).lower()


def test_get_auto_drafts(app_env, http, monkeypatch):
    http.reply = FakeResponse(200, {"ai": {"chat_jid": ANA, "enabled": True}})
    out = reload_main(monkeypatch, "drafts").get_auto_drafts(ANA)
    ((method, url, kw),) = http.calls
    assert (method, url) == ("GET", f"http://127.0.0.1:8085/api/agent/chats/{ANA}/ai")
    assert "json" not in kw or kw["json"] is None
    assert "on" in out.lower()


def test_jid_is_escaped_in_the_path(app_env, http, monkeypatch):
    http.reply = FakeResponse(200, {"ai": {"enabled": False}})
    reload_main(monkeypatch, "drafts").get_auto_drafts("a?b c@lid")
    assert http.calls[0][1].endswith("/api/agent/chats/a%3Fb%20c@lid/ai")


def test_bad_jids_never_reach_the_app(app_env, http, monkeypatch):
    main = reload_main(monkeypatch, "drafts")
    for bad in ["", "   ", "a/b@lid", "../x"]:
        for out in (main.push_draft(bad, "x"), main.set_auto_drafts(bad, True), main.get_auto_drafts(bad)):
            assert "chat_jid must be" in out
    assert http.calls == []


def test_unknown_agent_route_is_not_reported_as_a_missing_token(app_env, http, monkeypatch):
    http.reply = FakeResponse(404, {"error": "Unknown agent route"})
    out = reload_main(monkeypatch, "drafts").get_auto_drafts(ANA)
    assert "older" in out and "WA_APP_AGENT_TOKEN" not in out


def test_relayed_error_is_truncated_and_any_exception_is_caught(app_env, http, monkeypatch):
    main = reload_main(monkeypatch, "drafts")
    http.reply = FakeResponse(422, {"error": "e" * 1000})
    assert len(main.push_draft(ANA, "x")) <= 300
    http.error = RuntimeError(f"weird {TOKEN}")
    assert main.push_draft(ANA, "x") == "wa-app isn't reachable"


# --- errors -------------------------------------------------------------------------------------------------------


def test_missing_token(monkeypatch, http):
    monkeypatch.delenv("WA_APP_AGENT_TOKEN", raising=False)
    main = reload_main(monkeypatch, "drafts")
    for out in (main.push_draft(ANA, "x"), main.set_auto_drafts(ANA, True), main.get_auto_drafts(ANA)):
        assert out == "wa-app drafts aren't set up (WA_APP_AGENT_TOKEN)"
    assert http.calls == []  # nothing goes out without a token
    monkeypatch.setenv("WA_APP_AGENT_TOKEN", "   ")
    assert main.push_draft(ANA, "x") == "wa-app drafts aren't set up (WA_APP_AGENT_TOKEN)"


@pytest.mark.parametrize("error", [requests.ConnectionError(f"boom {TOKEN}"), requests.Timeout(f"slow {TOKEN}")])
def test_app_down(app_env, http, monkeypatch, error):
    http.error = error
    out = reload_main(monkeypatch, "drafts").push_draft(ANA, "x")
    assert out == "wa-app isn't reachable"
    assert TOKEN not in out


def test_server_errors_are_relayed_without_the_token(app_env, http, monkeypatch):
    main = reload_main(monkeypatch, "drafts")
    http.reply = FakeResponse(404, {"error": "Unknown chat"})
    assert "Unknown chat" in main.push_draft("nobody@s.whatsapp.net", "x")
    http.reply = FakeResponse(422, {"error": "'text' must be 1 to 4096 characters"})
    assert "4096" in main.push_draft(ANA, "")
    http.reply = FakeResponse(401, {"error": "Bad token"})
    out = main.push_draft(ANA, "x")
    assert "token" in out.lower() and TOKEN not in out
    http.reply = FakeResponse(404, {"error": "Not found"})
    assert "WA_APP_AGENT_TOKEN" in main.get_auto_drafts(ANA)  # the app has the feature off
    http.reply = FakeResponse(500, "<html>oops</html>")
    assert main.get_auto_drafts(ANA).startswith("wa-app answered 500")
    http.reply = FakeResponse(200, ["not", "a", "dict"])
    assert TOKEN not in main.get_auto_drafts(ANA)


def test_token_never_logged(app_env, http, monkeypatch, caplog):
    http.error = requests.ConnectionError(f"boom {TOKEN}")
    caplog.set_level("DEBUG")
    reload_main(monkeypatch, "drafts").push_draft(ANA, "x")
    assert TOKEN not in caplog.text


def teardown_module(module):  # leave main imported with the default toolsets for later tests
    importlib.reload(importlib.import_module("main"))

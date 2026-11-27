"""Draft context settings: how much chat a draft reads, how many own replies it learns from, About me."""

import pytest
from starlette.testclient import TestClient

from wa_app import appdb
from wa_app.ai import config as ai_config
from wa_app.ai.context import DraftContext, get_context, save_context


@pytest.fixture
def conn(settings):
    return appdb.connect(settings)


def test_defaults(conn) -> None:
    assert get_context(conn) == DraftContext(30, 30, "")


def test_save_partial(conn) -> None:
    assert save_context(conn, {"own_examples": 80}) == DraftContext(30, 80, "")
    assert save_context(conn, {"about_me": "Simon, 27"}) == DraftContext(30, 80, "Simon, 27")
    assert get_context(conn) == DraftContext(30, 80, "Simon, 27")


@pytest.mark.parametrize(
    "body",
    [
        {"context_messages": 9},
        {"context_messages": 101},
        {"own_examples": -1},
        {"own_examples": 101},
        {"about_me": "x" * 1501},
        {"context_messages": True},
        {"own_examples": "30"},
        {"about_me": 5},
    ],
)
def test_bounds(conn, body) -> None:
    with pytest.raises(ai_config.ConfigError):
        save_context(conn, body)
    assert get_context(conn) == DraftContext(30, 30, "")


def test_bad_input_changes_nothing(conn) -> None:
    with pytest.raises(ai_config.ConfigError):
        save_context(conn, {"context_messages": 50, "own_examples": 500})
    assert get_context(conn).context_messages == 30


def test_route_roundtrip(logged_in: TestClient) -> None:
    out = logged_in.put("/api/ai/context", json={"context_messages": 60, "about_me": "x"})
    assert out.status_code == 200
    assert out.json() == {"context": {"context_messages": 60, "own_examples": 30, "about_me": "x"}}
    assert logged_in.get("/api/ai/context").json() == out.json()
    bad = logged_in.put("/api/ai/context", json={"context_messages": 500})
    assert bad.status_code == 422 and "error" in bad.json()
    assert logged_in.put("/api/ai/context", json=[1]).status_code == 422


def test_route_needs_login_and_origin(client: TestClient) -> None:
    assert client.get("/api/ai/context").status_code == 401
    assert client.put("/api/ai/context", json={}).status_code == 401

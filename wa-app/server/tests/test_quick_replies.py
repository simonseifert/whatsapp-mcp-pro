"""Quick replies: shortcut and text validation, CRUD, usage count."""

from typing import Any

import pytest
from starlette.testclient import TestClient


def add(client: TestClient, shortcut: str, text: str = "Hello") -> Any:
    return client.post("/api/quick-replies", json={"shortcut": shortcut, "text": text})


def test_create_list_update_delete(logged_in: TestClient) -> None:
    assert add(logged_in, "thanks", "Thanks a lot").status_code == 200
    assert add(logged_in, "omw", "On my way").status_code == 200
    rows = logged_in.get("/api/quick-replies").json()["quick_replies"]
    assert rows == [
        {"shortcut": "omw", "text": "On my way", "used": 0},
        {"shortcut": "thanks", "text": "Thanks a lot", "used": 0},
    ]
    assert logged_in.put("/api/quick-replies/omw", json={"text": "Be there soon"}).status_code == 200
    assert logged_in.get("/api/quick-replies").json()["quick_replies"][0]["text"] == "Be there soon"
    assert logged_in.delete("/api/quick-replies/omw").status_code == 200
    assert [r["shortcut"] for r in logged_in.get("/api/quick-replies").json()["quick_replies"]] == ["thanks"]


@pytest.mark.parametrize("shortcut", ["", "Hello", "has space", "a/b", "x" * 25, "café", None, 5])
def test_bad_shortcut_is_rejected(logged_in: TestClient, shortcut: object) -> None:
    assert logged_in.post("/api/quick-replies", json={"shortcut": shortcut, "text": "x"}).status_code == 422


@pytest.mark.parametrize("shortcut", ["a", "x" * 24, "a_b-9"])
def test_good_shortcut_is_accepted(logged_in: TestClient, shortcut: str) -> None:
    assert add(logged_in, shortcut).status_code == 200


def test_text_rules(logged_in: TestClient) -> None:
    assert logged_in.post("/api/quick-replies", json={"shortcut": "a", "text": "  "}).status_code == 422
    assert logged_in.post("/api/quick-replies", json={"shortcut": "a", "text": "x" * 4097}).status_code == 422
    assert logged_in.post("/api/quick-replies", json={"shortcut": "a", "text": "x" * 4096}).status_code == 200
    assert logged_in.put("/api/quick-replies/a", json={"text": "x" * 4097}).status_code == 422
    assert logged_in.put("/api/quick-replies/a", json={}).status_code == 422


def test_duplicate_and_missing(logged_in: TestClient) -> None:
    assert add(logged_in, "dup").status_code == 200
    assert add(logged_in, "dup").status_code == 409
    assert logged_in.put("/api/quick-replies/nope", json={"text": "x"}).status_code == 404
    assert logged_in.delete("/api/quick-replies/nope").status_code == 404
    assert logged_in.post("/api/quick-replies/nope/use").status_code == 404


def test_use_counts_and_orders(logged_in: TestClient) -> None:
    for s in ("b", "a", "c"):
        add(logged_in, s)
    logged_in.post("/api/quick-replies/c/use")
    logged_in.post("/api/quick-replies/c/use")
    logged_in.post("/api/quick-replies/b/use")
    rows = logged_in.get("/api/quick-replies").json()["quick_replies"]
    assert [(r["shortcut"], r["used"]) for r in rows] == [("c", 2), ("b", 1), ("a", 0)]


def test_needs_a_session(client: TestClient) -> None:
    assert client.get("/api/quick-replies").status_code == 401

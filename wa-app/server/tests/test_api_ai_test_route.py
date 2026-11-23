import pytest
from starlette.testclient import TestClient

from tests import conftest as fx
from wa_app.ai import backends

SECRET = "sk-test-FICTIONAL-0123456789"
OPENAI = {"kind": "openai", "label": "Local Ollama", "base_url": "http://127.0.0.1:11434/v1", "model": "llama-test"}


def _make(client: TestClient) -> int:
    return client.put("/api/ai/backends", json={**OPENAI, "api_key": SECRET}).json()["backend"]["id"]


def test_ok_returns_sample_and_ms_without_key(logged_in: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    async def fake(cfg, secret, prompt, settings, **kw):  # noqa: ANN001, ANN003
        seen.update(secret=secret, user=prompt.user, timeout=kw.get("timeout"))
        return '{"alternatives": ["Može, vidimo se u 18.", "Može!"]}'

    monkeypatch.setattr(backends, "complete", fake)
    resp = logged_in.post(f"/api/ai/backends/{_make(logged_in)}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["sample"] == "Može, vidimo se u 18."
    assert isinstance(body["ms"], int) and body["ms"] >= 0
    assert seen["secret"] == SECRET and seen["timeout"] == 60
    assert SECRET not in resp.text and SECRET not in seen["user"]


def test_failure_is_http_200_with_error_text(logged_in: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*a, **kw):  # noqa: ANN002, ANN003
        raise backends.BackendError("Couldn't reach the AI")

    monkeypatch.setattr(backends, "complete", boom)
    resp = logged_in.post(f"/api/ai/backends/{_make(logged_in)}/test")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False and resp.json()["error"] == "Couldn't reach the AI"
    assert isinstance(resp.json()["ms"], int)


def test_unparseable_answer_is_a_failure(logged_in: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def junk(*a, **kw):  # noqa: ANN002, ANN003
        return "not json"

    monkeypatch.setattr(backends, "complete", junk)
    body = logged_in.post(f"/api/ai/backends/{_make(logged_in)}/test").json()
    assert body["ok"] is False and "not json" not in body["error"]


def test_unknown_id_login_and_origin(client: TestClient) -> None:
    assert client.post("/api/ai/backends/1/test").status_code == 401
    client.post("/api/auth/setup", json={"passphrase": fx.PASSPHRASE})
    client.post("/api/auth/login", json={"passphrase": fx.PASSPHRASE})
    assert client.post("/api/ai/backends/1/test", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/ai/backends/999/test").status_code == 404
    assert client.post("/api/ai/backends/abc/test").status_code == 404

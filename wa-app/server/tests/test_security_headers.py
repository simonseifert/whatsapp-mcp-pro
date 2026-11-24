"""Security headers on every response, and the request body cap."""

import pytest
from starlette.testclient import TestClient

from tests.conftest import ANA, PASSPHRASE
from wa_app import auth, headers
from wa_app.main import create_app

EXPECTED = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "x-frame-options": "DENY",
    "cross-origin-opener-policy": "same-origin",
}


def _assert_headers(response) -> None:
    for name, value in EXPECTED.items():
        assert response.headers[name] == value, name
    csp = response.headers["content-security-policy"]
    assert csp == headers.CSP
    for part in (
        "default-src 'self'",
        "img-src 'self' blob: data:",
        "media-src 'self' blob:",
        "connect-src 'self'",
        "style-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "object-src 'none'",
    ):
        assert part in csp
    assert "unsafe-inline" not in csp


@pytest.fixture
def spa(settings, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>x</title>")
    with TestClient(create_app(settings, dist=dist), base_url="https://testserver") as c:
        yield c


def test_spa_and_fallback_routes_carry_the_headers(spa):
    _assert_headers(spa.get("/"))
    _assert_headers(spa.get("/some/client/route"))


def test_api_responses_carry_the_headers(spa):
    _assert_headers(spa.get("/api/health"))
    _assert_headers(spa.get("/api/chats"))  # 401
    assert spa.get("/api/chats").status_code == 401
    _assert_headers(spa.get("/api/nope"))  # 404
    _assert_headers(spa.post("/api/auth/login", json={"passphrase": "x"}, headers={"Origin": "https://evil.example"}))


def test_headers_on_the_build_missing_503(client):
    response = client.get("/")
    assert response.status_code in (200, 503)
    _assert_headers(response)


def test_theme_init_is_a_file_not_an_inline_script():
    from pathlib import Path

    index = (Path(__file__).resolve().parents[2] / "web" / "index.html").read_text()
    assert "<script>" not in index
    assert 'src="/theme-init.js"' in index


# ---------- body size ----------


@pytest.mark.parametrize("path", ["/api/auth/login", "/api/auth/setup", "/api/chats/x/send", "/api/ai/backends"])
def test_oversize_content_length_gets_413_before_the_body_is_read(spa, path):
    big = b"x" * (auth.MAX_JSON_BODY + 1)
    response = spa.put(path, content=big) if path.endswith("backends") else spa.post(path, content=big)
    assert response.status_code == 413
    assert response.json() == {"error": "Body too large"}
    _assert_headers(response)


def test_413_is_sent_without_reading_the_body(settings):
    """Drive the ASGI app by hand: a declared length over the cap must be answered with receive never called."""
    import asyncio

    app = create_app(settings)
    sent: list[dict] = []

    async def receive():
        raise AssertionError("the body was read")

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/api/auth/login",
        "raw_path": b"/api/auth/login",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"content-length", str(auth.MAX_JSON_BODY + 1).encode())],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 443),
        "scheme": "https",
        "state": {},
    }
    asyncio.run(app(scope, receive, send))
    assert sent[0]["status"] == 413


def test_chunked_oversize_body_is_cut_off(spa):
    def chunks():
        for _ in range(10):
            yield b"x" * (auth.MAX_JSON_BODY // 4)

    response = spa.post("/api/auth/login", content=chunks())
    assert response.status_code == 413


def test_normal_bodies_still_pass(logged_in):
    assert logged_in.post("/api/auth/login", json={"passphrase": PASSPHRASE}).status_code == 200
    assert logged_in.post(f"/api/chats/{ANA}/read", json={"up_to": "2026-01-01T00:00:00Z"}).status_code != 413


def test_agent_api_is_capped_too(settings):
    from dataclasses import replace

    token = "t" * 40
    with TestClient(create_app(replace(settings, agent_token=token))) as c:
        response = c.post(
            "/api/agent/drafts", content=b"x" * (auth.MAX_JSON_BODY + 1), headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 413

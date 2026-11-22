from starlette.testclient import TestClient

from tests.conftest import PASSPHRASE
from wa_app import appdb, auth
from wa_app.config import Settings


def test_setup_only_once(client: TestClient) -> None:
    assert client.get("/api/auth/state").json() == {"setup_needed": True, "logged_in": False}
    assert client.post("/api/auth/setup", json={"passphrase": PASSPHRASE}).status_code == 200
    assert client.post("/api/auth/setup", json={"passphrase": "another passphrase"}).status_code == 409
    assert client.get("/api/auth/state").json() == {"setup_needed": False, "logged_in": False}


def test_setup_rejects_short_passphrase(client: TestClient) -> None:
    assert client.post("/api/auth/setup", json={"passphrase": "short"}).status_code == 400
    assert client.get("/api/auth/state").json()["setup_needed"] is True


def test_login_wrong_then_right(client: TestClient) -> None:
    client.post("/api/auth/setup", json={"passphrase": PASSPHRASE})
    wrong = client.post("/api/auth/login", json={"passphrase": "nope nope nope"})
    assert wrong.status_code == 401
    assert wrong.json() == {"error": "Wrong passphrase"}
    right = client.post("/api/auth/login", json={"passphrase": PASSPHRASE})
    assert right.status_code == 200
    cookie = right.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie or "samesite=strict" in cookie.lower()
    assert "Secure" in cookie
    assert client.get("/api/auth/state").json() == {"setup_needed": False, "logged_in": True}


def test_rate_limit(client: TestClient) -> None:
    client.post("/api/auth/setup", json={"passphrase": PASSPHRASE})
    for _ in range(5):
        assert client.post("/api/auth/login", json={"passphrase": "wrong wrong wrong"}).status_code == 401
    blocked = client.post("/api/auth/login", json={"passphrase": PASSPHRASE})
    assert blocked.status_code == 429


def test_api_requires_session(client: TestClient) -> None:
    resp = client.get("/api/chats")
    assert resp.status_code == 401
    assert resp.json() == {"error": "Not logged in"}
    assert client.get("/api/health").json() == {"ok": True}


def test_origin_checked(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/setup",
        json={"passphrase": PASSPHRASE},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403
    same = client.post(
        "/api/auth/setup",
        json={"passphrase": PASSPHRASE},
        headers={"Origin": "https://testserver"},
    )
    assert same.status_code == 200


def test_logout_kills_server_session(logged_in: TestClient) -> None:
    old = logged_in.cookies.get("wa_session")
    assert old
    assert logged_in.post("/api/auth/logout").status_code == 200
    logged_in.cookies.clear()
    logged_in.cookies.set("wa_session", old)
    assert logged_in.get("/api/auth/state").json()["logged_in"] is False
    assert logged_in.get("/api/chats").status_code == 401


def test_session_token_stored_hashed(settings: Settings) -> None:
    conn = appdb.connect(settings)
    token = auth.new_session(conn, "ua")
    stored = conn.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert stored != token and len(stored) == 64
    assert auth.session_valid(conn, token)
    auth.end_session(conn, token)
    assert not auth.session_valid(conn, token)


def test_expired_session_invalid(settings: Settings) -> None:
    conn = appdb.connect(settings)
    token = auth.new_session(conn, "ua")
    conn.execute("UPDATE sessions SET expires_at = 1")
    assert not auth.session_valid(conn, token)

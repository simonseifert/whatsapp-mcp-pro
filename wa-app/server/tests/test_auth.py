import asyncio

from starlette.testclient import TestClient

from tests.conftest import PASSPHRASE
from wa_app import appdb, auth
from wa_app.config import Settings
from wa_app.main import create_app


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


def _async_client(settings: Settings, peer: tuple[str, int] = ("127.0.0.1", 50000)):
    import httpx2

    transport = httpx2.ASGITransport(app=create_app(settings), client=peer)
    return httpx2.AsyncClient(transport=transport, base_url="https://testserver")


def test_parallel_wrong_logins_cannot_outrun_the_limit(settings: Settings, monkeypatch) -> None:
    verified = 0
    real = auth.check_passphrase

    def counting(conn, passphrase):
        nonlocal verified
        verified += 1
        return real(conn, passphrase)

    monkeypatch.setattr(auth, "check_passphrase", counting)

    async def scenario() -> int:
        async with _async_client(settings) as c:
            await c.post("/api/auth/setup", json={"passphrase": PASSPHRASE})
            await asyncio.gather(
                *[c.post("/api/auth/login", json={"passphrase": "wrong wrong wrong"}) for _ in range(10)]
            )
            return (await c.post("/api/auth/login", json={"passphrase": PASSPHRASE})).status_code

    assert asyncio.run(scenario()) == 429
    assert verified <= 5


def test_concurrent_setups_yield_one_winner(settings: Settings) -> None:
    async def scenario():
        async with _async_client(settings) as c:
            first, second = await asyncio.gather(
                c.post("/api/auth/setup", json={"passphrase": PASSPHRASE}),
                c.post("/api/auth/setup", json={"passphrase": "a different passphrase"}),
            )
            login = await c.post("/api/auth/login", json={"passphrase": PASSPHRASE})
            return sorted([first.status_code, second.status_code]), login.status_code

    statuses, login = asyncio.run(scenario())
    assert statuses == [200, 409]
    assert login == 200


def test_set_passphrase_never_overwrites(settings: Settings) -> None:
    conn = appdb.connect(settings)
    assert auth.set_passphrase(conn, PASSPHRASE) is True
    assert auth.set_passphrase(conn, "a different passphrase") is False
    assert auth.check_passphrase(conn, PASSPHRASE)


def test_rate_limit_keys_on_last_forwarded_hop_behind_loopback(settings: Settings) -> None:
    async def scenario():
        async with _async_client(settings) as c:
            await c.post("/api/auth/setup", json={"passphrase": PASSPHRASE})
            for i in range(5):  # forged, rotating first hop; the proxy-appended last hop is the same
                await c.post(
                    "/api/auth/login",
                    json={"passphrase": "wrong wrong wrong"},
                    headers={"X-Forwarded-For": f"1.1.1.{i}, 100.64.0.5"},
                )
            blocked = await c.post(
                "/api/auth/login", json={"passphrase": PASSPHRASE}, headers={"X-Forwarded-For": "1.1.1.1, 100.64.0.5"}
            )
            other = await c.post(
                "/api/auth/login", json={"passphrase": PASSPHRASE}, headers={"X-Forwarded-For": "1.1.1.1, 100.64.0.6"}
            )
            return blocked.status_code, other.status_code

    assert asyncio.run(scenario()) == (429, 200)


def test_forwarded_for_ignored_from_non_loopback_peer(settings: Settings) -> None:
    async def scenario():
        async with _async_client(settings, peer=("203.0.113.9", 50000)) as c:
            await c.post("/api/auth/setup", json={"passphrase": PASSPHRASE})
            for i in range(5):
                await c.post(
                    "/api/auth/login",
                    json={"passphrase": "wrong wrong wrong"},
                    headers={"X-Forwarded-For": f"198.51.100.{i}"},
                )
            return (
                await c.post(
                    "/api/auth/login", json={"passphrase": PASSPHRASE}, headers={"X-Forwarded-For": "198.51.100.99"}
                )
            ).status_code

    assert asyncio.run(scenario()) == 429


def test_logout_all_deletes_every_session_and_clears_cookie(logged_in: TestClient) -> None:
    other = logged_in.post("/api/auth/login", json={"passphrase": PASSPHRASE})
    assert other.status_code == 200
    token_a = logged_in.cookies.get("wa_session")
    assert token_a
    resp = logged_in.post("/api/auth/logout-all")
    assert resp.status_code == 200
    assert "wa_session" in resp.headers["set-cookie"]
    logged_in.cookies.clear()
    logged_in.cookies.set("wa_session", token_a)
    assert logged_in.get("/api/chats").status_code == 401


def test_logout_all_needs_a_session(client: TestClient) -> None:
    assert client.post("/api/auth/logout-all").status_code == 401


def test_end_all_sessions_counts_rows(settings: Settings) -> None:
    conn = appdb.connect(settings)
    auth.new_session(conn, "phone")
    auth.new_session(conn, "laptop")
    assert auth.end_all_sessions(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_about_names_a_version(logged_in: TestClient) -> None:
    body = logged_in.get("/api/about").json()
    assert isinstance(body["version"], str) and body["version"]

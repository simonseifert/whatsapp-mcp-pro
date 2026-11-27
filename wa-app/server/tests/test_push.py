import sqlite3
import stat
import time
from unittest.mock import MagicMock, patch

import pytest
from pywebpush import WebPushException
from starlette.testclient import TestClient

from tests.conftest import PASSPHRASE
from wa_app import push
from wa_app.config import Settings

GOOD = "https://web.push.apple.com/abc"


def sub(endpoint: str = GOOD) -> dict:
    return {"endpoint": endpoint, "keys": {"p256dh": "BKey", "auth": "aKey"}}


def rows(settings: Settings) -> list[tuple]:
    db = sqlite3.connect(settings.data_dir / "app.db")
    try:
        return db.execute("SELECT endpoint, session_hash, failures FROM push_subscriptions").fetchall()
    finally:
        db.close()


def failure(status: int) -> WebPushException:
    return WebPushException("boom", response=MagicMock(status_code=status))


@pytest.fixture
def push_settings(settings: Settings) -> Settings:
    from dataclasses import replace

    return replace(settings, push_subject="mailto:test@example.com")


def test_subscribe_rejects_foreign_host(logged_in: TestClient, settings: Settings) -> None:
    assert logged_in.post("/api/push/subscribe", json=sub("https://evil.example/push")).status_code == 422
    assert logged_in.post("/api/push/subscribe", json=sub("http://web.push.apple.com/x")).status_code == 422
    assert logged_in.post("/api/push/subscribe", json=sub("https://push.apple.com.evil.example/x")).status_code == 422
    assert rows(settings) == []


def test_subscribe_accepts_known_hosts(logged_in: TestClient) -> None:
    for host in (
        "web.push.apple.com",
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "x.notify.windows.com",
    ):
        assert logged_in.post("/api/push/subscribe", json=sub(f"https://{host}/p")).status_code == 200


def test_subscribe_upserts_and_binds_session(logged_in: TestClient, settings: Settings) -> None:
    assert logged_in.post("/api/push/subscribe", json=sub()).status_code == 200
    assert logged_in.post("/api/push/subscribe", json=sub()).status_code == 200
    found = rows(settings)
    assert len(found) == 1
    db = sqlite3.connect(settings.data_dir / "app.db")
    assert found[0][1] == db.execute("SELECT token_hash FROM sessions").fetchone()[0]
    db.close()


def test_subscribe_validates_body(logged_in: TestClient) -> None:
    assert logged_in.post("/api/push/subscribe", json={"endpoint": GOOD}).status_code == 422
    assert (
        logged_in.post("/api/push/subscribe", json={"endpoint": GOOD, "keys": {"p256dh": 1, "auth": "a"}}).status_code
        == 422
    )
    assert logged_in.post("/api/push/subscribe", json=[1]).status_code == 422


def test_unsubscribe(logged_in: TestClient, settings: Settings) -> None:
    logged_in.post("/api/push/subscribe", json=sub())
    assert logged_in.post("/api/push/unsubscribe", json={"endpoint": GOOD}).status_code == 200
    assert rows(settings) == []


def test_logout_drops_that_sessions_subscriptions(logged_in: TestClient, settings: Settings) -> None:
    logged_in.post("/api/push/subscribe", json=sub())
    db = sqlite3.connect(settings.data_dir / "app.db", isolation_level=None)
    db.execute(
        "INSERT INTO push_subscriptions (endpoint, p256dh, auth, session_hash, created_at) "
        "VALUES ('https://fcm.googleapis.com/other', 'p', 'a', 'someone-else', 1)"
    )
    db.close()
    assert logged_in.post("/api/auth/logout").status_code == 200
    assert [r[0] for r in rows(settings)] == ["https://fcm.googleapis.com/other"]


def test_logout_all_drops_every_subscription(logged_in: TestClient, settings: Settings) -> None:
    logged_in.post("/api/push/subscribe", json=sub())
    logged_in.post("/api/push/subscribe", json=sub("https://fcm.googleapis.com/two"))
    assert logged_in.post("/api/auth/logout-all").status_code == 200
    assert rows(settings) == []


def test_key_503_without_subject(logged_in: TestClient) -> None:
    resp = logged_in.get("/api/push/key")
    assert resp.status_code == 503
    assert resp.json() == {"error": "Push not configured"}


def test_key_with_subject(push_settings: Settings) -> None:
    from wa_app.main import create_app

    with TestClient(create_app(push_settings), base_url="https://testserver") as c:
        c.post("/api/auth/setup", json={"passphrase": PASSPHRASE})
        c.post("/api/auth/login", json={"passphrase": PASSPHRASE})
        key = c.get("/api/push/key").json()["key"]
        assert key == push.vapid_keys(push_settings)[1]
        assert len(key) == 87 and "=" not in key  # 65-byte uncompressed point, URL-safe base64 unpadded


def test_vapid_file_is_0600(push_settings: Settings) -> None:
    path, public = push.vapid_keys(push_settings)
    assert path.endswith("vapid.pem")
    assert stat.S_IMODE(push_settings.data_dir.joinpath("vapid.pem").stat().st_mode) == 0o600
    assert push.vapid_keys(push_settings) == (path, public)  # second call reuses the file


def _conn_with_row(settings: Settings) -> sqlite3.Connection:
    from wa_app import appdb

    conn = appdb.connect(settings)
    _session(conn, "h", time.time() + 3600)
    push.save_subscription(conn, endpoint=GOOD, p256dh="p", auth="a", session_hash="h")
    return conn


def _session(conn: sqlite3.Connection, token_hash: str, expires_at: float) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sessions (token_hash, created_at, expires_at, user_agent) VALUES (?, 0, ?, NULL)",
        (token_hash, int(expires_at)),
    )


def test_send_skips_expired_and_unknown_sessions(push_settings: Settings) -> None:
    conn = _conn_with_row(push_settings)
    _session(conn, "old", time.time() - 5)
    push.save_subscription(conn, endpoint="https://web.push.apple.com/old", p256dh="p", auth="a", session_hash="old")
    push.save_subscription(conn, endpoint="https://web.push.apple.com/gone", p256dh="p", auth="a", session_hash="none")
    with patch("wa_app.push.webpush", return_value=MagicMock(status_code=201)) as sent:
        assert push.send(push_settings, conn, {"title": "t"}) == 1
    assert sent.call_count == 1
    assert sent.call_args.kwargs["subscription_info"]["endpoint"] == GOOD


def test_send_counts_accepted(push_settings: Settings) -> None:
    conn = _conn_with_row(push_settings)
    with patch("wa_app.push.webpush", return_value=MagicMock(status_code=201)) as sent:
        assert push.send(push_settings, conn, {"title": "t", "body": "b"}) == 1
    assert sent.call_args.kwargs["ttl"] == 3600
    assert sent.call_args.kwargs["timeout"] == 10
    assert sent.call_args.kwargs["headers"] == {"Urgency": "high"}


def test_send_drops_gone_endpoint(push_settings: Settings) -> None:
    conn = _conn_with_row(push_settings)
    for status in (404, 410):
        push.save_subscription(conn, endpoint=GOOD, p256dh="p", auth="a", session_hash="h")
        with patch("wa_app.push.webpush", side_effect=failure(status)):
            assert push.send(push_settings, conn, {"title": "t"}) == 0
        assert rows(push_settings) == []


def test_send_drops_after_five_failures(push_settings: Settings) -> None:
    conn = _conn_with_row(push_settings)
    with patch("wa_app.push.webpush", side_effect=failure(500)):
        for _ in range(4):
            assert push.send(push_settings, conn, {"title": "t"}) == 0
        assert len(rows(push_settings)) == 1 and rows(push_settings)[0][2] == 4
        push.send(push_settings, conn, {"title": "t"})
    assert rows(push_settings) == []


def test_success_resets_failures(push_settings: Settings) -> None:
    conn = _conn_with_row(push_settings)
    with patch("wa_app.push.webpush", side_effect=failure(500)):
        push.send(push_settings, conn, {"title": "t"})
    with patch("wa_app.push.webpush", return_value=MagicMock(status_code=201)):
        push.send(push_settings, conn, {"title": "t"})
    assert rows(push_settings)[0][2] == 0


def test_test_route_sends_fixed_payload(push_settings: Settings) -> None:
    from wa_app.main import create_app

    with TestClient(create_app(push_settings), base_url="https://testserver") as c:
        c.post("/api/auth/setup", json={"passphrase": PASSPHRASE})
        c.post("/api/auth/login", json={"passphrase": PASSPHRASE})
        c.post("/api/push/subscribe", json=sub())
        with patch("wa_app.push.webpush", return_value=MagicMock(status_code=201)) as sent:
            assert c.post("/api/push/test").json() == {"sent": 1}
        import json

        payload = json.loads(sent.call_args.kwargs["data"])
        assert payload["title"] == "wa-app" and payload["body"] == "Notifications work."


def test_test_route_503_without_subject(logged_in: TestClient) -> None:
    assert logged_in.post("/api/push/test").status_code == 503

"""Web Push: VAPID keys, subscriptions in app.db, and the blocking send. Never logs payloads."""

import base64
import json
import logging
import os
import sqlite3
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from . import appdb
from .config import Settings

logger = logging.getLogger(__name__)

ALLOWED_PUSH_HOSTS = ("push.apple.com", "fcm.googleapis.com", "push.services.mozilla.com", "notify.windows.com")
MAX_FAILURES = 5
PUSH_TIMEOUT = 10  # seconds; webpush would otherwise wait forever on a stalled endpoint
GONE = (404, 410)
MAX_FIELD = 512  # p256dh and auth are ~87 and ~22 characters
MAX_ENDPOINT = 2048


def endpoint_allowed(endpoint: str) -> bool:
    if len(endpoint) > MAX_ENDPOINT:
        return False
    parts = urlsplit(endpoint)
    host = parts.hostname or ""
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_PUSH_HOSTS)


def vapid_keys(settings: Settings) -> tuple[str, str]:
    """(path to the private PEM, public key as URL-safe base64 of the uncompressed point). Creates the PEM, mode 0600."""
    path = settings.data_dir / "vapid.pem"
    if not path.exists():
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        vapid = Vapid()
        vapid.generate_keys()
        pem = vapid.private_pem()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
    vapid = Vapid.from_file(str(path))
    raw = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return str(path), base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def save_subscription(appconn: sqlite3.Connection, *, endpoint: str, p256dh: str, auth: str, session_hash: str) -> None:
    appconn.execute(
        "INSERT INTO push_subscriptions (endpoint, p256dh, auth, session_hash, created_at, failures) "
        "VALUES (?, ?, ?, ?, ?, 0) ON CONFLICT(endpoint) DO UPDATE SET "
        "p256dh=excluded.p256dh, auth=excluded.auth, session_hash=excluded.session_hash, failures=0",
        (endpoint, p256dh, auth, session_hash, int(time.time())),
    )


def drop_subscription(appconn: sqlite3.Connection, endpoint: str) -> None:
    appconn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))


def drop_for_session(appconn: sqlite3.Connection, session_hash: str) -> None:
    appconn.execute("DELETE FROM push_subscriptions WHERE session_hash=?", (session_hash,))


def drop_all(appconn: sqlite3.Connection) -> None:
    appconn.execute("DELETE FROM push_subscriptions")


def _status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def send(settings: Settings, appconn: sqlite3.Connection, payload: dict) -> int:
    """Push `payload` to every subscription (blocking: call from a thread). Returns how many accepted."""
    if not settings.push_subject:
        return 0
    key_path, _ = vapid_keys(settings)
    with appdb.db_lock:
        subs = appconn.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions").fetchall()
    data = json.dumps(payload)
    accepted = 0
    for endpoint, p256dh, auth in subs:
        try:
            webpush(
                subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
                data=data,
                vapid_private_key=key_path,
                vapid_claims={"sub": settings.push_subject},
                ttl=3600,
                timeout=PUSH_TIMEOUT,
                headers={"Urgency": "high"},
            )
        except (WebPushException, OSError) as exc:  # requests errors are OSError subclasses
            status = _status(exc)
            with appdb.db_lock:
                if status in GONE:
                    drop_subscription(appconn, endpoint)
                else:
                    appconn.execute(
                        "UPDATE push_subscriptions SET failures = failures + 1 WHERE endpoint=?", (endpoint,)
                    )
                    appconn.execute(
                        "DELETE FROM push_subscriptions WHERE endpoint=? AND failures >= ?", (endpoint, MAX_FAILURES)
                    )
            logger.warning("push to a subscription failed: status %s", status)
            continue
        accepted += 1
        with appdb.db_lock:
            appconn.execute("UPDATE push_subscriptions SET failures=0 WHERE endpoint=?", (endpoint,))
    return accepted

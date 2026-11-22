"""Passphrase, server-side sessions, login rate limit, Origin check and the API middleware."""

import hashlib
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE = "wa_session"
SESSION_SECONDS = 30 * 24 * 3600
MIN_PASSPHRASE = 10
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60

_hasher = PasswordHasher()
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def passphrase_is_set(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM meta WHERE key='passphrase_hash'").fetchone() is not None


def set_passphrase(conn: sqlite3.Connection, passphrase: str) -> bool:
    """Store the first passphrase. Returns False (changing nothing) when one is already set."""
    if len(passphrase) < MIN_PASSPHRASE:
        raise ValueError(f"Passphrase must be at least {MIN_PASSPHRASE} characters")
    cursor = conn.execute(
        "INSERT INTO meta (key, value) VALUES ('passphrase_hash', ?) ON CONFLICT(key) DO NOTHING",
        (_hasher.hash(passphrase),),
    )
    return cursor.rowcount == 1


def check_passphrase(conn: sqlite3.Connection, passphrase: str) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key='passphrase_hash'").fetchone()
    if row is None:
        return False
    try:
        return _hasher.verify(row[0], passphrase)
    except (VerificationError, InvalidHashError):
        return False


def new_session(conn: sqlite3.Connection, ua: str | None) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    conn.execute(
        "INSERT INTO sessions (token_hash, created_at, expires_at, user_agent) VALUES (?, ?, ?, ?)",
        (_hash_token(token), now, now + SESSION_SECONDS, ua),
    )
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
    return token


def session_valid(conn: sqlite3.Connection, token: str | None) -> bool:
    if not token:
        return False
    row = conn.execute("SELECT expires_at FROM sessions WHERE token_hash=?", (_hash_token(token),)).fetchone()
    return row is not None and row[0] > time.time()


def end_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))


def login_blocked(conn: sqlite3.Connection, ip: str) -> bool:
    since = int(time.time()) - FAILURE_WINDOW
    (count,) = conn.execute("SELECT COUNT(*) FROM login_failures WHERE ip=? AND at>?", (ip, since)).fetchone()
    return count >= MAX_FAILURES


def record_failure(conn: sqlite3.Connection, ip: str) -> None:
    now = int(time.time())
    conn.execute("INSERT INTO login_failures (ip, at) VALUES (?, ?)", (ip, now))
    conn.execute("DELETE FROM login_failures WHERE at<=?", (now - FAILURE_WINDOW,))


def origin_ok(request: Request) -> bool:
    """State-changing requests must come from the page we serve: Origin host == Host."""
    origin = request.headers.get("origin")
    if origin is None:
        return True  # non-browser client; the SameSite=Strict cookie covers browsers
    return urlsplit(origin).netloc == request.headers.get("host")


_LOOPBACK = {"127.0.0.1", "::1"}


def client_ip(request: Request) -> str:
    """Peer address; behind a loopback reverse proxy (tailscale serve) the last X-Forwarded-For hop.

    The proxy appends the real client address, so only the last hop is trustworthy; earlier hops are
    client-supplied. The header is trusted only when the direct peer is loopback.
    """
    peer = request.client.host if request.client else "unknown"
    if peer in _LOOPBACK:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        if forwarded:
            return forwarded
    return peer


def _error(status: int, message: str) -> Response:
    return JSONResponse({"error": message}, status_code=status)


class AuthMiddleware:
    """Origin check on writes; 401 JSON on /api/* without a session (auth and health excepted)."""

    def __init__(self, app: ASGIApp, conn: sqlite3.Connection) -> None:
        self.app = app
        self.conn = conn

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path
        if request.method not in _SAFE_METHODS and not origin_ok(request):
            await _error(403, "Bad origin")(scope, receive, send)
            return
        open_path = path.startswith("/api/auth/") or path == "/api/health"
        if path.startswith("/api/") and not open_path and not session_valid(self.conn, request.cookies.get(COOKIE)):
            await _error(401, "Not logged in")(scope, receive, send)
            return
        await self.app(scope, receive, send)

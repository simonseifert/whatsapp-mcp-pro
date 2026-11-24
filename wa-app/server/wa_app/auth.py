"""Passphrase, server-side sessions, login rate limit, Origin check and the API middleware."""

import hmac
import secrets
import sqlite3
import time
from collections.abc import Mapping
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from . import live
from .tokens import token_hash

COOKIE_HOST = (
    "__Host-wa_session"  # Secure, Path=/, no Domain: a sibling service on the same hostname cannot set or shadow it
)
COOKIE_LEGACY = "wa_session"  # accepted for one release so existing logins survive; the next login sets the new name
COOKIE = COOKIE_HOST
COOKIES = (COOKIE_HOST, COOKIE_LEGACY)
SESSION_SECONDS = 30 * 24 * 3600
MIN_PASSPHRASE = 10
MAX_PASSPHRASE_BYTES = 1024  # argon2 hashes whatever it is given: bound the work an unauthenticated caller can ask for
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60

_hasher = PasswordHasher()
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


_hash_token = token_hash


def cookie_name(secure: bool) -> str:
    """The `__Host-` prefix needs Secure, so plain-http dev setups (WA_APP_INSECURE_COOKIES=1) keep the old name."""
    return COOKIE_HOST if secure else COOKIE_LEGACY


def session_token(cookies: Mapping[str, str]) -> str | None:
    """The session cookie value: the `__Host-` name first, the legacy name second."""
    return cookies.get(COOKIE_HOST) or cookies.get(COOKIE_LEGACY)


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


def session_hash_valid(conn: sqlite3.Connection, hashed: str) -> bool:
    row = conn.execute("SELECT expires_at FROM sessions WHERE token_hash=?", (hashed,)).fetchone()
    return row is not None and row[0] > time.time()


def end_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))
        live.drop_session(_hash_token(token))  # its open live streams end now


def end_all_sessions(conn: sqlite3.Connection) -> int:
    """Log out everywhere: every session row goes and every open live stream ends. Returns the sessions removed."""
    removed = conn.execute("DELETE FROM sessions").rowcount
    live.drop_all_sessions()
    return removed


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
    """Origin check on writes; 401 JSON on /api/* without a session (auth and health excepted).

    `/api/agent/*` is the one exception: it takes `Authorization: Bearer <WA_APP_AGENT_TOKEN>` instead of a session
    and skips the Origin check (a bearer token is not attached by browsers on their own, so CSRF does not apply).
    With no token configured those paths answer 404, as if the feature did not exist."""

    def __init__(self, app: ASGIApp, conn: sqlite3.Connection, agent_token: str | None = None) -> None:
        self.app = app
        self.conn = conn
        self.agent_token = agent_token

    def _agent_response(self, request: Request) -> Response | None:
        """None when the caller may proceed to an agent route."""
        if not self.agent_token:
            return _error(404, "Not found")
        scheme, _, given = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(given.encode(), self.agent_token.encode()):
            return _error(401, "Bad token")
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path
        if path.startswith("/api/agent/"):
            refused = self._agent_response(request)
            if refused is not None:
                await refused(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        if request.method not in _SAFE_METHODS and not origin_ok(request):
            await _error(403, "Bad origin")(scope, receive, send)
            return
        open_path = path.startswith("/api/auth/") or path == "/api/health"
        if path.startswith("/api/") and not open_path and not session_valid(self.conn, session_token(request.cookies)):
            await _error(401, "Not logged in")(scope, receive, send)
            return
        await self.app(scope, receive, send)

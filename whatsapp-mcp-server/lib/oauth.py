"""Opt-in OAuth 2.1 for the shared HTTP server (serve_http.py).

Remote connectors in claude.ai and ChatGPT cannot send a static
`Authorization: Bearer` header; they only speak the MCP authorization spec
(OAuth 2.1 + PKCE, RFC 9728 protected-resource metadata, RFC 8414
authorization-server metadata, RFC 7591 dynamic client registration). This
module turns the server into its own authorization server for exactly one
user, the operator.

The protocol is the mcp SDK's (`mcp.server.auth`): metadata, /authorize,
/token, /register and /revoke handlers plus PKCE and client-secret checks all
come from it. What lives here is the part the SDK leaves to the provider:

- storage: clients, hashed tokens and pending consents in a 0600 SQLite
  file, so a restart does not log connectors out and the CLI can approve a
  consent the server is holding; auth codes stay in memory because they live
  minutes and a restart mid-login only costs a retry.
- the "login": a consent page that asks for a passphrase (or the full static
  token) and lets the operator pick read-only or full access. That choice is
  the token's scope, and serve_http maps it onto the same full/readonly roles
  the static tokens use, so the read-only gate is one code path for both.
  `python -m lib.oauth approve <req> --scope read|full` is the same approval
  from a shell on the box, which the public endpoint cannot rate-limit away.

Off unless MCP_OAUTH_ENABLED=true. CLI: python -m lib.oauth list|revoke|approve.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import secrets
import sqlite3
import sys
import time
import uuid
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    ProviderTokenVerifier,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, ValidationError
from starlette.applications import Starlette
from starlette.authentication import AuthCredentials
from starlette.requests import HTTPConnection, Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

logger = logging.getLogger("whatsapp-mcp.oauth")

SCOPE_READ = "wa:read"
SCOPE_FULL = "wa:full"
ALL_SCOPES = [SCOPE_READ, SCOPE_FULL]
MCP_PATH = "/mcp"
CONSENT_PATH = "/oauth/consent"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

# One operator, so these are small on purpose: they bound what an anonymous
# caller on the public URL can make the server hold or store.
MAX_CLIENTS = 50
UNAPPROVED_CLIENT_TTL = 3600  # DCR is open to anyone; drop clients nobody consented to
PENDING_TTL = 600
CODE_TTL = 300
ATTEMPTS_PER_CONSENT = 3
BACKOFF_MAX = 900
DEFAULT_FAILURE_BUDGET = 20
DEFAULT_FAILURE_WINDOW = 3600
MAX_TRACKED_IPS = 10000
MIN_PASSPHRASE_LEN = 12
# DCR lets anyone name their client "Claude"; the redirect host is the only
# identity that means anything, so only known connector hosts get a consent page.
DEFAULT_REDIRECT_HOSTS = ("claude.ai", "claude.com", "chatgpt.com", "localhost", "127.0.0.1")
CONSENT_COOKIE_PREFIX = "wac_"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip().lower() for v in (value or "").split(",") if v.strip())


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_local_host(host: str | None) -> bool:
    """True for loopback hosts, the only ones allowed plain http."""
    return (host or "").lower() in LOCAL_HOSTS


def role_for_scopes(scopes: list[str]) -> str | None:
    """Map OAuth scopes onto serve_http's static-token roles."""
    if SCOPE_FULL in scopes:
        return "full"
    if SCOPE_READ in scopes:
        return "readonly"
    return None


# ───────────────────────────────────────────────────────────── configuration


def default_db_path() -> str:
    """oauth.db next to the bridge store dir (the data dir, not the repo)."""
    from lib.utils import MESSAGES_DB_PATH

    store_dir = os.path.dirname(os.path.abspath(MESSAGES_DB_PATH))
    return os.path.join(os.path.dirname(store_dir), "oauth.db")


@dataclass(frozen=True)
class OAuthConfig:
    """Validated OAuth settings. Build with `OAuthConfig.from_env`."""

    public_url: str  # issuer: scheme://host[:port], no path
    passphrase: str
    db_path: str
    access_ttl: int = 3600
    refresh_ttl: int = 30 * 86400
    allowed_redirect_hosts: tuple[str, ...] = DEFAULT_REDIRECT_HOSTS
    trusted_proxies: tuple[str, ...] = ()
    failure_budget: int = DEFAULT_FAILURE_BUDGET  # wrong passphrases per window, all sources
    failure_window: int = DEFAULT_FAILURE_WINDOW

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    def redirect_allowed(self, uri: Any) -> bool:
        """https (or loopback http) to a host on MCP_OAUTH_ALLOWED_REDIRECT_HOSTS."""
        parsed = urlparse(str(uri))
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" and not (parsed.scheme == "http" and is_local_host(host)):
            return False
        return host in self.allowed_redirect_hosts

    @property
    def resource_url(self) -> str:
        """The MCP endpoint's canonical URI, the RFC 8707 audience of every token."""
        return self.public_url + MCP_PATH

    @property
    def public_host(self) -> str:
        """Host header value clients send when reaching the public URL."""
        return urlparse(self.public_url).netloc

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OAuthConfig | None:
        """Read MCP_OAUTH_* from the environment; None when OAuth is off.

        Raises ValueError on a config that would be unsafe to serve, rather
        than starting with a half-working or open authorization server.
        """
        env = os.environ if env is None else env
        if not _truthy(env.get("MCP_OAUTH_ENABLED")):
            return None

        raw_url = (env.get("MCP_PUBLIC_URL") or "").strip().rstrip("/")
        if not raw_url:
            raise ValueError("MCP_OAUTH_ENABLED=true requires MCP_PUBLIC_URL (e.g. https://wa.example.ts.net)")
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"MCP_PUBLIC_URL is not an http(s) URL: {raw_url!r}")
        # Plain http would put bearer tokens and the passphrase on the wire.
        if parsed.scheme != "https" and not is_local_host(parsed.hostname):
            raise ValueError("MCP_PUBLIC_URL must be https unless the host is localhost/127.0.0.1")
        # The issuer and every endpoint hang off the origin; a path prefix would
        # need the proxy to rewrite paths, which the metadata cannot express.
        if parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("MCP_PUBLIC_URL must be an origin only (scheme://host[:port]), no path or query")

        # Reusing the full static token is allowed: it already grants everything
        # the consent page can grant, so it adds no new secret to protect.
        passphrase = env.get("MCP_OAUTH_PASSPHRASE") or env.get("WA_MCP_FULL_TOKEN") or ""
        if len(passphrase) < MIN_PASSPHRASE_LEN:
            raise ValueError(
                f"MCP_OAUTH_ENABLED=true needs MCP_OAUTH_PASSPHRASE (or WA_MCP_FULL_TOKEN) of at least "
                f"{MIN_PASSPHRASE_LEN} characters; it is the only thing between the internet and the consent page"
            )

        def _int(name: str, default: int) -> int:
            value = int(env.get(name) or default)
            if value <= 0:
                raise ValueError(f"{name} must be a positive number of seconds")
            return value

        return cls(
            public_url=f"{parsed.scheme}://{parsed.netloc.lower()}",
            passphrase=passphrase,
            db_path=env.get("MCP_OAUTH_DB") or default_db_path(),
            access_ttl=_int("MCP_OAUTH_ACCESS_TTL", 3600),
            refresh_ttl=_int("MCP_OAUTH_REFRESH_TTL", 30 * 86400),
            allowed_redirect_hosts=_csv(env.get("MCP_OAUTH_ALLOWED_REDIRECT_HOSTS")) or DEFAULT_REDIRECT_HOSTS,
            trusted_proxies=_csv(env.get("MCP_TRUSTED_PROXY")),
            failure_budget=_int("MCP_OAUTH_FAILURE_BUDGET", DEFAULT_FAILURE_BUDGET),
            failure_window=_int("MCP_OAUTH_FAILURE_WINDOW", DEFAULT_FAILURE_WINDOW),
        )


# ─────────────────────────────────────────────────────────────────── storage


class OAuthStore:
    """Clients and tokens in SQLite. Tokens are stored as SHA-256 hashes, so
    a copied database (e.g. by the standby sync) holds no usable bearer."""

    def __init__(self, path: str):
        self.path = path
        parent = Path(path).parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create 0600 before sqlite opens it; sqlite gives the rollback journal
        # the same mode as the database file.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    client_id   TEXT PRIMARY KEY,
                    info_json   TEXT NOT NULL,
                    created_at  INTEGER NOT NULL,
                    approved_at INTEGER,
                    last_used_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    token_hash TEXT PRIMARY KEY,
                    kind       TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
                    client_id  TEXT NOT NULL,
                    grant_id   TEXT NOT NULL,
                    scopes     TEXT NOT NULL,
                    resource   TEXT,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    rotated    INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS tokens_grant ON tokens(grant_id);
                CREATE INDEX IF NOT EXISTS tokens_client ON tokens(client_id);
                CREATE TABLE IF NOT EXISTS pending (
                    req_id      TEXT PRIMARY KEY,
                    client_id   TEXT NOT NULL,
                    client_name TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    offered     TEXT NOT NULL,
                    nonce_hash  TEXT NOT NULL,
                    created_at  REAL NOT NULL,
                    failures    INTEGER NOT NULL DEFAULT 0,
                    cli_scope   TEXT
                );
                """
            )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # clients

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._conn() as conn:
            row = conn.execute("SELECT info_json FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row["info_json"]) if row else None

    def add_client(self, info: OAuthClientInformationFull) -> None:
        now = int(time.time())
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM clients WHERE approved_at IS NULL AND created_at < ?", (now - UNAPPROVED_CLIENT_TTL,)
            )
            (count,) = conn.execute("SELECT COUNT(*) FROM clients").fetchone()
            if count >= MAX_CLIENTS:
                raise RegistrationError(
                    "invalid_client_metadata", "too many registered clients; revoke some with python -m lib.oauth"
                )
            conn.execute(
                "INSERT INTO clients (client_id, info_json, created_at) VALUES (?, ?, ?)",
                (info.client_id, info.model_dump_json(exclude_none=True), now),
            )

    def mark_approved(self, client_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE clients SET approved_at = COALESCE(approved_at, ?) WHERE client_id = ?",
                (int(time.time()), client_id),
            )

    def list_clients(self) -> list[dict[str, Any]]:
        now = int(time.time())
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM clients ORDER BY created_at").fetchall()
            out = []
            for row in rows:
                info = json.loads(row["info_json"])
                live = conn.execute(
                    "SELECT kind, scopes FROM tokens WHERE client_id = ? AND rotated = 0 AND expires_at > ?",
                    (row["client_id"], now),
                ).fetchall()
                scopes = sorted({s for t in live for s in t["scopes"].split()})
                out.append(
                    {
                        "client_id": row["client_id"],
                        "client_name": info.get("client_name"),
                        "redirect_uris": info.get("redirect_uris", []),
                        "created_at": row["created_at"],
                        "approved_at": row["approved_at"],
                        "last_used_at": row["last_used_at"],
                        "live_tokens": len(live),
                        "access": role_for_scopes(scopes),
                    }
                )
        return out

    def revoke_client(self, client_id: str) -> bool:
        """Delete a client and every token it holds. True if it existed."""
        with self._conn() as conn:
            conn.execute("DELETE FROM tokens WHERE client_id = ?", (client_id,))
            return conn.execute("DELETE FROM clients WHERE client_id = ?", (client_id,)).rowcount > 0

    # tokens

    def issue(self, client_id: str, grant_id: str, scopes: list[str], resource: str, cfg: OAuthConfig) -> OAuthToken:
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        rows = [
            (_hash(access), "access", client_id, grant_id, " ".join(scopes), resource, now + cfg.access_ttl, now),
            (_hash(refresh), "refresh", client_id, grant_id, " ".join(scopes), resource, now + cfg.refresh_ttl, now),
        ]
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO tokens (token_hash, kind, client_id, grant_id, scopes, resource, expires_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.execute("UPDATE clients SET last_used_at = ? WHERE client_id = ?", (now, client_id))
        return OAuthToken(
            access_token=access,
            expires_in=cfg.access_ttl,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    def find(self, token: str, kind: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM tokens WHERE token_hash = ? AND kind = ?", (_hash(token), kind)
            ).fetchone()

    def rotate(self, refresh_token: str, grant_id: str) -> None:
        """Retire a refresh token and the grant's current access tokens.

        The retired refresh row is kept (rotated=1) so a second use of it can be
        recognised as a replay rather than an unknown token.
        """
        with self._conn() as conn:
            conn.execute("UPDATE tokens SET rotated = 1 WHERE token_hash = ?", (_hash(refresh_token),))
            conn.execute("DELETE FROM tokens WHERE grant_id = ? AND kind = 'access'", (grant_id,))

    def revoke_grant(self, grant_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM tokens WHERE grant_id = ?", (grant_id,))

    def prune(self) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM tokens WHERE expires_at < ?", (int(time.time()),))
            conn.execute("DELETE FROM pending WHERE created_at < ?", (time.time() - PENDING_TTL,))

    # pending consents

    def add_pending(self, req_id: str, p: _Pending) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM pending WHERE created_at < ?", (time.time() - PENDING_TTL,))
            conn.execute(
                "INSERT INTO pending (req_id, client_id, client_name, params_json, offered, nonce_hash, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    req_id,
                    p.client_id,
                    p.client_name,
                    p.params.model_dump_json(),
                    " ".join(p.offered),
                    p.nonce_hash,
                    p.created,
                ),
            )

    def get_pending(self, req_id: str) -> _Pending | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM pending WHERE req_id = ?", (req_id,)).fetchone()
        if row is None or time.time() - row["created_at"] > PENDING_TTL:
            return None
        return _Pending(
            client_id=row["client_id"],
            client_name=row["client_name"],
            params=AuthorizationParams.model_validate_json(row["params_json"]),
            offered=row["offered"].split(),
            nonce_hash=row["nonce_hash"],
            created=row["created_at"],
            failures=row["failures"],
            cli_scope=row["cli_scope"],
        )

    def pending_failed(self, req_id: str) -> int:
        with self._conn() as conn:
            conn.execute("UPDATE pending SET failures = failures + 1 WHERE req_id = ?", (req_id,))
            row = conn.execute("SELECT failures FROM pending WHERE req_id = ?", (req_id,)).fetchone()
        return row["failures"] if row else ATTEMPTS_PER_CONSENT

    def approve_pending(self, req_id: str, scope: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE pending SET cli_scope = ? WHERE req_id = ?", (scope, req_id))

    def delete_pending(self, req_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM pending WHERE req_id = ?", (req_id,))


# ────────────────────────────────────────────────────────────────── provider


class WAAuthCode(AuthorizationCode):
    grant_id: str


class WARefreshToken(RefreshToken):
    grant_id: str


class WAAccessToken(AccessToken):
    grant_id: str


@dataclass
class _Pending:
    client_id: str
    client_name: str
    params: AuthorizationParams
    offered: list[str]
    nonce_hash: str
    created: float = field(default_factory=time.time)
    failures: int = 0
    cli_scope: str | None = None  # set by `python -m lib.oauth approve`

    @property
    def redirect_host(self) -> str:
        return urlparse(str(self.params.redirect_uri)).hostname or "?"


def backoff_key(ip: str) -> str:
    """Group addresses the way one attacker holds them: an IPv6 /64 (one
    subscriber gets at least that many) or a single IPv4 address."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{addr}/64", strict=False))
    return str(addr)


class IPBackoff:
    """Per-address exponential backoff on wrong passphrases (keys: backoff_key).

    Deliberately not a global lock: one would let anyone who can reach the
    public consent page keep the operator locked out indefinitely. Here an
    attacker only slows their own address (2 s, 4 s, 8 s ... capped at 15 min),
    and the CLI approval path bypasses it entirely. FailureBudget covers the
    distributed case this cannot.
    """

    def __init__(self, window: int = DEFAULT_FAILURE_WINDOW) -> None:
        self.window = window
        # key -> (failures, next allowed, last failure)
        self.state: dict[str, tuple[int, float, float]] = {}

    def retry_after(self, ip: str) -> int:
        _, until, _ = self.state.get(backoff_key(ip), (0, 0.0, 0.0))
        return max(0, int(until - time.time() + 0.999))

    def fail(self, ip: str) -> None:
        key = backoff_key(ip)
        now = time.time()
        count, _, last = self.state.get(key, (0, 0.0, 0.0))
        if now - last > self.window:
            count = 0
        count += 1
        self.state[key] = (count, now + min(2**count, BACKOFF_MAX), now)
        if len(self.state) > MAX_TRACKED_IPS:
            self.state = {k: v for k, v in self.state.items() if now - v[2] <= self.window}

    def succeed(self, ip: str) -> None:
        # A success does not wipe recent failures: an attacker sharing the
        # operator's address (or a /64) must not get a fresh doubling series
        # every time the operator logs in.
        key = backoff_key(ip)
        entry = self.state.get(key)
        if entry and time.time() - entry[2] > self.window:
            del self.state[key]


class FailureBudget:
    """Sliding-window cap on wrong passphrases across all sources.

    Per-address backoff does nothing against many addresses. Once the budget
    is spent, passphrase approval is off until the window slides; the CLI
    approval path is unaffected, so this cannot lock the operator out either.
    """

    def __init__(self, limit: int = DEFAULT_FAILURE_BUDGET, window: int = DEFAULT_FAILURE_WINDOW) -> None:
        self.limit = limit
        self.window = window
        self.failures: deque[float] = deque()
        self.tripped = False

    def _slide(self) -> None:
        now = time.time()
        while self.failures and now - self.failures[0] > self.window:
            self.failures.popleft()
        if self.tripped and len(self.failures) < self.limit:
            self.tripped = False
            logger.warning("OAuth consent: passphrase approval re-enabled")

    def exhausted(self) -> bool:
        self._slide()
        return len(self.failures) >= self.limit

    def fail(self) -> None:
        self._slide()
        self.failures.append(time.time())
        if len(self.failures) >= self.limit and not self.tripped:
            self.tripped = True
            logger.warning(
                "OAuth consent: %d wrong passphrases within %d s from all sources; passphrase approval is "
                "disabled until the window slides. Approve with: python -m lib.oauth approve <req> --scope read",
                len(self.failures),
                self.window,
            )


def client_ip(scope: Mapping[str, Any], trusted_proxies: tuple[str, ...]) -> str:
    """The request's client address: the socket peer, or, when that peer is a
    trusted proxy (MCP_TRUSTED_PROXY), the nearest untrusted X-Forwarded-For hop.

    X-Forwarded-For is client-spoofable, so it is only read from proxies the
    operator named; otherwise anyone could rotate it to dodge the backoff.
    """
    peer = str((scope.get("client") or ("unknown", 0))[0])
    if peer.lower() not in trusted_proxies:
        return peer
    xff = ",".join(v.decode() for k, v in scope.get("headers", []) if k.lower() == b"x-forwarded-for")
    for hop in reversed([h.strip() for h in xff.split(",") if h.strip()]):
        if hop.lower() not in trusted_proxies:
            return hop
    return peer


class WAOAuthProvider:
    """`OAuthAuthorizationServerProvider` for a single-operator server."""

    def __init__(self, config: OAuthConfig, store: OAuthStore | None = None):
        self.config = config
        self.store = store or OAuthStore(config.db_path)
        self.codes: dict[str, WAAuthCode] = {}
        self.used_codes: dict[str, tuple[str, float]] = {}  # code -> (grant_id, expires)
        # req_id -> raw nonce, handed from authorize() to the /authorize
        # response wrapper that sets the browser-binding cookie.
        self.fresh_nonces: dict[str, str] = {}
        self.backoff = IPBackoff(config.failure_window)
        self.budget = FailureBudget(config.failure_budget, config.failure_window)

    # registration

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            # https (or loopback http) to an allowlisted host. Anything else
            # either hands the code to whoever controls that target or lets a
            # self-registered client pose as a known one on the consent page.
            if not self.config.redirect_allowed(uri):
                raise RegistrationError(
                    "invalid_redirect_uri",
                    f"redirect URI {uri} is not https/localhost on an allowed host "
                    f"({', '.join(self.config.allowed_redirect_hosts)}); see MCP_OAUTH_ALLOWED_REDIRECT_HOSTS",
                )
        self.store.add_client(client_info)
        logger.info("OAuth client registered: %s (%s)", client_info.client_id, client_info.client_name)

    # authorization

    def _check_resource(self, resource: str | None) -> None:
        # RFC 8707: tokens are only ever minted for this server's MCP endpoint.
        # An absent indicator (older clients) is bound to it anyway.
        if resource is None:
            return
        try:
            given = str(AnyHttpUrl(resource)).rstrip("/")
        except ValidationError:
            given = resource
        if given != str(AnyHttpUrl(self.config.resource_url)).rstrip("/"):
            raise AuthorizeError("invalid_target", f"unknown resource {resource!r}; use {self.config.resource_url}")

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        # Re-checked here, not only at registration: the operator may have
        # narrowed the allowlist since. Not an AuthorizeError, because the SDK
        # would redirect that error to the very host being refused.
        if not self.config.redirect_allowed(params.redirect_uri):
            return f"{self.config.public_url}{CONSENT_PATH}?refused=redirect_host"
        self._check_resource(params.resource)
        registered = (client.scope or "").split()
        offered = [s for s in ALL_SCOPES if s in registered]
        if not offered:
            raise AuthorizeError("invalid_scope", f"client is registered for none of {ALL_SCOPES}")
        self._sweep()
        req_id = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(32)
        self.store.add_pending(
            req_id,
            _Pending(
                client_id=client.client_id,
                client_name=client.client_name or client.client_id,
                params=params,
                offered=offered,
                nonce_hash=_hash(nonce),
            ),
        )
        self.fresh_nonces[req_id] = nonce
        return f"{self.config.public_url}{CONSENT_PATH}?req={req_id}"

    def _sweep(self) -> None:
        now = time.time()
        for code, c in list(self.codes.items()):
            if c.expires_at < now:
                del self.codes[code]
        for code, (_, exp) in list(self.used_codes.items()):
            if exp < now:
                del self.used_codes[code]

    def issue_code(self, pending: _Pending, scopes: list[str]) -> str:
        code = secrets.token_urlsafe(32)
        self.codes[code] = WAAuthCode(
            code=code,
            scopes=scopes,
            expires_at=time.time() + CODE_TTL,
            client_id=pending.client_id,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=self.config.resource_url,
            subject="operator",
            grant_id=uuid.uuid4().hex,
        )
        self.store.mark_approved(pending.client_id)
        return code

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> WAAuthCode | None:
        self._sweep()
        if authorization_code in self.used_codes:
            # OAuth 2.1 §4.1.2: a replayed code means it leaked; revoke what it bought.
            grant_id, _ = self.used_codes.pop(authorization_code)
            self.store.revoke_grant(grant_id)
            logger.warning("OAuth: authorization code replayed; revoked its tokens (client %s)", client.client_id)
            return None
        code = self.codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: WAAuthCode
    ) -> OAuthToken:
        if self.codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "authorization code already used")
        self.used_codes[authorization_code.code] = (authorization_code.grant_id, time.time() + CODE_TTL)
        if self.store.get_client(client.client_id) is None:
            raise TokenError("invalid_client", "client was revoked")
        return self.store.issue(
            client.client_id,
            authorization_code.grant_id,
            authorization_code.scopes,
            self.config.resource_url,
            self.config,
        )

    # tokens

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> WARefreshToken | None:
        row = self.store.find(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        if row["rotated"]:
            # Reuse of a rotated refresh token: either the client or a thief
            # holds a stale copy. Kill the whole grant; the operator re-consents.
            self.store.revoke_grant(row["grant_id"])
            logger.warning("OAuth: rotated refresh token reused; revoked grant for client %s", client.client_id)
            return None
        if row["expires_at"] < time.time():
            return None
        return WARefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scopes"].split(),
            expires_at=row["expires_at"],
            resource=row["resource"],
            subject="operator",
            grant_id=row["grant_id"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: WARefreshToken, scopes: list[str]
    ) -> OAuthToken:
        self.store.rotate(refresh_token.token, refresh_token.grant_id)
        return self.store.issue(client.client_id, refresh_token.grant_id, scopes, self.config.resource_url, self.config)

    async def load_access_token(self, token: str) -> WAAccessToken | None:
        row = self.store.find(token, "access")
        if row is None or row["expires_at"] < time.time():
            return None
        return WAAccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=row["scopes"].split(),
            expires_at=row["expires_at"],
            resource=row["resource"],
            subject="operator",
            claims={"iss": self.config.public_url},
            grant_id=row["grant_id"],
        )

    async def revoke_token(self, token: WAAccessToken | WARefreshToken) -> None:
        self.store.revoke_grant(token.grant_id)

    async def exchange_identity_assertion(self, client: Any, params: Any) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "not supported")

    def check_passphrase(self, given: str) -> bool:
        return hmac.compare_digest(given.encode(), self.config.passphrase.encode())


# ─────────────────────────────────────────────────────────────── consent page

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    # No form-action: browsers apply it to the redirect after the POST too,
    # which would block the hop back to the client's redirect_uri.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize WhatsApp MCP</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:3rem auto;padding:0 1rem;color:#111}}
.box{{border:1px solid #ccc;border-radius:8px;padding:1rem 1.25rem}}
.host{{font-size:1.6rem;font-weight:700;margin:.25rem 0}} .muted{{color:#555;font-size:.9rem}}
code{{word-break:break-all}} .err{{color:#b00020}} label{{display:block;margin:.4rem 0}}
input[type=password]{{width:100%;padding:.5rem;font-size:1rem;box-sizing:border-box}}
button{{padding:.5rem 1rem;font-size:1rem;margin:.25rem .5rem .25rem 0}}
</style></head><body>
<h1>Authorize access to WhatsApp</h1>
<div class="box">
<p>Access will be handed to:</p>
<p class="host">{host}</p>
<p class="muted">Full redirect: <code>{redirect}</code><br>
Name: {client} <em>(self-declared, unverified)</em></p>
<p class="muted">Only approve if you just started this sign-in on that site.</p>
</div>
{error}
<form method="post" action="{action}">
<input type="hidden" name="req" value="{req}">
<h2>Access</h2>
{choices}
<h2>Passphrase</h2>
<input type="password" name="passphrase" autocomplete="current-password" autofocus>
<p><button type="submit" name="decision" value="approve">Approve</button>
<button type="submit" name="decision" value="deny">Deny</button></p>
<h2>Or approve from the server</h2>
<p class="muted">Rate-limited, or you would rather not type the passphrase here? On the server run
<code>python -m lib.oauth approve {req} --scope read</code> (or <code>--scope full</code>), then:</p>
<p><button type="submit" name="decision" value="continue">Continue after server approval</button></p>
</form></body></html>"""

_CHOICES = {
    SCOPE_READ: "Read-only: search and read chats, contacts and media. Cannot send or change anything.",
    SCOPE_FULL: "Full: everything above plus sending messages and files, reacting, editing, deleting.",
}


def _render(req_id: str, pending: _Pending, error: str = "", status: int = 200) -> HTMLResponse:
    choices = "".join(
        f'<label><input type="radio" name="scope" value="{s}"{" checked" if s == pending.offered[0] else ""}> '
        f"{html.escape(_CHOICES[s])}</label>"
        for s in pending.offered
    )
    body = _PAGE.format(
        host=html.escape(pending.redirect_host),
        client=html.escape(pending.client_name),
        redirect=html.escape(str(pending.params.redirect_uri)),
        error=f'<p class="err">{html.escape(error)}</p>' if error else "",
        action=CONSENT_PATH,
        req=html.escape(req_id),
        choices=choices,
    )
    return HTMLResponse(body, status_code=status, headers=_SECURITY_HEADERS)


def _plain(text: str, status: int, headers: Mapping[str, str] | None = None) -> Response:
    return Response(text, status_code=status, media_type="text/plain", headers={**_SECURITY_HEADERS, **(headers or {})})


_EXPIRED = "This authorization request expired or was already used. Start again from the client."
_UNBOUND = (
    "This consent link was not opened in the browser that started the sign-in, so it cannot be approved here. "
    "If you did not start a sign-in just now, ignore it. Otherwise start again from the client."
)


_BUDGET_MSG = (
    "Passphrase approval is temporarily disabled: too many wrong passphrases from all sources. "
    "Approve from the server with the command below, then press Continue."
)


def _bound(request: Request, req_id: str, pending: _Pending) -> bool:
    """True if this browser holds the nonce cookie set by its own /authorize."""
    nonce = request.cookies.get(CONSENT_COOKIE_PREFIX + req_id, "")
    return bool(nonce) and hmac.compare_digest(_hash(nonce), pending.nonce_hash)


def consent_routes(provider: WAOAuthProvider) -> list[Route]:
    """GET renders the consent form, POST approves or denies it."""
    store = provider.store

    def _finish(req_id: str, pending: _Pending, choice: str, how: str) -> Response:
        store.delete_pending(req_id)
        # Full includes read so a client can later refresh down to read-only.
        scopes = [SCOPE_READ, SCOPE_FULL] if choice == SCOPE_FULL else [SCOPE_READ]
        code = provider.issue_code(pending, scopes)
        logger.info(
            "OAuth consent granted (%s): client %s -> %s, scope %s",
            how,
            pending.client_id,
            pending.redirect_host,
            " ".join(scopes),
        )
        resp = RedirectResponse(
            construct_redirect_uri(str(pending.params.redirect_uri), code=code, state=pending.params.state),
            status_code=303,
        )
        resp.delete_cookie(CONSENT_COOKIE_PREFIX + req_id, path=CONSENT_PATH)
        return resp

    async def consent(request: Request) -> Response:
        provider._sweep()
        if request.method == "GET":
            if request.query_params.get("refused") == "redirect_host":
                return _plain(
                    "This client's redirect host is not on MCP_OAUTH_ALLOWED_REDIRECT_HOSTS. "
                    "Add it on the server if you trust it.",
                    400,
                )
            req_id = request.query_params.get("req", "")
            pending = store.get_pending(req_id)
            if pending is None:
                return _plain(_EXPIRED, 400)
            if not _bound(request, req_id, pending):
                return _plain(_UNBOUND, 403)
            return _render(req_id, pending, _BUDGET_MSG if provider.budget.exhausted() else "")

        form = await request.form()
        req_id = str(form.get("req", ""))
        pending = store.get_pending(req_id)
        if pending is None:
            return _plain(_EXPIRED, 400)
        if not _bound(request, req_id, pending):
            logger.warning("OAuth consent POST without the binding cookie (client %s)", pending.client_id)
            return _plain(_UNBOUND, 403)

        decision = form.get("decision")
        if decision == "deny":
            store.delete_pending(req_id)
            return RedirectResponse(
                construct_redirect_uri(
                    str(pending.params.redirect_uri), error="access_denied", state=pending.params.state
                ),
                status_code=303,
            )
        if decision == "continue":
            if pending.cli_scope is None:
                return _render(req_id, pending, "Not approved on the server yet.", status=409)
            return _finish(req_id, pending, pending.cli_scope, "cli")
        if decision != "approve":
            return _render(req_id, pending, "Choose Approve or Deny.", status=400)

        if provider.budget.exhausted():
            return _render(req_id, pending, _BUDGET_MSG, status=429)
        ip = client_ip(request.scope, provider.config.trusted_proxies)
        wait = provider.backoff.retry_after(ip)
        if wait:
            return _plain(
                f"Too many wrong passphrases from your address. Try again in {wait} s, "
                f"or approve from the server: python -m lib.oauth approve {req_id} --scope read",
                429,
                {"Retry-After": str(wait)},
            )
        if not provider.check_passphrase(str(form.get("passphrase", ""))):
            provider.backoff.fail(ip)
            provider.budget.fail()
            failures = store.pending_failed(req_id)
            logger.warning("OAuth consent: wrong passphrase from %s for client %s", ip, pending.client_id)
            if failures >= ATTEMPTS_PER_CONSENT:
                store.delete_pending(req_id)
                return _plain("Too many wrong passphrases for this request. Start again from the client.", 403)
            return _render(req_id, pending, "Wrong passphrase.", status=401)
        provider.backoff.succeed(ip)

        choice = str(form.get("scope", ""))
        if choice not in pending.offered:
            return _render(req_id, pending, "Pick an access level.", status=400)
        return _finish(req_id, pending, choice, "passphrase")

    return [Route(CONSENT_PATH, consent, methods=["GET", "POST"])]


class _BindConsentCookie:
    """Wrap the SDK's /authorize endpoint to set the consent-binding cookie.

    The provider cannot touch the response (it only returns a URL), so this
    adds a Set-Cookie to the redirect it produced. A consent link forwarded to
    the operator from elsewhere then arrives without the cookie and is refused.
    """

    def __init__(self, app: Any, provider: WAOAuthProvider):
        self.app = app
        self.provider = provider

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        prefix = self.provider.config.public_url + CONSENT_PATH + "?req="
        secure = "; Secure" if self.provider.config.secure_cookies else ""

        async def wrapped(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                location = next((v.decode() for k, v in headers if k.lower() == b"location"), "")
                if location.startswith(prefix):
                    req_id = location[len(prefix) :]
                    nonce = self.provider.fresh_nonces.pop(req_id, None)
                    if nonce:
                        cookie = (
                            f"{CONSENT_COOKIE_PREFIX}{req_id}={nonce}; Path={CONSENT_PATH}; "
                            f"Max-Age={PENDING_TTL}; HttpOnly; SameSite=Lax{secure}"
                        )
                        headers.append((b"set-cookie", cookie.encode()))
                        message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, wrapped)


# ──────────────────────────────────────────────────────────── app assembly


def auth_settings(config: OAuthConfig) -> AuthSettings:
    """SDK settings for this server acting as both AS and RS."""
    # Strings, not AnyHttpUrl(...): AuthSettings preserves an empty path only
    # when it parses the URL itself, and RFC 8414 compares issuers exactly, so
    # a stray trailing slash breaks discovery in strict clients.
    return AuthSettings.model_validate(
        {
            "issuer_url": config.public_url,
            "resource_server_url": config.resource_url,
            "validate_token_resource": True,
            "client_registration_options": ClientRegistrationOptions(
                enabled=True, valid_scopes=ALL_SCOPES, default_scopes=ALL_SCOPES
            ),
            "revocation_options": RevocationOptions(enabled=True),
        }
    )


def build_oauth_app(provider: WAOAuthProvider) -> Starlette:
    """Starlette app serving the metadata, OAuth endpoints and the consent page."""
    settings = auth_settings(provider.config)
    assert settings.resource_server_url is not None
    routes = [
        Route(r.path, _BindConsentCookie(r.endpoint, provider), methods=r.methods) if r.path == "/authorize" else r
        for r in create_auth_routes(
            provider=provider,
            issuer_url=settings.issuer_url,
            client_registration_options=settings.client_registration_options,
            revocation_options=settings.revocation_options,
        )
    ]
    prm = create_protected_resource_routes(
        resource_url=settings.resource_server_url,
        authorization_servers=[settings.issuer_url],
        scopes_supported=ALL_SCOPES,
        resource_name="WhatsApp MCP",
    )
    # RFC 9728 puts the document at /.well-known/oauth-protected-resource/mcp;
    # some clients probe the bare path first, so serve it there too.
    prm_root = [Route("/.well-known/oauth-protected-resource", prm[0].endpoint, methods=["GET", "OPTIONS"])]
    return Starlette(routes=[*routes, *prm, *prm_root, *consent_routes(provider)])


class OAuthGuard:
    """Resource-server side: turn a bearer into a static-token role.

    Validation is the SDK's BearerAuthBackend (lookup, expiry, RFC 8707
    audience); serve_http's ScopedAuth then applies the same full/readonly
    rules it applies to static tokens.
    """

    def __init__(self, provider: WAOAuthProvider):
        settings = auth_settings(provider.config)
        self.provider = provider
        self.backend = BearerAuthBackend(
            ProviderTokenVerifier(provider),  # type: ignore[arg-type]
            resource_server_url=settings.resource_server_url,
        )
        assert settings.resource_server_url is not None
        self.metadata_url = str(build_resource_metadata_url(settings.resource_server_url))

    async def role(self, scope: dict[str, Any]) -> str | None:
        result = await self.backend.authenticate(HTTPConnection(scope))
        if result is None:
            return None
        creds, _user = result
        assert isinstance(creds, AuthCredentials)
        return role_for_scopes(list(creds.scopes))

    def challenge(self) -> str:
        """WWW-Authenticate value pointing clients at the RFC 9728 document."""
        return f'Bearer error="invalid_token", resource_metadata="{self.metadata_url}", scope="{SCOPE_READ}"'

    @property
    def paths(self) -> set[str]:
        return {
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource" + MCP_PATH,
            "/authorize",
            "/token",
            "/register",
            "/revoke",
            CONSENT_PATH,
        }


# ───────────────────────────────────────────────────────────────────── CLI


def _fmt_time(ts: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def main(argv: list[str] | None = None) -> int:
    """`python -m lib.oauth list|revoke|approve` against the token store."""
    parser = argparse.ArgumentParser(prog="python -m lib.oauth", description="Manage OAuth clients of serve_http.")
    parser.add_argument("--db", help="token store (default: MCP_OAUTH_DB or oauth.db next to the store dir)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list registered clients")
    revoke = sub.add_parser("revoke", help="delete a client and all of its tokens")
    revoke.add_argument("client_id")
    approve = sub.add_parser("approve", help="approve a pending consent (the req shown on the consent page)")
    approve.add_argument("req_id")
    approve.add_argument("--scope", choices=["read", "full"], required=True)
    args = parser.parse_args(argv)

    path = args.db or os.environ.get("MCP_OAUTH_DB") or default_db_path()
    if not os.path.exists(path):
        sys.stderr.write(f"no OAuth store at {path}\n")
        return 1
    store = OAuthStore(path)
    if args.cmd == "list":
        clients = store.list_clients()
        if not clients:
            sys.stdout.write("no clients\n")
        for c in clients:
            sys.stdout.write(
                f"{c['client_id']}  {c['client_name'] or '(unnamed)'}\n"
                f"    access: {c['access'] or 'none'}  live tokens: {c['live_tokens']}  "
                f"registered: {_fmt_time(c['created_at'])}  approved: {_fmt_time(c['approved_at'])}  "
                f"last token: {_fmt_time(c['last_used_at'])}\n"
                f"    redirect: {', '.join(c['redirect_uris'])}\n"
            )
        return 0
    if args.cmd == "approve":
        pending = store.get_pending(args.req_id)
        if pending is None:
            sys.stderr.write(f"no pending consent {args.req_id} (expired after {PENDING_TTL // 60} min?)\n")
            return 1
        scope = SCOPE_FULL if args.scope == "full" else SCOPE_READ
        if scope not in pending.offered:
            sys.stderr.write(f"this client may only be granted: {' '.join(pending.offered)}\n")
            return 1
        store.approve_pending(args.req_id, scope)
        sys.stdout.write(
            f"approved {args.scope} access for {pending.redirect_host} "
            f"(name: {pending.client_name}, self-declared). Press 'Continue' in the browser.\n"
        )
        return 0
    if store.revoke_client(args.client_id):
        sys.stdout.write(f"revoked {args.client_id}\n")
        return 0
    sys.stderr.write(f"no such client: {args.client_id}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

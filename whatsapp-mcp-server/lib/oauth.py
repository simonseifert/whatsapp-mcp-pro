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

- storage: clients and hashed tokens in a 0600 SQLite file, so a restart does
  not log connectors out; pending consents and auth codes stay in memory
  because they live minutes and a restart mid-login only costs a retry.
- the "login": a consent page that asks for a passphrase (or the full static
  token) and lets the operator pick read-only or full access. That choice is
  the token's scope, and serve_http maps it onto the same full/readonly roles
  the static tokens use, so the read-only gate is one code path for both.

Off unless MCP_OAUTH_ENABLED=true. CLI: python -m lib.oauth list|revoke <id>.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
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
LOCKOUT_FAILURES = 5
LOCKOUT_WINDOW = 900
MIN_PASSPHRASE_LEN = 12


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


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
    created: float = field(default_factory=time.time)
    failures: int = 0


class WAOAuthProvider:
    """`OAuthAuthorizationServerProvider` for a single-operator server."""

    def __init__(self, config: OAuthConfig, store: OAuthStore | None = None):
        self.config = config
        self.store = store or OAuthStore(config.db_path)
        self.pending: dict[str, _Pending] = {}
        self.codes: dict[str, WAAuthCode] = {}
        self.used_codes: dict[str, tuple[str, float]] = {}  # code -> (grant_id, expires)
        self.failures: deque[float] = deque()

    # registration

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            # https, or a loopback redirect for native/CLI clients. Anything
            # else (http to a LAN host, custom schemes) would hand the code to
            # whoever controls that target.
            if uri.scheme != "https" and not (uri.scheme == "http" and is_local_host(uri.host)):
                raise RegistrationError("invalid_redirect_uri", f"redirect URI must be https or localhost: {uri}")
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
        self._check_resource(params.resource)
        registered = (client.scope or "").split()
        offered = [s for s in ALL_SCOPES if s in registered]
        if not offered:
            raise AuthorizeError("invalid_scope", f"client is registered for none of {ALL_SCOPES}")
        self._sweep()
        req_id = secrets.token_urlsafe(24)
        self.pending[req_id] = _Pending(
            client_id=client.client_id,
            client_name=client.client_name or client.client_id,
            params=params,
            offered=offered,
        )
        return f"{self.config.public_url}{CONSENT_PATH}?req={req_id}"

    def _sweep(self) -> None:
        now = time.time()
        for req_id, p in list(self.pending.items()):
            if now - p.created > PENDING_TTL:
                del self.pending[req_id]
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

    # consent rate limiting

    def locked_out(self) -> bool:
        now = time.time()
        while self.failures and now - self.failures[0] > LOCKOUT_WINDOW:
            self.failures.popleft()
        return len(self.failures) >= LOCKOUT_FAILURES

    def check_passphrase(self, given: str) -> bool:
        ok = hmac.compare_digest(given.encode(), self.config.passphrase.encode())
        if not ok:
            self.failures.append(time.time())
        return ok


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
body{{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;color:#111}}
.box{{border:1px solid #ccc;border-radius:8px;padding:1rem 1.25rem}}
code{{word-break:break-all}} .err{{color:#b00020}} label{{display:block;margin:.4rem 0}}
input[type=password]{{width:100%;padding:.5rem;font-size:1rem;box-sizing:border-box}}
button{{padding:.5rem 1rem;font-size:1rem;margin-right:.5rem}}
</style></head><body>
<h1>Authorize access to WhatsApp</h1>
<div class="box">
<p><strong>{client}</strong> wants to use this WhatsApp MCP server.</p>
<p>After approval it will be sent back to <code>{redirect}</code>.</p>
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
        client=html.escape(pending.client_name),
        redirect=html.escape(str(pending.params.redirect_uri)),
        error=f'<p class="err">{html.escape(error)}</p>' if error else "",
        action=CONSENT_PATH,
        req=html.escape(req_id),
        choices=choices,
    )
    return HTMLResponse(body, status_code=status, headers=_SECURITY_HEADERS)


def _plain(text: str, status: int) -> Response:
    return Response(text, status_code=status, media_type="text/plain", headers=_SECURITY_HEADERS)


def consent_routes(provider: WAOAuthProvider) -> list[Route]:
    """GET renders the consent form, POST approves or denies it."""

    async def consent(request: Request) -> Response:
        provider._sweep()
        if request.method == "GET":
            req_id = request.query_params.get("req", "")
            pending = provider.pending.get(req_id)
            if pending is None:
                return _plain("This authorization request expired. Start again from the client.", 400)
            return _render(req_id, pending)

        form = await request.form()
        req_id = str(form.get("req", ""))
        pending = provider.pending.get(req_id)
        if pending is None:
            return _plain("This authorization request expired. Start again from the client.", 400)
        state = pending.params.state
        redirect = str(pending.params.redirect_uri)

        if form.get("decision") != "approve":
            del provider.pending[req_id]
            return RedirectResponse(
                construct_redirect_uri(redirect, error="access_denied", state=state), status_code=303
            )

        if provider.locked_out():
            logger.warning("OAuth consent locked out after repeated wrong passphrases")
            return _plain("Too many wrong passphrases. Try again in 15 minutes.", 429)
        if not provider.check_passphrase(str(form.get("passphrase", ""))):
            pending.failures += 1
            logger.warning("OAuth consent: wrong passphrase for client %s", pending.client_id)
            if pending.failures >= ATTEMPTS_PER_CONSENT:
                del provider.pending[req_id]
                return _plain("Too many wrong passphrases for this request. Start again from the client.", 403)
            return _render(req_id, pending, "Wrong passphrase.", status=401)

        choice = str(form.get("scope", ""))
        if choice not in pending.offered:
            return _render(req_id, pending, "Pick an access level.", status=400)
        del provider.pending[req_id]
        # Full includes read so a client can later refresh down to read-only.
        scopes = [SCOPE_READ, SCOPE_FULL] if choice == SCOPE_FULL else [SCOPE_READ]
        code = provider.issue_code(pending, scopes)
        logger.info("OAuth consent granted: client %s, scope %s", pending.client_id, " ".join(scopes))
        return RedirectResponse(construct_redirect_uri(redirect, code=code, state=state), status_code=303)

    return [Route(CONSENT_PATH, consent, methods=["GET", "POST"])]


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
    routes = create_auth_routes(
        provider=provider,
        issuer_url=settings.issuer_url,
        client_registration_options=settings.client_registration_options,
        revocation_options=settings.revocation_options,
    )
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
    """`python -m lib.oauth list|revoke <client_id>` against the token store."""
    parser = argparse.ArgumentParser(prog="python -m lib.oauth", description="Manage OAuth clients of serve_http.")
    parser.add_argument("--db", help="token store (default: MCP_OAUTH_DB or oauth.db next to the store dir)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list registered clients")
    revoke = sub.add_parser("revoke", help="delete a client and all of its tokens")
    revoke.add_argument("client_id")
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
    if store.revoke_client(args.client_id):
        sys.stdout.write(f"revoked {args.client_id}\n")
        return 0
    sys.stderr.write(f"no such client: {args.client_id}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

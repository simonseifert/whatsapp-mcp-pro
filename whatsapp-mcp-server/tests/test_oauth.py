"""OAuth 2.1 for serve_http, end to end against the in-process ASGI app.

A two-tool server stands in for main.mcp: the gate under test is generic over
readOnlyHint, and the real tools would need a bridge and a store.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import stat
import sys
import time
from collections import deque
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import AnyUrl
from starlette.testclient import TestClient

import serve_http
from lib import oauth
from lib.oauth import CONSENT_PATH, OAuthConfig, OAuthStore, main

PUBLIC = "https://wa.example.com"
PASSPHRASE = "correct horse battery staple"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
FULL_STATIC = "static-full-token-0123456789"
RO_STATIC = "static-readonly-token-0123456789"


def _server() -> MCPServer:
    srv = MCPServer("oauth-test")

    @srv.tool(annotations=ToolAnnotations(read_only_hint=True))
    def read_thing() -> str:
        """A read-only tool."""
        return "read ok"

    @srv.tool(annotations=ToolAnnotations(read_only_hint=False))
    def send_thing() -> str:
        """A write tool."""
        return "sent"

    return srv


@pytest.fixture
def cfg(tmp_path):
    return OAuthConfig(public_url=PUBLIC, passphrase=PASSPHRASE, db_path=str(tmp_path / "data" / "oauth.db"))


def _client(cfg, tokens=None, base_url=PUBLIC):
    app = serve_http.build_app(_server(), host="127.0.0.1", port=8082, tokens=tokens or {}, oauth_config=cfg)
    return TestClient(app, base_url=base_url, follow_redirects=False)


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _register(c, redirect=REDIRECT, **extra):
    body = {"redirect_uris": [redirect], "client_name": "Claude", "token_endpoint_auth_method": "none", **extra}
    return c.post("/register", json=body)


def _authorize(c, client_id, challenge, **extra):
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "st4te",
        "resource": PUBLIC + "/mcp",
        **extra,
    }
    return c.get("/authorize", params=params)


def _authorize_h(c, client_id, challenge, headers):
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "s",
        "resource": PUBLIC + "/mcp",
    }
    return c.get("/authorize", params=params, headers=headers)


def _consent(c, location, scope="wa:read", passphrase=PASSPHRASE, decision="approve", headers=None):
    req = parse_qs(urlparse(location).query)["req"][0]
    return c.post(
        CONSENT_PATH,
        data={"req": req, "scope": scope, "passphrase": passphrase, "decision": decision},
        headers=headers,
    )


def _login(c, scope="wa:read"):
    """Run DCR + authorize + consent + token exchange; return (client_id, token json)."""
    client_id = _register(c).json()["client_id"]
    verifier, challenge = _pkce()
    r = _authorize(c, client_id, challenge)
    assert r.status_code == 302, r.text
    r = _consent(c, r.headers["location"], scope=scope)
    assert r.status_code == 303, r.text
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r = c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": PUBLIC + "/mcp",
        },
    )
    assert r.status_code == 200, r.text
    return client_id, r.json()


def _rpc_result(resp):
    """JSON-RPC payload from a plain-JSON or SSE response."""
    text = resp.text
    if text.lstrip().startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"no JSON-RPC payload in {text!r}")


def _mcp_call(c, token, tool):
    """initialize + tools/call over streamable HTTP; returns the tools/call payload."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    init = c.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
    )
    assert init.status_code == 200, init.text
    sid = init.headers.get("mcp-session-id")
    if sid:
        headers["mcp-session-id"] = sid
    c.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    r = c.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": {}}},
    )
    assert r.status_code == 200, r.text
    return _rpc_result(r)


# ───────────────────────────────────────────────────────────── config


def test_disabled_by_default():
    assert OAuthConfig.from_env({}) is None
    assert OAuthConfig.from_env({"MCP_OAUTH_ENABLED": "false", "MCP_PUBLIC_URL": PUBLIC}) is None


@pytest.mark.parametrize(
    "url",
    ["", "http://wa.example.com", "https://wa.example.com/mcp", "ftp://wa.example.com", "https://wa.example.com?x=1"],
)
def test_config_rejects_bad_public_url(url, tmp_path):
    env = {"MCP_OAUTH_ENABLED": "true", "MCP_PUBLIC_URL": url, "MCP_OAUTH_PASSPHRASE": PASSPHRASE}
    with pytest.raises(ValueError):
        OAuthConfig.from_env({**env, "MCP_OAUTH_DB": str(tmp_path / "o.db")})


def test_config_allows_localhost_http_and_reuses_full_token(tmp_path):
    env = {
        "MCP_OAUTH_ENABLED": "1",
        "MCP_PUBLIC_URL": "http://localhost:8082/",
        "WA_MCP_FULL_TOKEN": FULL_STATIC,
        "MCP_OAUTH_DB": str(tmp_path / "o.db"),
        "MCP_OAUTH_ACCESS_TTL": "60",
    }
    c = OAuthConfig.from_env(env)
    assert c is not None
    assert c.public_url == "http://localhost:8082"
    assert c.resource_url == "http://localhost:8082/mcp"
    assert c.passphrase == FULL_STATIC
    assert c.access_ttl == 60


def test_config_requires_strong_passphrase(tmp_path):
    env = {"MCP_OAUTH_ENABLED": "true", "MCP_PUBLIC_URL": PUBLIC, "MCP_OAUTH_PASSPHRASE": "short"}
    with pytest.raises(ValueError):
        OAuthConfig.from_env({**env, "MCP_OAUTH_DB": str(tmp_path / "o.db")})


# ─────────────────────────────────────────────────────────── metadata


def test_metadata_endpoints(cfg):
    with _client(cfg) as c:
        asm = c.get("/.well-known/oauth-authorization-server").json()
        assert asm["issuer"] == PUBLIC
        assert asm["authorization_endpoint"] == PUBLIC + "/authorize"
        assert asm["registration_endpoint"] == PUBLIC + "/register"
        assert asm["code_challenge_methods_supported"] == ["S256"]
        assert set(asm["scopes_supported"]) == {"wa:read", "wa:full"}

        for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
            prm = c.get(path).json()
            assert prm["resource"] == PUBLIC + "/mcp"
            assert prm["authorization_servers"] == [PUBLIC]


def test_unauthenticated_mcp_gets_resource_metadata_challenge(cfg):
    with _client(cfg) as c:
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401
        assert (
            'resource_metadata="https://wa.example.com/.well-known/oauth-protected-resource/mcp"'
            in (r.headers["www-authenticate"])
        )


def test_oauth_routes_enforce_host_allowlist(cfg):
    with _client(cfg, base_url="https://evil.example.net") as c:
        assert c.get("/.well-known/oauth-authorization-server").status_code == 421


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows has no POSIX mode bits; the file relies on the user profile ACL"
)
def test_store_file_is_private(cfg):
    with _client(cfg):
        pass
    assert stat.S_IMODE(os.stat(cfg.db_path).st_mode) == 0o600


# ─────────────────────────────────────────────────────────────── flow


def test_full_flow_read_scope_gates_write_tools(cfg):
    with _client(cfg) as c:
        _, tok = _login(c, scope="wa:read")
        assert tok["scope"] == "wa:read"
        assert tok["token_type"] == "Bearer"
        assert tok["expires_in"] == cfg.access_ttl

        ok = _mcp_call(c, tok["access_token"], "read_thing")
        assert "error" not in ok, ok
        assert ok["result"]["content"][0]["text"] == "read ok"

        denied = _mcp_call(c, tok["access_token"], "send_thing")
        assert denied["error"]["code"] == -32001


def test_full_scope_can_call_write_tools(cfg):
    with _client(cfg) as c:
        _, tok = _login(c, scope="wa:full")
        assert set(tok["scope"].split()) == {"wa:read", "wa:full"}
        res = _mcp_call(c, tok["access_token"], "send_thing")
        assert res["result"]["content"][0]["text"] == "sent"


def test_refresh_rotates_and_detects_reuse(cfg):
    with _client(cfg) as c:
        client_id, tok = _login(c)
        form = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client_id}
        r = c.post("/token", data=form)
        assert r.status_code == 200, r.text
        new = r.json()
        assert new["refresh_token"] != tok["refresh_token"]
        assert new["access_token"] != tok["access_token"]

        # The old access token died with the rotation; the new one works.
        assert c.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"}, json={}).status_code == 401
        assert "error" not in _mcp_call(c, new["access_token"], "read_thing")

        # Replaying the rotated refresh token is refused and revokes the grant.
        assert c.post("/token", data=form).status_code == 400
        r = c.post("/mcp", headers={"Authorization": f"Bearer {new['access_token']}"}, json={})
        assert r.status_code == 401

        # Refresh cannot widen a read-only grant.
        client2, tok2 = _login(c)
        r = c.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tok2["refresh_token"],
                "client_id": client2,
                "scope": "wa:full",
            },
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_scope"


def test_expired_access_token_is_rejected(cfg):
    with _client(cfg) as c:
        _, tok = _login(c)
        with sqlite3.connect(cfg.db_path) as conn:
            conn.execute("UPDATE tokens SET expires_at = 1 WHERE kind = 'access'")
        r = c.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"}, json={})
        assert r.status_code == 401


def test_revoked_client_loses_access(cfg, capsys):
    with _client(cfg) as c:
        client_id, tok = _login(c)
        assert main(["--db", cfg.db_path, "list"]) == 0
        assert client_id in capsys.readouterr().out
        assert main(["--db", cfg.db_path, "revoke", client_id]) == 0

        r = c.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"}, json={})
        assert r.status_code == 401
        r = c.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client_id},
        )
        assert r.status_code == 401
        assert main(["--db", cfg.db_path, "revoke", client_id]) == 1


def test_tokens_survive_restart(cfg):
    with _client(cfg) as c:
        _, tok = _login(c)
    with _client(cfg) as c:
        assert "error" not in _mcp_call(c, tok["access_token"], "read_thing")


def test_token_for_other_resource_is_rejected(cfg):
    with _client(cfg) as c:
        _, tok = _login(c)
        with sqlite3.connect(cfg.db_path) as conn:
            conn.execute("UPDATE tokens SET resource = 'https://other.example.com/mcp'")
        r = c.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"}, json={})
        assert r.status_code == 401


# ───────────────────────────────────────────────────────── refusals


@pytest.mark.parametrize(
    "redirect",
    [
        "http://example.com/cb",
        "javascript:alert(1)",
        "myapp://callback",
        "http://192.168.1.5/cb",
        "http://claude.ai/cb",
    ],
)
def test_bad_redirect_uri_rejected_at_registration(cfg, redirect):
    with _client(cfg) as c:
        r = _register(c, redirect=redirect)
        assert r.status_code == 400
        assert r.json()["error"] in {"invalid_redirect_uri", "invalid_client_metadata"}


def test_localhost_redirect_allowed(cfg):
    with _client(cfg) as c:
        assert _register(c, redirect="http://localhost:33418/callback").status_code == 201
        assert _register(c, redirect="http://127.0.0.1/cb").status_code == 201


def test_unregistered_redirect_uri_rejected_at_authorize(cfg):
    with _client(cfg) as c:
        client_id = _register(c).json()["client_id"]
        _, challenge = _pkce()
        r = _authorize(c, client_id, challenge, redirect_uri="https://attacker.example/cb")
        assert r.status_code == 400


def test_missing_pkce_rejected(cfg):
    with _client(cfg) as c:
        client_id = _register(c).json()["client_id"]
        r = c.get(
            "/authorize",
            params={"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": "s"},
        )
        assert r.status_code == 302
        assert parse_qs(urlparse(r.headers["location"]).query)["error"] == ["invalid_request"]

        r = _authorize(c, client_id, "x", code_challenge_method="plain")
        assert parse_qs(urlparse(r.headers["location"]).query)["error"] == ["invalid_request"]


def test_wrong_pkce_verifier_rejected(cfg):
    with _client(cfg) as c:
        client_id = _register(c).json()["client_id"]
        _, challenge = _pkce()
        r = _consent(c, _authorize(c, client_id, challenge).headers["location"])
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        r = c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "client_id": client_id,
                "code_verifier": secrets.token_urlsafe(48),
            },
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"


def test_foreign_resource_indicator_rejected(cfg):
    with _client(cfg) as c:
        client_id = _register(c).json()["client_id"]
        _, challenge = _pkce()
        r = _authorize(c, client_id, challenge, resource="https://other.example.com/mcp")
        assert parse_qs(urlparse(r.headers["location"]).query)["error"] == ["invalid_target"]


def _req(location):
    return parse_qs(urlparse(location).query)["req"][0]


def _start(c):
    """Register + /authorize in browser `c`; returns (client_id, verifier, consent location)."""
    client_id = _register(c).json()["client_id"]
    verifier, challenge = _pkce()
    r = _authorize(c, client_id, challenge)
    assert r.status_code == 302, r.text
    return client_id, verifier, r.headers["location"]


def _exchange(c, client_id, verifier, consent_response):
    code = parse_qs(urlparse(consent_response.headers["location"]).query)["code"][0]
    return c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )


def test_consent_page_identifies_client_by_redirect_host(cfg):
    with _client(cfg) as c:
        _, _, loc = _start(c)
        page = c.get(loc)
        assert page.status_code == 200
        assert page.headers["x-frame-options"] == "DENY"
        assert '<p class="host">claude.ai</p>' in page.text
        assert "Claude <em>(self-declared, unverified)</em>" in page.text
        assert f"python -m lib.oauth approve {_req(loc)} --scope read" in page.text


def test_wrong_passphrase_burns_request_after_three_tries(tmp_path):
    # Distinct client IPs (via a trusted proxy) so per-IP backoff stays out of the way.
    cfg = OAuthConfig(
        public_url=PUBLIC, passphrase=PASSPHRASE, db_path=str(tmp_path / "o.db"), trusted_proxies=("testclient",)
    )
    with _client(cfg) as c:
        _, _, loc = _start(c)
        for i, expected in enumerate([401, 401, 403]):
            r = _consent(c, loc, passphrase="nope", headers={"X-Forwarded-For": f"203.0.113.{i}"})
            assert r.status_code == expected
        assert _consent(c, loc, headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 400  # gone


def test_backoff_is_per_ip_and_cannot_lock_out_the_operator(tmp_path):
    cfg = OAuthConfig(
        public_url=PUBLIC, passphrase=PASSPHRASE, db_path=str(tmp_path / "o.db"), trusted_proxies=("testclient",)
    )
    attacker = {"X-Forwarded-For": "203.0.113.66"}
    with _client(cfg) as c:
        _, _, loc = _start(c)
        assert _consent(c, loc, passphrase="nope", headers=attacker).status_code == 401
        r = _consent(c, loc, passphrase="nope", headers=attacker)
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
        # Many more failures from other attacker addresses lock nobody else out.
        # (8, not more: this test registers every client from one address, and 10 per hour is the cap.)
        for i in range(8):
            _, _, other = _start(c)
            _consent(c, other, passphrase="nope", headers={"X-Forwarded-For": f"203.0.113.{100 + i}"})
        # The operator, from their own address, still gets in.
        client_id, verifier, loc = _start(c)
        r = _consent(c, loc, headers={"X-Forwarded-For": "198.51.100.7"})
        assert r.status_code == 303
        assert _exchange(c, client_id, verifier, r).status_code == 200


def test_backoff_grows_and_ignores_untrusted_forwarded_for(cfg):
    from lib.oauth import IPBackoff, client_ip

    b = IPBackoff()
    b.fail("1.2.3.4")
    first = b.retry_after("1.2.3.4")
    b.fail("1.2.3.4")
    b.fail("1.2.3.4")
    assert b.retry_after("1.2.3.4") > first
    assert b.retry_after("5.6.7.8") == 0

    scope = {"client": ("10.0.0.9", 1), "headers": [(b"x-forwarded-for", b"6.6.6.6")]}
    assert client_ip(scope, ()) == "10.0.0.9"  # peer not trusted: header ignored
    assert client_ip(scope, ("10.0.0.9",)) == "6.6.6.6"
    chained = {"client": ("127.0.0.1", 1), "headers": [(b"x-forwarded-for", b"6.6.6.6, 7.7.7.7, 127.0.0.1")]}
    assert client_ip(chained, ("127.0.0.1",)) == "7.7.7.7"  # rightmost untrusted hop, not the spoofable left

    # Without a trusted proxy, rotating X-Forwarded-For does not reset the backoff.
    with _client(cfg) as c:
        _, _, loc = _start(c)
        assert _consent(c, loc, passphrase="nope", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 401
        assert _consent(c, loc, passphrase="nope", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 429


def test_cli_approve_bypasses_passphrase_and_backoff(cfg, capsys):
    with _client(cfg) as c:
        client_id, verifier, loc = _start(c)
        req = _req(loc)
        assert _consent(c, loc, passphrase="nope").status_code == 401
        assert _consent(c, loc, passphrase="nope").status_code == 429  # this IP is backing off
        assert _consent(c, loc, decision="continue").status_code == 409  # not approved yet

        assert main(["--db", cfg.db_path, "approve", req, "--scope", "full"]) == 0
        assert "claude.ai" in capsys.readouterr().out
        r = _consent(c, loc, decision="continue")
        assert r.status_code == 303, r.text
        tok = _exchange(c, client_id, verifier, r).json()
        assert set(tok["scope"].split()) == {"wa:read", "wa:full"}

        assert main(["--db", cfg.db_path, "approve", req, "--scope", "read"]) == 1  # used up


def test_consent_link_is_bound_to_the_authorizing_browser(cfg):
    app = serve_http.build_app(_server(), host="127.0.0.1", port=8082, tokens={}, oauth_config=cfg)
    with TestClient(app, base_url=PUBLIC, follow_redirects=False) as attacker:
        client_id = _register(attacker).json()["client_id"]
        _, challenge = _pkce()
        r = _authorize(attacker, client_id, challenge)
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" in cookie
        assert "Path=/oauth/consent" in cookie
        loc = r.headers["location"]

        # The operator opens a forwarded link: no cookie, nothing to approve.
        operator = TestClient(app, base_url=PUBLIC, follow_redirects=False)
        assert operator.get(loc).status_code == 403
        assert _consent(operator, loc).status_code == 403
        assert _consent(operator, loc, decision="continue").status_code == 403
        # A guessed cookie value does not help either.
        operator.cookies.set(f"wac_{_req(loc)}", "guess", path="/oauth/consent")
        assert _consent(operator, loc).status_code == 403
        # The browser that started it still can.
        assert _consent(attacker, loc).status_code == 303


def test_redirect_host_allowlist_at_registration(tmp_path):
    base = OAuthConfig(public_url=PUBLIC, passphrase=PASSPHRASE, db_path=str(tmp_path / "o.db"))
    with _client(base) as c:
        r = _register(c, redirect="https://evil.example/cb")
        assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"
        assert _register(c, redirect="https://chatgpt.com/connector_platform_oauth_redirect").status_code == 201
        assert _register(c, redirect="https://sub.claude.ai/cb").status_code == 400  # exact host only
    with _client(replace(base, allowed_redirect_hosts=("example.org",))) as c:
        assert _register(c, redirect="https://example.org/cb").status_code == 201
        assert _register(c, redirect=REDIRECT).status_code == 400


def test_redirect_host_allowlist_rechecked_at_authorize(tmp_path):
    base = OAuthConfig(public_url=PUBLIC, passphrase=PASSPHRASE, db_path=str(tmp_path / "o.db"))
    with _client(base) as c:
        client_id = _register(c).json()["client_id"]
    # Operator narrows the allowlist; the already-registered client is refused
    # without being redirected back to its (now untrusted) host.
    with _client(replace(base, allowed_redirect_hosts=("chatgpt.com",))) as c:
        _, challenge = _pkce()
        r = _authorize(c, client_id, challenge)
        assert r.status_code == 302
        assert r.headers["location"].startswith(PUBLIC + CONSENT_PATH + "?refused=")
        page = c.get(r.headers["location"])
        assert page.status_code == 400 and "MCP_OAUTH_ALLOWED_REDIRECT_HOSTS" in page.text


def test_config_reads_redirect_hosts_and_trusted_proxy(tmp_path):
    env = {
        "MCP_OAUTH_ENABLED": "true",
        "MCP_PUBLIC_URL": PUBLIC,
        "MCP_OAUTH_PASSPHRASE": PASSPHRASE,
        "MCP_OAUTH_DB": str(tmp_path / "o.db"),
    }
    c = OAuthConfig.from_env(env)
    assert c is not None
    assert c.allowed_redirect_hosts == ("claude.ai", "claude.com", "chatgpt.com", "localhost", "127.0.0.1")
    assert c.trusted_proxies == ()
    c = OAuthConfig.from_env(
        {**env, "MCP_OAUTH_ALLOWED_REDIRECT_HOSTS": "Claude.ai, example.org", "MCP_TRUSTED_PROXY": "127.0.0.1"}
    )
    assert c is not None
    assert c.allowed_redirect_hosts == ("claude.ai", "example.org")
    assert c.trusted_proxies == ("127.0.0.1",)


def test_consent_deny_redirects_with_access_denied(cfg):
    with _client(cfg) as c:
        client_id = _register(c).json()["client_id"]
        _, challenge = _pkce()
        r = _consent(c, _authorize(c, client_id, challenge).headers["location"], decision="deny")
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["error"] == ["access_denied"] and q["state"] == ["st4te"]


def test_code_cannot_be_replayed(cfg):
    with _client(cfg) as c:
        client_id = _register(c).json()["client_id"]
        verifier, challenge = _pkce()
        r = _consent(c, _authorize(c, client_id, challenge).headers["location"])
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        }
        tok = c.post("/token", data=form).json()
        assert c.post("/token", data=form).status_code == 400
        # The replay revoked what the code bought.
        r = c.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"}, json={})
        assert r.status_code == 401


# ────────────────────────────────────────────── static tokens alongside


def test_static_tokens_still_work_with_oauth_on(cfg):
    with _client(cfg, tokens={FULL_STATIC: "full", RO_STATIC: "readonly"}) as c:
        assert _mcp_call(c, FULL_STATIC, "send_thing")["result"]["content"][0]["text"] == "sent"
        assert _mcp_call(c, RO_STATIC, "send_thing")["error"]["code"] == -32001
        assert "error" not in _mcp_call(c, RO_STATIC, "read_thing")


def test_static_tokens_unchanged_with_oauth_off():
    app = serve_http.build_app(_server(), host="127.0.0.1", port=8082, tokens={RO_STATIC: "readonly"})
    with TestClient(app, base_url="http://127.0.0.1:8082") as c:
        r = c.post("/mcp", json={})
        assert r.status_code == 401 and "www-authenticate" not in r.headers
        assert c.get("/.well-known/oauth-authorization-server").status_code == 401
        assert _mcp_call(c, RO_STATIC, "send_thing")["error"]["code"] == -32001
        assert "error" not in _mcp_call(c, RO_STATIC, "read_thing")


def test_public_host_allowed_only_with_oauth():
    hosts, origins = serve_http.allowed_hosts("127.0.0.1", 8082, PUBLIC)
    assert "wa.example.com" in hosts and "wa.example.com:443" in hosts
    assert "https://wa.example.com" in origins
    assert serve_http.allowed_hosts("127.0.0.1", 8082) == (["127.0.0.1:8082"], ["http://127.0.0.1:8082"])


def test_backoff_key_groups_ipv6_by_64():
    from lib.oauth import backoff_key

    assert backoff_key("2001:db8:1:2:aaaa::1") == backoff_key("2001:db8:1:2:ffff:ffff:ffff:ffff") == "2001:db8:1:2::/64"
    assert backoff_key("2001:db8:1:3::1") != backoff_key("2001:db8:1:2::1")
    assert backoff_key("203.0.113.5") == "203.0.113.5"
    assert backoff_key("203.0.113.5") != backoff_key("203.0.113.6")
    assert backoff_key("::ffff:203.0.113.5") == "203.0.113.5"
    assert backoff_key("testclient") == "testclient"


def test_backoff_shared_across_a_64():
    from lib.oauth import IPBackoff

    b = IPBackoff()
    b.fail("2001:db8:1:2::1")
    assert b.retry_after("2001:db8:1:2:dead:beef:0:1") > 0  # same /64, rotated host bits
    assert b.retry_after("2001:db8:1:3::1") == 0


def test_backoff_not_reset_by_success_within_window():
    from lib.oauth import IPBackoff

    b = IPBackoff(window=3600)
    b.fail("1.2.3.4")
    b.fail("1.2.3.4")
    b.succeed("1.2.3.4")
    assert b.retry_after("1.2.3.4") > 0
    b.fail("1.2.3.4")
    assert b.state["1.2.3.4"][0] == 3  # the doubling series continued

    # Once the last failure is older than the window, success clears it and a
    # new failure starts the series over.
    count, until, last = b.state["1.2.3.4"]
    b.state["1.2.3.4"] = (count, until - 7200, last - 7200)
    b.succeed("1.2.3.4")
    assert "1.2.3.4" not in b.state
    b.state["5.6.7.8"] = (9, 0.0, time.time() - 7200)
    b.fail("5.6.7.8")
    assert b.state["5.6.7.8"][0] == 1


def test_failure_budget_slides():
    from lib.oauth import FailureBudget

    b = FailureBudget(limit=2, window=60)
    b.fail()
    assert not b.exhausted()
    b.fail()
    assert b.exhausted()
    b.failures = deque(t - 120 for t in b.failures)  # the window moves past them
    assert not b.exhausted()


def test_global_failure_budget_disables_passphrase_but_not_cli(tmp_path, caplog, capsys):
    cfg = OAuthConfig(
        public_url=PUBLIC,
        passphrase=PASSPHRASE,
        db_path=str(tmp_path / "o.db"),
        trusted_proxies=("testclient",),
        failure_budget=3,
    )
    with _client(cfg) as c, caplog.at_level(logging.WARNING, logger="whatsapp-mcp.oauth"):
        # A distributed guesser: every attempt from a fresh address, so no
        # per-address backoff ever applies.
        for i in range(3):
            _, _, loc = _start(c)
            r = _consent(c, loc, passphrase="nope", headers={"X-Forwarded-For": f"2001:db8:{i}::1"})
            assert r.status_code == 401
        tripped = [r for r in caplog.records if "passphrase approval is disabled" in r.getMessage()]
        assert len(tripped) == 1 and tripped[0].levelno == logging.WARNING

        # Now even the right passphrase from a clean address is refused...
        client_id, verifier, loc = _start(c)
        r = _consent(c, loc, headers={"X-Forwarded-For": "198.51.100.7"})
        assert r.status_code == 429
        assert "temporarily disabled" in r.text and "python -m lib.oauth approve" in r.text
        assert "temporarily disabled" in c.get(loc).text

        # ...but the CLI path still works, so the operator is never locked out.
        assert main(["--db", cfg.db_path, "approve", _req(loc), "--scope", "read"]) == 0
        capsys.readouterr()
        r = _consent(c, loc, decision="continue")
        assert r.status_code == 303
        assert _exchange(c, client_id, verifier, r).status_code == 200


def test_config_reads_failure_budget(tmp_path):
    env = {
        "MCP_OAUTH_ENABLED": "true",
        "MCP_PUBLIC_URL": PUBLIC,
        "MCP_OAUTH_PASSPHRASE": PASSPHRASE,
        "MCP_OAUTH_DB": str(tmp_path / "o.db"),
    }
    c = OAuthConfig.from_env(env)
    assert c is not None and (c.failure_budget, c.failure_window) == (20, 3600)
    c = OAuthConfig.from_env({**env, "MCP_OAUTH_FAILURE_BUDGET": "5", "MCP_OAUTH_FAILURE_WINDOW": "600"})
    assert c is not None and (c.failure_budget, c.failure_window) == (5, 600)
    with pytest.raises(ValueError):
        OAuthConfig.from_env({**env, "MCP_OAUTH_FAILURE_BUDGET": "0"})


# ---------- log forgery (L4) ----------


def test_log_safe_strips_control_characters_and_caps_length():
    forged = "x\n2026-11-24 12:00:00 INFO OAuth consent granted: client evil\r\x1b[31m end"
    cleaned = oauth.log_safe(forged)
    assert "\n" not in cleaned and "\r" not in cleaned and "\x1b" not in cleaned and " " not in cleaned
    assert len(oauth.log_safe("a" * 1000)) <= oauth.LOG_VALUE_MAX + 3


def test_registration_log_line_cannot_be_forged(cfg, caplog):
    forged = "x\nOAuth consent granted (passphrase): client evil -> claude.ai, scope wa:full" + "y" * 500
    with caplog.at_level("INFO", logger="whatsapp-mcp.oauth"):
        r = _client(cfg).post(
            "/register",
            json={"redirect_uris": [REDIRECT], "client_name": forged, "token_endpoint_auth_method": "none"},
        )
    assert r.status_code == 201
    lines = [rec.getMessage() for rec in caplog.records if "client registered" in rec.getMessage()]
    assert len(lines) == 1
    assert "\n" not in lines[0]
    assert len(lines[0]) < 300


# ---------- open registration (L6) ----------


def _reg(c, **kw):
    return c.post(
        "/register",
        json={"redirect_uris": [REDIRECT], "client_name": "c", "token_endpoint_auth_method": "none"},
        **kw,
    )


def test_registrations_are_rate_limited_per_ip(cfg):
    c = _client(cfg)
    assert [_reg(c).status_code for _ in range(oauth.REGISTRATIONS_PER_IP)] == [201] * oauth.REGISTRATIONS_PER_IP
    blocked = _reg(c)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    assert blocked.json()["error"] == "too_many_requests"


def test_untrusted_peer_cannot_dodge_the_limit_with_forwarded_for(cfg):
    c = _client(cfg)
    for i in range(oauth.REGISTRATIONS_PER_IP):
        assert _reg(c, headers={"X-Forwarded-For": f"203.0.113.{i}"}).status_code == 201
    assert _reg(c, headers={"X-Forwarded-For": "198.51.100.77"}).status_code == 429


def test_trusted_proxy_forwarded_for_gives_each_client_its_own_budget(tmp_path):
    cfg = OAuthConfig(
        public_url=PUBLIC,
        passphrase=PASSPHRASE,
        db_path=str(tmp_path / "data" / "oauth.db"),
        trusted_proxies=("testclient",),
    )
    c = _client(cfg)
    for _ in range(oauth.REGISTRATIONS_PER_IP):
        assert _reg(c, headers={"X-Forwarded-For": "203.0.113.5"}).status_code == 201
    assert _reg(c, headers={"X-Forwarded-For": "203.0.113.5"}).status_code == 429
    assert _reg(c, headers={"X-Forwarded-For": "203.0.113.6"}).status_code == 201
    # A hop the client forged in front of the proxy's own entry is ignored: the nearest untrusted hop counts.
    assert _reg(c, headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.5"}).status_code == 429


def test_registration_limit_window_slides(monkeypatch):
    limit = oauth.RegistrationLimit(limit=2, window=100)
    now = [1000.0]
    monkeypatch.setattr(oauth.time, "time", lambda: now[0])
    assert limit.check("203.0.113.1") == 0 and limit.check("203.0.113.1") == 0
    assert limit.check("203.0.113.1") > 0
    now[0] += 101
    assert limit.check("203.0.113.1") == 0


def _info(i, name="c"):
    from mcp.shared.auth import OAuthClientInformationFull

    return OAuthClientInformationFull(client_id=f"c{i}", redirect_uris=[AnyUrl(REDIRECT)], client_name=name)


def test_client_table_cap_is_200_and_a_flood_cannot_lock_out_a_new_registration(tmp_path):
    assert oauth.MAX_CLIENTS == 200
    store = OAuthStore(str(tmp_path / "o.db"))
    for i in range(oauth.MAX_CLIENTS):
        store.add_client(_info(i))
    # Full of clients that never got a token: the next registration evicts the oldest instead of being refused.
    store.add_client(_info("legit"))
    assert store.get_client("clegit") is not None
    assert store.get_client("c0") is None
    assert len(store.list_clients()) == oauth.MAX_CLIENTS


def test_a_registration_flood_through_the_api_does_not_stop_the_owner(tmp_path):
    cfg = OAuthConfig(
        public_url=PUBLIC, passphrase=PASSPHRASE, db_path=str(tmp_path / "o.db"), trusted_proxies=("testclient",)
    )
    c = _client(cfg)
    for i in range(oauth.MAX_CLIENTS + 20):  # many sources, so the per-address limit is not what is tested
        assert _reg(c, headers={"X-Forwarded-For": f"203.0.{i // 250}.{i % 250 + 1}"}).status_code == 201
    owner = _reg(c, headers={"X-Forwarded-For": "198.51.100.7"})
    assert owner.status_code == 201


def test_clients_in_use_are_never_evicted(tmp_path):
    import sqlite3

    store = OAuthStore(str(tmp_path / "o.db"))
    for i in range(oauth.MAX_CLIENTS):
        store.add_client(_info(i))
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE clients SET last_used_at = ?", (int(time.time()),))
    with pytest.raises(oauth.RegistrationError):
        store.add_client(_info("new"))
    assert store.get_client("c0") is not None


def test_registration_limit_is_generous_enough_for_a_shared_connector_address():
    assert oauth.REGISTRATIONS_PER_IP >= 100


def test_client_name_is_capped_and_cleaned_in_storage_and_cli(cfg, capsys):
    c = _client(cfg)
    r = c.post(
        "/register",
        json={
            "redirect_uris": [REDIRECT],
            "client_name": "evil\x1b[31m\rname" + "x" * 5000,
            "token_endpoint_auth_method": "none",
        },
    )
    assert r.status_code == 201
    stored = OAuthStore(cfg.db_path).get_client(r.json()["client_id"])
    assert stored is not None and stored.client_name is not None
    assert len(stored.client_name) <= oauth.CLIENT_NAME_MAX + 3
    assert "\x1b" not in stored.client_name and "\r" not in stored.client_name
    # The CLI prints through log_safe too, whatever is already in the database.
    import sqlite3

    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute("UPDATE clients SET info_json = replace(info_json, 'evil', 'evil\\u001b[2J')")
    main(["--db", cfg.db_path, "list"])
    assert "\x1b" not in capsys.readouterr().out


def test_unused_clients_older_than_30_days_are_pruned(tmp_path):
    import sqlite3

    from mcp.shared.auth import OAuthClientInformationFull

    store = OAuthStore(str(tmp_path / "o.db"))
    for cid in ("old-unused", "old-used", "new-unused"):
        store.add_client(OAuthClientInformationFull(client_id=cid, redirect_uris=[AnyUrl(REDIRECT)], client_name="c"))
        store.mark_approved(cid)
    old = int(time.time()) - oauth.UNUSED_CLIENT_TTL - 60
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE clients SET created_at = ? WHERE client_id LIKE 'old-%'", (old,))
        conn.execute("UPDATE clients SET last_used_at = ? WHERE client_id = 'old-used'", (int(time.time()),))
        conn.execute(
            "INSERT INTO pending (req_id, client_id, client_name, params_json, offered, nonce_hash, created_at)"
            " VALUES ('r1', 'old-unused', 'c', '{}', 'wa:read', 'h', ?)",
            (time.time(),),
        )
    store.prune()
    assert store.get_client("old-unused") is None
    assert store.get_client("old-used") is not None
    assert store.get_client("new-unused") is not None
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0


def _pending_rows(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT req_id, client_id, src FROM pending").fetchall()


def test_expired_pending_authorizations_are_pruned(cfg):
    c = _client(cfg)
    client_id = _register(c).json()["client_id"]
    _, challenge = _pkce()
    with sqlite3.connect(cfg.db_path) as conn:
        for i in range(oauth.MAX_PENDING):
            conn.execute(
                "INSERT INTO pending (req_id, client_id, client_name, params_json, offered, nonce_hash, created_at)"
                " VALUES (?, 'other', 'c', '{}', 'wa:read', 'h', ?)",
                (f"old{i}", time.time() - oauth.PENDING_TTL - 5),
            )
    assert _authorize(c, client_id, challenge).status_code == 302
    assert len(_pending_rows(cfg.db_path)) == 1


def test_one_ip_flooding_authorize_blocks_neither_another_ip_nor_another_client(tmp_path):
    cfg = OAuthConfig(
        public_url=PUBLIC, passphrase=PASSPHRASE, db_path=str(tmp_path / "o.db"), trusted_proxies=("testclient",)
    )
    c = _client(cfg)
    flood = {"X-Forwarded-For": "203.0.113.9"}
    _, challenge = _pkce()
    flooder = c.post(
        "/register",
        json={"redirect_uris": [REDIRECT], "client_name": "f", "token_endpoint_auth_method": "none"},
        headers=flood,
    ).json()["client_id"]
    for _ in range(oauth.PENDING_PER_PRINCIPAL + 4):
        assert _authorize_h(c, flooder, challenge, flood).status_code == 302
    mine = [r for r in _pending_rows(cfg.db_path) if r[1] == flooder]
    assert len(mine) == oauth.PENDING_PER_PRINCIPAL  # its own oldest were evicted, nobody was refused
    # The flooder's address is now rate limited; the operator on another address is not.
    for _ in range(oauth.AUTHORIZES_PER_IP):
        _authorize_h(c, flooder, challenge, flood)
    blocked = _authorize_h(c, flooder, challenge, flood)
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0
    other_client = c.post(
        "/register",
        json={"redirect_uris": [REDIRECT], "client_name": "op", "token_endpoint_auth_method": "none"},
        headers={"X-Forwarded-For": "198.51.100.7"},
    ).json()["client_id"]
    ok = _authorize_h(c, other_client, challenge, {"X-Forwarded-For": "198.51.100.7"})
    assert ok.status_code == 302 and "/oauth/consent?req=" in ok.headers["location"]
    # Even from the flooder's address, a different client is not shut out by the flooder's pending rows
    # (it is only slowed by the per-address rate limit, which has not run out for a fresh address).
    again = _authorize_h(c, other_client, challenge, {"X-Forwarded-For": "198.51.100.8"})
    assert again.status_code == 302


def test_a_full_pending_table_evicts_the_oldest_instead_of_refusing(cfg):
    c = _client(cfg)
    client_id = _register(c).json()["client_id"]
    _, challenge = _pkce()
    with sqlite3.connect(cfg.db_path) as conn:
        for i in range(oauth.MAX_PENDING):
            conn.execute(
                "INSERT INTO pending (req_id, client_id, client_name, params_json, offered, nonce_hash, created_at, src)"
                " VALUES (?, ?, 'c', '{}', 'wa:read', 'h', ?, ?)",
                (f"live{i}", f"other{i}", time.time() - 100 + i * 0.01, f"ip{i}"),
            )
    r = _authorize(c, client_id, challenge)
    assert r.status_code == 302 and "/oauth/consent?req=" in r.headers["location"]
    rows = _pending_rows(cfg.db_path)
    assert len(rows) == oauth.MAX_PENDING
    assert "live0" not in {x[0] for x in rows}  # the oldest went
    assert client_id in {x[1] for x in rows}


def test_request_ids_never_start_with_a_dash(monkeypatch):
    from lib import oauth as oauth_mod

    tokens = iter(["-starts-with-dash", "fine-token"])
    monkeypatch.setattr(oauth_mod.secrets, "token_urlsafe", lambda n: next(tokens))
    assert oauth_mod._cli_safe_token(24) == "fine-token"

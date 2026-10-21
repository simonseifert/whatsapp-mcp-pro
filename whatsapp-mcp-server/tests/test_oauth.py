"""OAuth 2.1 for serve_http, end to end against the in-process ASGI app.

A two-tool server stands in for main.mcp: the gate under test is generic over
readOnlyHint, and the real tools would need a bridge and a store.
"""

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import stat
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from starlette.testclient import TestClient

import serve_http
from lib.oauth import CONSENT_PATH, OAuthConfig, main

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


def _consent(c, location, scope="wa:read", passphrase=PASSPHRASE, decision="approve"):
    req = parse_qs(urlparse(location).query)["req"][0]
    return c.post(CONSENT_PATH, data={"req": req, "scope": scope, "passphrase": passphrase, "decision": decision})


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
    "redirect", ["http://example.com/cb", "javascript:alert(1)", "myapp://callback", "http://192.168.1.5/cb"]
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


def test_consent_wrong_passphrase_and_lockout(cfg):
    with _client(cfg) as c:
        client_id = _register(c).json()["client_id"]
        _, challenge = _pkce()
        loc = _authorize(c, client_id, challenge).headers["location"]
        page = c.get(loc)
        assert page.status_code == 200
        assert "Claude" in page.text and page.headers["x-frame-options"] == "DENY"

        assert _consent(c, loc, passphrase="nope").status_code == 401
        assert _consent(c, loc, passphrase="nope").status_code == 401
        assert _consent(c, loc, passphrase="nope").status_code == 403  # request burned
        assert _consent(c, loc).status_code == 400  # and gone

        loc = _authorize(c, client_id, challenge).headers["location"]
        _consent(c, loc, passphrase="nope")
        _consent(c, loc, passphrase="nope")
        # Five failures across requests lock consent even with the right passphrase.
        assert _consent(c, loc).status_code == 429


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

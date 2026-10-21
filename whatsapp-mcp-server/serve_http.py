"""Shared streamable-HTTP entrypoint for the WhatsApp MCP server.

One always-on instance (a systemd unit or launchd agent) replaces spawning
`uv run main.py` over stdio once per Claude session, e.g. via ssh to the box
that holds the store. Those leaked: the stdio server does not exit on stdin
EOF, so every finished session orphaned a python process (82 of them took
44 GB and crashed the host once).

Scoped access: if WA_MCP_FULL_TOKEN / WA_MCP_READONLY_TOKEN are set (.env),
requests need `Authorization: Bearer <token>`. The read-only token can only
call tools whose annotations declare readOnlyHint=true; everything else gets
a JSON-RPC error. With no tokens configured, auth is off (tailnet-only bind
remains the outer wall).

Optional OAuth 2.1 (MCP_OAUTH_ENABLED=true, see lib/oauth.py) adds the
endpoints remote connectors (claude.ai, ChatGPT) need. An OAuth access token
resolves to the same "full"/"readonly" role a static token does and then
goes through the same gate below; static tokens keep working unchanged.

Clients connect via Claude Code MCP config:
    type=http, url=http://<MCP_HOST>:8082/mcp
    (+ header Authorization: Bearer <token> when tokens are configured)
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from lib.oauth import OAuthConfig, OAuthGuard


def static_tokens(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Bearer token -> role from WA_MCP_FULL_TOKEN / WA_MCP_READONLY_TOKEN."""
    env = os.environ if env is None else env
    tokens = {}
    if env.get("WA_MCP_FULL_TOKEN"):
        tokens[env["WA_MCP_FULL_TOKEN"]] = "full"
    if env.get("WA_MCP_READONLY_TOKEN"):
        tokens[env["WA_MCP_READONLY_TOKEN"]] = "readonly"
    return tokens


def readonly_tool_names(server: Any) -> set[str]:
    """Tools a readonly role may call: those annotated readOnlyHint=true."""
    return {
        t.name for t in server._tool_manager.list_tools() if t.annotations is not None and t.annotations.read_only_hint
    }


def allowed_hosts(host: str, port: int, public_url: str | None = None) -> tuple[list[str], list[str]]:
    """Host and Origin header allowlists for DNS-rebinding protection.

    With OAuth on, requests arrive through a proxy/funnel carrying the public
    hostname, so that has to pass the same check the bind address does.
    """
    hosts = [f"{host}:{port}"]
    origins = [f"http://{host}:{port}"]
    if public_url:
        parsed = urlparse(public_url)
        default_port = 443 if parsed.scheme == "https" else 80
        hosts += [parsed.netloc, f"{parsed.hostname}:{parsed.port or default_port}"]
        origins.append(f"{parsed.scheme}://{parsed.netloc}")
    return list(dict.fromkeys(hosts)), list(dict.fromkeys(origins))


def _filter_tools_payload(payload, readonly_tools):
    """Drop non-read-only tools from a tools/list result, in place."""
    try:
        tools = payload["result"]["tools"]
    except (KeyError, TypeError):
        return payload
    payload["result"]["tools"] = [t for t in tools if t.get("name") in readonly_tools]
    return payload


class ScopedAuth:
    """Pure ASGI middleware: bearer-token auth + read-only tool gating."""

    def __init__(self, app, tokens: dict[str, str], readonly_tools: set[str], oauth: OAuthGuard | None = None):
        self.app = app
        self.tokens = tokens
        self.readonly_tools = readonly_tools
        self.oauth = oauth

    def _filtered_list_sender(self, send):
        """Wrap `send` so a tools/list response is filtered before it leaves.

        The whole response is buffered because content-length must be
        rewritten, and a streamed body cannot be edited after its header
        has gone out.
        """
        start_msg: dict[str, Any] | None = None
        chunks: list[bytes] = []
        readonly_tools = self.readonly_tools

        async def wrapped(message):
            nonlocal start_msg
            if message["type"] == "http.response.start":
                start_msg = message
                return
            if message["type"] != "http.response.body":
                return await send(message)
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            raw = b"".join(chunks)
            out = raw
            try:
                text = raw.decode("utf-8")
                if text.lstrip().startswith("{"):
                    out = json.dumps(_filter_tools_payload(json.loads(text), readonly_tools)).encode()
                else:  # SSE frames: rewrite each `data:` line, keep the rest
                    lines = []
                    for line in text.split("\n"):
                        if line.startswith("data:"):
                            payload = json.loads(line[5:].strip())
                            lines.append("data: " + json.dumps(_filter_tools_payload(payload, readonly_tools)))
                        else:
                            lines.append(line)
                    out = "\n".join(lines).encode()
            except Exception:
                out = raw  # never break the response over cosmetics
            start = start_msg or {"type": "http.response.start", "status": 200, "headers": []}
            headers = [(k, v) for k, v in start.get("headers", []) if k.decode().lower() != "content-length"]
            headers.append((b"content-length", str(len(out)).encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": out, "more_body": False})

        return wrapped

    async def _role(self, scope) -> str | None:
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        token = headers.get("authorization", "").removeprefix("Bearer ").strip()
        role = self.tokens.get(token)
        if role is None and self.oauth is not None and token:
            role = await self.oauth.role(scope)
        return role

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or (not self.tokens and self.oauth is None):
            return await self.app(scope, receive, send)
        role = await self._role(scope)
        if role is None:
            # MCP authorization spec: a 401 must point OAuth clients at the
            # protected-resource metadata, or they never find the login.
            extra = [(b"www-authenticate", self.oauth.challenge().encode())] if self.oauth else []
            return await self._reply(send, 401, {"error": "unauthorized"}, extra)
        if role == "full" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)

        chunks, more = [], True
        while more:
            msg = await receive()
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        body = b"".join(chunks)

        try:
            rpc = json.loads(body)
        except Exception:
            rpc = {}
        # params can legally be null or a list in JSON-RPC; .get on those
        # raises and turned a malformed request into a 500. Treat any
        # non-dict params as "not a readonly tool" and reject cleanly.
        params = rpc.get("params") if isinstance(rpc, dict) else None
        if (
            isinstance(rpc, dict)
            and rpc.get("method") == "tools/call"
            and (not isinstance(params, dict) or params.get("name") not in self.readonly_tools)
        ):
            return await self._reply(
                send,
                200,
                {
                    "jsonrpc": "2.0",
                    "id": rpc.get("id"),
                    "error": {
                        "code": -32001,
                        "message": "read-only token: this tool is not read-only",
                    },
                },
            )

        # tools/call is gated above. tools/list also needs filtering: left
        # unfiltered a read-only client is shown every tool, including the
        # ~20 it cannot call, and will confidently try one and burn a turn
        # on a rejection. The response may come back as plain JSON or as an
        # SSE frame, so handle both framings.
        if isinstance(rpc, dict) and rpc.get("method") == "tools/list":
            send = self._filtered_list_sender(send)

        body_sent = False

        async def replay():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            # After the body: delegate so disconnect detection still works.
            return await receive()

        return await self.app(scope, replay, send)

    @staticmethod
    async def _reply(send, status, payload, extra_headers=()):
        data = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json"), *extra_headers],
            }
        )
        await send({"type": "http.response.body", "body": data})


class OAuthRouter:
    """Send OAuth/metadata paths to the OAuth app, everything else to the MCP app.

    The SDK's Host check lives inside the MCP transport and so never sees the
    OAuth routes; they get the same allowlist here. Lifespan goes to the MCP
    app, whose session manager has to start.
    """

    def __init__(self, mcp_app, oauth_app, paths: set[str], hosts: list[str]):
        self.mcp_app = mcp_app
        self.oauth_app = oauth_app
        self.paths = paths
        self.hosts = set(hosts)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") in self.paths:
            host = next((v.decode() for k, v in scope.get("headers", []) if k.lower() == b"host"), "")
            if host not in self.hosts:
                await send({"type": "http.response.start", "status": 421, "headers": []})
                await send({"type": "http.response.body", "body": b"Invalid Host header"})
                return
            return await self.oauth_app(scope, receive, send)
        return await self.mcp_app(scope, receive, send)


def build_app(
    server: Any,
    *,
    host: str,
    port: int,
    tokens: dict[str, str],
    oauth_config: OAuthConfig | None = None,
):
    """The ASGI app serve_http runs: MCP transport + auth (+ OAuth when configured)."""
    from mcp.server.transport_security import TransportSecuritySettings

    public_url = oauth_config.public_url if oauth_config else None
    hosts, origins = allowed_hosts(host, port, public_url)
    # The SDK's DNS-rebinding protection only allows localhost Hosts by
    # default; without this, tailnet clients get 421 Misdirected Request.
    transport_security = TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)
    mcp_app = server.streamable_http_app(transport_security=transport_security, host=host)

    if oauth_config is None:
        return ScopedAuth(mcp_app, tokens, readonly_tool_names(server))

    from lib.oauth import OAuthGuard, WAOAuthProvider, build_oauth_app

    provider = WAOAuthProvider(oauth_config)
    provider.store.prune()
    guard = OAuthGuard(provider)
    scoped = ScopedAuth(mcp_app, tokens, readonly_tool_names(server), oauth=guard)
    return OAuthRouter(scoped, build_oauth_app(provider), guard.paths, hosts)


if __name__ == "__main__":
    # The pro toolsets (audio, recall) are opt-in so the default stdio
    # surface matches upstream's curated one; the shared server wants
    # everything. Must be set before main is imported.
    os.environ.setdefault("WHATSAPP_MCP_TOOLSETS", "all")

    import uvicorn

    from lib.oauth import OAuthConfig as _OAuthConfig
    from lib.recall import start_periodic_indexing
    from main import mcp

    # 127.0.0.1, not a specific machine's address: this default shipped as a
    # tailnet IP in a public repo, so anyone else running it bare hit a bind
    # failure. Set MCP_HOST to a VPN/tailnet address to share it across hosts.
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8082"))
    # Validated before any background work starts: a bad OAuth config must
    # stop the server, not leave it half up.
    oauth_config = _OAuthConfig.from_env()

    # Optional: transcribe fresh voice notes in the background so recall
    # covers what people said, not just what they typed.
    if os.environ.get("AUTO_TRANSCRIBE_VOICE", "").lower() in {"1", "true", "yes"}:
        from lib.transcribe import start_auto_transcribe

        start_auto_transcribe(int(os.environ.get("AUTO_TRANSCRIBE_INTERVAL", "180")))

    # Keep the recall index warm so first-call results are never partial.
    start_periodic_indexing(int(os.environ.get("RECALL_INDEX_INTERVAL", "600")))

    app = build_app(mcp, host=host, port=port, tokens=static_tokens(), oauth_config=oauth_config)
    uvicorn.run(app, host=host, port=port, log_level="warning")

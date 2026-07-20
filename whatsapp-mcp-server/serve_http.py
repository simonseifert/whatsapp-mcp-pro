"""Shared streamable-HTTP entrypoint for the WhatsApp MCP server.

One always-on instance under launchd (com.simon.whatsapp-mcp) replaces the
per-Claude-session `ssh m1 uv run main.py` stdio spawns. Those leaked: the
stdio server does not exit on stdin EOF, so every finished session orphaned
a python here (82 of them = 44 GB = the 2026-07-03 jetsam storm + panic).

Scoped access: if WA_MCP_FULL_TOKEN / WA_MCP_READONLY_TOKEN are set (.env),
requests need `Authorization: Bearer <token>`. The read-only token can only
call tools whose annotations declare readOnlyHint=true; everything else gets
a JSON-RPC error. With no tokens configured, auth is off (tailnet-only bind
remains the outer wall).

Clients connect via Claude Code MCP config:
    type=http, url=http://100.78.169.70:8082/mcp
    (+ header Authorization: Bearer <token> when tokens are configured)
"""

import json
import os

if __name__ == "__main__":
    # The pro toolsets (audio, recall) are opt-in so the default stdio
    # surface matches upstream's curated one; the shared server wants
    # everything. Must be set before main is imported.
    os.environ.setdefault("WHATSAPP_MCP_TOOLSETS", "all")

    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    from lib.recall import start_periodic_indexing
    from main import mcp

    host = os.environ.get("MCP_HOST", "100.78.169.70")
    port = int(os.environ.get("MCP_PORT", "8082"))

    mcp.settings.host = host
    mcp.settings.port = port
    # The SDK's DNS-rebinding protection only allows localhost Hosts by
    # default; without this, tailnet clients get 421 Misdirected Request.
    mcp.settings.transport_security = TransportSecuritySettings(
        allowed_hosts=[f"{host}:{port}"],
        allowed_origins=[f"http://{host}:{port}"],
    )

    tokens = {}
    if os.environ.get("WA_MCP_FULL_TOKEN"):
        tokens[os.environ["WA_MCP_FULL_TOKEN"]] = "full"
    if os.environ.get("WA_MCP_READONLY_TOKEN"):
        tokens[os.environ["WA_MCP_READONLY_TOKEN"]] = "readonly"

    readonly_tools = {
        t.name for t in mcp._tool_manager.list_tools() if t.annotations is not None and t.annotations.readOnlyHint
    }

    def _filter_tools_payload(payload):
        """Drop non-read-only tools from a tools/list result, in place."""
        try:
            tools = payload["result"]["tools"]
        except (KeyError, TypeError):
            return payload
        payload["result"]["tools"] = [
            t for t in tools if t.get("name") in readonly_tools
        ]
        return payload

    class ScopedAuth:
        """Pure ASGI middleware: bearer-token auth + read-only tool gating."""

        def __init__(self, app):
            self.app = app

        def _filtered_list_sender(self, send):
            """Wrap `send` so a tools/list response is filtered before it leaves.

            The whole response is buffered because content-length must be
            rewritten, and a streamed body cannot be edited after its header
            has gone out.
            """
            state = {"start": None, "chunks": []}

            async def wrapped(message):
                if message["type"] == "http.response.start":
                    state["start"] = message
                    return
                if message["type"] != "http.response.body":
                    return await send(message)
                state["chunks"].append(message.get("body", b""))
                if message.get("more_body"):
                    return
                raw = b"".join(state["chunks"])
                out = raw
                try:
                    text = raw.decode("utf-8")
                    if text.lstrip().startswith("{"):
                        out = json.dumps(_filter_tools_payload(json.loads(text))).encode()
                    else:  # SSE frames: rewrite each `data:` line, keep the rest
                        lines = []
                        for line in text.split("\n"):
                            if line.startswith("data:"):
                                payload = json.loads(line[5:].strip())
                                lines.append("data: " + json.dumps(
                                    _filter_tools_payload(payload)))
                            else:
                                lines.append(line)
                        out = "\n".join(lines).encode()
                except Exception:
                    out = raw  # never break the response over cosmetics
                start = state["start"] or {"type": "http.response.start",
                                           "status": 200, "headers": []}
                headers = [(k, v) for k, v in start.get("headers", [])
                           if k.decode().lower() != "content-length"]
                headers.append((b"content-length", str(len(out)).encode()))
                await send({**start, "headers": headers})
                await send({"type": "http.response.body", "body": out,
                            "more_body": False})

            return wrapped

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http" or not tokens:
                return await self.app(scope, receive, send)
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            token = headers.get("authorization", "").removeprefix("Bearer ").strip()
            role = tokens.get(token)
            if role is None:
                return await self._reply(send, 401, {"error": "unauthorized"})
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
            if (
                isinstance(rpc, dict)
                and rpc.get("method") == "tools/call"
                and rpc.get("params", {}).get("name") not in readonly_tools
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
        async def _reply(send, status, payload):
            data = json.dumps(payload).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": data})

    # Optional: transcribe fresh voice notes in the background so recall
    # covers what people said, not just what they typed.
    if os.environ.get("AUTO_TRANSCRIBE_VOICE", "").lower() in {"1", "true", "yes"}:
        from lib.transcribe import start_auto_transcribe

        start_auto_transcribe(int(os.environ.get("AUTO_TRANSCRIBE_INTERVAL", "180")))

    # Keep the recall index warm so first-call results are never partial.
    start_periodic_indexing(int(os.environ.get("RECALL_INDEX_INTERVAL", "600")))

    app = ScopedAuth(mcp.streamable_http_app())
    uvicorn.run(app, host=host, port=port, log_level="warning")

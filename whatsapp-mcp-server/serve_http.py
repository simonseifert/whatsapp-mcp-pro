"""Shared streamable-HTTP entrypoint for the WhatsApp MCP server.

One always-on instance under launchd (com.simon.whatsapp-mcp) replaces the
per-Claude-session `ssh m1 uv run main.py` stdio spawns. Those leaked: the
stdio server does not exit on stdin EOF, so every finished session orphaned
a python here (82 of them = 44 GB = the 2026-07-03 jetsam storm + panic).

Clients connect via Claude Code MCP config:
    type=http, url=http://100.78.169.70:8082/mcp
"""

import os

if __name__ == "__main__":
    # The pro toolsets (audio, recall) are opt-in so the default stdio
    # surface matches upstream's curated one; the shared server wants
    # everything. Must be set before main is imported.
    os.environ.setdefault("WHATSAPP_MCP_TOOLSETS", "all")

    from mcp.server.transport_security import TransportSecuritySettings

    from lib.recall import start_periodic_indexing
    from main import mcp

    # Bind to the Tailscale IP only: reachable from the tailnet, not the LAN.
    # If Tailscale isn't up yet at boot the bind fails and launchd retries.
    mcp.settings.host = os.environ.get("MCP_HOST", "100.78.169.70")
    mcp.settings.port = int(os.environ.get("MCP_PORT", "8082"))
    # The SDK's DNS-rebinding protection only allows localhost Hosts by
    # default; without this, tailnet clients get 421 Misdirected Request.
    mcp.settings.transport_security = TransportSecuritySettings(
        allowed_hosts=[f"{mcp.settings.host}:{mcp.settings.port}"],
        allowed_origins=[f"http://{mcp.settings.host}:{mcp.settings.port}"],
    )
    # Keep the recall index warm so first-call results are never partial.
    start_periodic_indexing(int(os.environ.get("RECALL_INDEX_INTERVAL", "600")))
    mcp.run(transport="streamable-http")

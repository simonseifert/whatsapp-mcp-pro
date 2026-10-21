# Security Policy

## Reporting a vulnerability

Please report privately through GitHub's
[private vulnerability reporting](https://github.com/simonseifert/whatsapp-mcp-pro/security/advisories/new)
for `simonseifert/whatsapp-mcp-pro`. Do not open a public issue for anything
that could be exploited.

Include what is affected (bridge, MCP server, one of the optional interfaces),
the configuration it needs, reproduction steps and the impact. This is a
one-maintainer project, so expect a reply on a best-effort basis; a fix is
published before the details are.

If the issue is also present in upstream
[FelixIsaac/whatsapp-mcp-extended](https://github.com/FelixIsaac/whatsapp-mcp-extended),
say so. It will be passed on to upstream once it is fixed here, or you can
report it there directly.

## Supported versions

Only the latest `main` and the most recent release (currently `v0.4.0-pro`)
get security fixes. Please reproduce on one of those before reporting.

## Threat model

This is built for **one person on their own machine**: a trusted host, a
single WhatsApp account, and AI agents that person runs. It is not designed for
multi-tenant use or for exposure to the public internet.

### What the defaults protect

| Layer | Control | Setting |
|---|---|---|
| Bridge REST | Binds to loopback only | `API_BIND_HOST=127.0.0.1` (a VPN/tailnet address to reach it from another machine, never a public interface) |
| Bridge REST | Every endpoint needs `X-API-Key`; the bridge refuses to start without a key | `API_KEY` (`DISABLE_AUTH_CHECK` is for development only) |
| Shared MCP HTTP server | Binds to loopback only | `MCP_HOST=127.0.0.1`, `MCP_PORT=8082` |
| Shared MCP HTTP server | Scoped bearer tokens. The read-only token can call only tools annotated `readOnlyHint=true`, enforced server-side. With neither token set, auth is **off** and the bind address is the only wall. | `WA_MCP_FULL_TOKEN`, `WA_MCP_READONLY_TOKEN` |
| Sending | Two **independent** recipient allowlists at different layers. They are not aliases; setting one leaves the other open. Matching is exact, never substring or prefix. Unset means no restriction. | `SEND_ALLOWED_JIDS` (HTTP API layer), `WHATSAPP_ALLOWLIST_JIDS` (whatsmeow send call; alias `WHATSAPP_JID_ALLOWLIST`) |
| Media guard | Local paths for `send_file` / `send_audio_message` are confined to built-in roots plus an explicit list. `..` is always rejected. Both the bridge and the MCP server check, and both refuse secret and database files (`*.env`, `*.pem`, `*.key`, `id_rsa*`, `*.db` and its sidecars, the top level of any `store/`) and hidden path segments. | `MEDIA_ALLOWED_DIRS` (`DISABLE_PATH_CHECK` is a dev escape hatch) |
| Webhooks and media download | Private and loopback targets are blocked (SSRF guard), and media CDN hosts are validated. Specific private targets can be allowed one by one. | `WEBHOOK_ALLOWED_ADDRS` (prefer this to `DISABLE_SSRF_CHECK`) |
| Rate limiting | `X-Forwarded-For` is ignored unless you trust the proxy in front | `TRUST_PROXY_HEADERS=false` |

Session keys and message history live in the bridge's `store/`, outside the
repository in a code/data split install. Never commit a `store/` or a `.env`.

### Out of scope

- **Prompt injection through incoming messages.** Anyone who can message you
  can write text that reaches your agent. Mitigate at the agent layer: use the
  read-only MCP token where sending isn't needed, set both send allowlists,
  and require a human to approve outbound actions (wa-dispatch only drafts, for
  this reason).
- **Malicious media.** Downloaded files land on the local disk unscanned.
- **Account bans.** This uses an unofficial client of the WhatsApp Web
  protocol. The optional anti-ban pacing (`ANTIBAN_ENABLED`) reduces the risk
  but does not remove it. Do not use it for bulk or unsolicited messaging.
- **Anyone with access to your user account on the host.** They can read the
  store and `.env` directly.

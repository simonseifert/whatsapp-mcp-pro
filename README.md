# whatsapp-mcp-pro

**AI-augmented WhatsApp for Claude and MCP agents — with semantic memory, local voice transcription, a shared multi-session server, and a self-hosted web client that costs zero extra device slots.**

Forked from [FelixIsaac/whatsapp-mcp-extended](https://github.com/FelixIsaac/whatsapp-mcp-extended) (itself descended from [lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp)), tracking upstream closely and adding a "pro" layer on top.

## Why this fork exists

Every WhatsApp MCP gives you send/read tools. This one also answers questions like *"what did we agree about the deadline six weeks ago?"* — across your full message history, in any language, including what was said in voice notes.

| Pro feature | What it does |
|---|---|
| **`recall`** — semantic search | Multilingual embedding search over your full message history (paraphrase-multilingual-MiniLM, 50+ languages). Vector store lives inside the bridge's SQLite; background indexer keeps it warm; the model auto-unloads after 15 min idle so a small always-on box stays small. |
| **`transcribe_audio`** — local voice-note transcription | mlx-whisper (large-v3-turbo) on Apple Silicon. No audio leaves your machine. |
| **Shared HTTP server** (`serve_http.py`) | One always-on streamable-HTTP MCP serving *all* your Claude sessions. Kills the per-session stdio-spawn pattern that leaks orphaned processes (we learned this the hard way: 82 orphans, 44 GB RAM, one kernel panic). |
| **Scoped bearer tokens** | A full token for trusted agents, a read-only token for dashboards/automations. Read-only can only call tools annotated `readOnlyHint=true` — enforced server-side. |
| **`wa-client/`** — self-hosted web client | A WhatsApp-Web-style chat UI that rides the bridge's device session. **Zero additional linked-device slots**, unlimited browsers. Real-time push (webhook → SSE), inline media, keyword + semantic search, file sending, **scheduled messages**. |
| **Send allowlist** | `SEND_ALLOWED_JIDS` limits which chats the bridge will ever send to. Safety gate for automation. |

Everything upstream ships is here too: 27 curated tools (31 with the pro toolsets), toolset gating, HMAC-signed webhooks with trigger filters, group management, polls, newsletters, presence, opt-in anti-ban protection with humanized send pacing, auto-download of media before CDN links expire.

## Architecture

```
                     ┌────────────────────────────────────────────┐
                     │                 your machine               │
 WhatsApp servers ◄──┤ whatsapp-bridge (Go, whatsmeow)            │
                     │   REST :8080 · SQLite store · webhooks     │
                     │      ▲                ▲                    │
                     │      │ REST           │ REST + webhook     │
                     │ whatsapp-mcp-server   wa-client            │
                     │  (Python FastMCP)      (chat web UI :8084) │
                     │   stdio  or  :8082     SSE push, schedule  │
                     └──────┬─────────────────────────────────────┘
                            │ streamable HTTP (+ bearer token)
                  Claude Code / Desktop / any MCP client, N sessions
```

- **whatsapp-bridge** — Go daemon on [whatsmeow](https://github.com/tulir/whatsmeow). Pairs as a linked device (QR once), stores history in SQLite, exposes REST + webhooks. Binds 127.0.0.1 by default.
- **whatsapp-mcp-server** — Python FastMCP. Run per-client via stdio, or (recommended) as the shared HTTP server.
- **wa-client** — the chat web UI. Optional but excellent.
- **whatsapp-web-ui** — upstream's Next.js admin panel (pairing, webhook management). Not a chat client.

## Quickstart

```bash
# 1. Bridge: build and pair (QR in terminal, scan with your phone)
cd whatsapp-bridge && go build -o whatsapp-bridge . && ./whatsapp-bridge

# 2. MCP server deps
cd ../whatsapp-mcp-server && uv sync            # + `uv sync --extra pro` for recall/transcription

# 3a. Claude Code, per-session stdio (simple)
claude mcp add whatsapp -- uv --directory /path/to/whatsapp-mcp-server run main.py

# 3b. OR the shared HTTP server (one process, many sessions)
MCP_HOST=0.0.0.0 MCP_PORT=8082 .venv/bin/python serve_http.py
claude mcp add -t http whatsapp http://<host>:8082/mcp \
  --header "Authorization: Bearer <WA_MCP_FULL_TOKEN>"

# 4. Optional: the web client
WA_WEB_HOST=<host> .venv/bin/python ../wa-client/app.py   # then open http://<host>:8084
```

Pro toolsets are opt-in: set `WHATSAPP_MCP_TOOLSETS=all` (the shared server does this automatically). See `.env.example` for bridge options (`API_KEY`, `ANTIBAN_*`, `SEND_ALLOWED_JIDS`, `DISABLE_SSRF_CHECK` for localhost webhooks).

## wa-client: unlimited "WhatsApp Web", one device slot

WhatsApp caps you at 4 linked devices. The bridge takes one slot — and `wa-client` rides that same session, so every browser/phone/tablet you open it in is free. It reads history from the bridge's SQLite directly and sends through the bridge API, which also means full history from day one and search WhatsApp itself doesn't have (semantic, via `recall`).

Features: real-time push (bridge webhook → SSE, no polling), inline media, file upload, group sender colors, keyword + AI search, mark-read, and **scheduled sends** (⏰ in the composer; a queue on the server delivers even with no browser open).

Deliberate limitations: no calls, no status posting (also a documented ban trigger — see below), media older than ~2 weeks may be expired upstream (mitigated by the bridge's auto-download-on-receipt).

**Security note:** wa-client has no login of its own. Bind it to 127.0.0.1 (default) or a VPN/tailnet address only. Never expose it to the internet.

## Transcription backends and integrations

Transcription is backend-pluggable (`WHISPER_BACKEND`, default `auto`):

| Backend | Where it runs | Notes |
|---|---|---|
| `mlx` | Apple Silicon, fully local | mlx-whisper large-v3-turbo (~1.5 GB RAM) |
| `faster-whisper` | any OS, CPU/GPU, fully local | `uv sync --extra pro-cpu` |
| `groq` | Groq API | needs `GROQ_API_KEY`; near-zero RAM — ideal for small always-on boxes |

Set `AUTO_TRANSCRIBE_VOICE=true` on the shared server and incoming voice notes
are transcribed in the background and written into the message store — voice
becomes searchable via `recall` and readable in wa-client. LLM integrations
beyond speech (digests, summaries via Groq/DeepSeek/local models) are on the
[roadmap](ROADMAP.md).

The bridge (Go) and server (Python) run on macOS, Linux, and Windows; service
examples in `scripts/` are macOS launchd, but any process manager works
(systemd, NSSM). The only Apple-only piece is the optional mlx backend.

## Account safety, honestly

This uses WhatsApp's linked-device protocol via whatsmeow, same as Baileys and other protocol-level libraries (browser-automation tools like whatsapp-web.js carry a related but architecturally distinct risk surface: a real Chromium instance fingerprints differently than a custom protocol client). All of it violates WhatsApp's ToS and carries real ban risk; 2025-26 saw ban waves that hit even low-volume personal bots. Mitigations shipped here: opt-in anti-ban pacing (randomized delays + typing simulation), presence-ping hygiene, a send allowlist, and no support for the known high-risk behaviors (status posting from servers, cold bulk sends). Community consensus applies: prefer a dedicated number, reply-heavy usage on existing chats, residential IP. **No unofficial client is safe, only quiet.**

## Versioning / upstream relationship

`main` tracks upstream's `main` plus the pro layer. Pro features are offered upstream as PRs ([#55](https://github.com/FelixIsaac/whatsapp-mcp-extended/pull/55) transcription, [#56](https://github.com/FelixIsaac/whatsapp-mcp-extended/pull/56) recall). Keep your bridge's whatsmeow fresh — stale builds eventually die with "405 Client outdated" (this is how the original repo's installs broke).

See [ROADMAP.md](ROADMAP.md) for where this is going.

## Credits

- [FelixIsaac/whatsapp-mcp-extended](https://github.com/FelixIsaac/whatsapp-mcp-extended) — the actively maintained base this forks
- [lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp) — the original
- [tulir/whatsmeow](https://github.com/tulir/whatsmeow) — the engine underneath everything

MIT, same as upstream.

# whatsapp-mcp-pro

**Your WhatsApp, turned into something you can program.** Read and send from any
Claude session, browse it in your own web client, hold a conversation with Claude
Code by texting yourself, or have incoming messages start work before you sit
down.

Runs entirely on your own machine. Nothing goes to a third party.

### Four ways to use it

| | |
|---|---|
| **Ask** | 32 MCP tools in any Claude session — read, send, search, media, groups, polls |
| **Browse** | [`wa-client`](#wa-client-unlimited-whatsapp-web-one-device-slot) — a WhatsApp-Web-style UI that costs **zero** extra linked-device slots |
| **Talk to it** | [`wa-assistant`](#wa-assistant-text-yourself-get-claude-code-back) — message your own chat, Claude Code answers. Voice notes both ways |
| **Have it act** | [`wa-dispatch`](#wa-dispatch-incoming-messages-start-the-work-optional) — a client's message opens a Claude session in that project and drafts the reply |

The last two are the ones nothing else does: your phone becomes a terminal, and
work starts when the message arrives rather than when you remember to look.

### Get started

| | |
|---|---|
| **Just want it working** | Paste this repo's URL into Claude and say *"set this up for me"* — [SETUP.md](SETUP.md) is written for exactly that |
| **Prefer doing it yourself** | [SETUP.md](SETUP.md), or the [Quickstart](#quickstart) below |
| **Curious first** | Keep reading |

Forked from [FelixIsaac/whatsapp-mcp-extended](https://github.com/FelixIsaac/whatsapp-mcp-extended) (itself descended from [lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp)), tracking upstream closely and adding a "pro" layer on top.

Upstream deliberately keeps ML out of its core — its [ARCHITECTURE.md](https://github.com/FelixIsaac/whatsapp-mcp-extended/blob/main/ARCHITECTURE.md) calls the transcription and vector-search work "heavy ML bloat" for a transport layer, and points here for it. That is the division of labour: upstream stays a lean, rock-solid transport primitive; this fork is the batteries-included build. Transcription and recall were contributed upstream from here ([#55](https://github.com/FelixIsaac/whatsapp-mcp-extended/pull/55), [#56](https://github.com/FelixIsaac/whatsapp-mcp-extended/pull/56)) and live in both.

## What the pro layer adds

Every WhatsApp MCP gives you send/read tools. These are the parts that aren't
standard, grouped by what they're for.

**Getting answers out of your history**

| | |
|---|---|
| **`recall`** — semantic search | Multilingual embedding search over your full history (paraphrase-multilingual-MiniLM, 50+ languages), so *"what did we agree about the deadline six weeks ago?"* works without remembering the words used. Vector store lives inside the bridge's SQLite; a background indexer keeps it current. Pinned to CPU and kept resident (~700 MB steady) — on Apple Silicon the GPU path pinned a gigabyte of Metal buffers it never gave back. |
| **`transcribe_audio`** — voice notes | Local transcription (mlx-whisper large-v3-turbo on Apple Silicon, faster-whisper anywhere, or Groq). With `AUTO_TRANSCRIBE_VOICE` the backlog is transcribed in the background, so speech becomes searchable too. |
| **`check_inbox`** | "What arrived since I last asked" — how a Claude Desktop session keeps up, since unlike a terminal it can't be interrupted by an incoming message. |

**Interfaces**

| | |
|---|---|
| **[`wa-client/`](wa-client)** — self-hosted web client | WhatsApp-Web-style chat UI riding the bridge's device session. **Zero additional linked-device slots**, unlimited browsers. Real-time push (webhook → SSE), inline media, keyword + semantic search, file sending, **scheduled messages**. |
| **[`wa-assistant/`](wa-assistant)** — text yourself | Message your own chat and a persistent Claude Code session answers. Voice note in → voice note out, screenshots filed automatically, long questions routed to a stronger model. Your phone becomes a terminal. |
| **[`wa-dispatch/`](wa-dispatch)** — messages start the work | A message from a routed chat opens or wakes a Claude Code session in that client's project, which reads the thread, researches and drafts a reply — then stops. It never sends. macOS + tmux. |

**Running it seriously**

| | |
|---|---|
| **Shared HTTP server** (`serve_http.py`) | One always-on streamable-HTTP MCP serving *all* your Claude sessions. Kills the per-session stdio-spawn pattern that leaks orphaned processes (we learned this the hard way: 82 orphans, 44 GB RAM, one kernel panic). |
| **Scoped bearer tokens** | A full token for trusted agents, a read-only token for dashboards/automations. Read-only can only call tools annotated `readOnlyHint=true` — enforced server-side. |
| **Send allowlist** | `WHATSAPP_ALLOWLIST_JIDS` (upstream) / `SEND_ALLOWED_JIDS` (ours, still honoured) limits which chats the bridge will ever send to. Safety gate for automation. |
| **Anti-ban pacing** | Opt-in humanized send delays and typing simulation. See [Account safety](#account-safety-honestly) — it is a real risk, honestly described. |

Everything upstream ships is here too: toolset gating, HMAC-signed webhooks with
trigger filters, group management, polls, newsletters, presence, auto-download of
media before CDN links expire.

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

## Install

**[SETUP.md](SETUP.md) is the full walkthrough** — prerequisites, pairing,
wiring up Claude Desktop or Claude Code, and troubleshooting. It is written so
an AI agent can follow it and do the setup for you; paste the repo link into
Claude and ask it to set this up.

The short version, if you know your way around:

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

### Toolsets

`recall` (semantic search), `audio` (transcription) and `inbox` are opt-in via
`WHATSAPP_MCP_TOOLSETS`. `inbox` adds `check_inbox` — "what arrived since I
last asked" — which is how a Claude Desktop session keeps up with messages,
since unlike a terminal session it cannot be interrupted by one. The first call
returns nothing and pins a cursor to now, so asking once never dumps your whole
history into the conversation.

Pro toolsets are opt-in: set `WHATSAPP_MCP_TOOLSETS=all` (the shared server does this automatically). See `.env.example` for bridge options (`API_KEY`, `API_BIND_HOST`, `PRESENCE_PING_ENABLED`, `DISABLE_SSRF_CHECK` for localhost webhooks). Anti-ban (`ANTIBAN_ENABLED`, off by default) and the send allowlist (`SEND_ALLOWED_JIDS`) are set the same way.

## wa-client: unlimited "WhatsApp Web", one device slot

WhatsApp caps you at 4 linked devices. The bridge takes one slot — and `wa-client` rides that same session, so every browser/phone/tablet you open it in is free. It reads history from the bridge's SQLite directly and sends through the bridge API, which also means full history from day one and search WhatsApp itself doesn't have (semantic, via `recall`).

Features: real-time push (bridge webhook → SSE, no polling), inline media, file upload, group sender colors, keyword + AI search, mark-read, and **scheduled sends** (⏰ in the composer; a queue on the server delivers even with no browser open).

Deliberate limitations: no calls, no status posting (also a documented ban trigger — see below), media older than ~2 weeks may be expired upstream (mitigated by the bridge's auto-download-on-receipt).

**Security note:** wa-client has no login of its own. Bind it to 127.0.0.1 (default) or a VPN/tailnet address only. Never expose it to the internet.

## wa-assistant: text yourself, get Claude Code back

Type into your own Note-to-Self chat and a persistent Claude Code session
replies. Send a voice note, get a voice note back. Send a screenshot, have it
read and filed. Ask something big and it hands off to a stronger model in a
separate process, so a ten-minute research task never blocks *"what time is my
meeting"*.

The session is kept **warm** deliberately: a cold `claude -p` per message costs
seconds of startup and forgets the thread, so follow-up questions stop working.

Only messages *you* send in *your own* chat are ever read
(`is_from_me=1 AND chat_jid IN (your jids)`), and it only ever replies to you.
Both of your identities — the phone-number jid and the LID — are detected
automatically, which matters because **which one your self-chat is filed under
differs per linked device**; watching only the one the API reports finds nothing
at all on some installs.

Speech in and out are off by default and configured as shell commands rather
than Python dependencies, so they can be a local model, a hosted API, or the
`say` binary macOS already has.

```bash
cd wa-assistant && cp config.example.env config.env
./wa-assistant.py --check     # validates setup, sends nothing
./wa-assistant.py --once      # handle one message and exit
```

See [wa-assistant/README.md](wa-assistant/README.md), and read its Security
section before enabling — the session runs without permission prompts.

## wa-dispatch: incoming messages start the work (optional)

Everything above is pull — you ask, Claude answers. `wa-dispatch/` inverts it:
a message from a chat you've routed **opens or wakes a Claude Code session in
that client's project directory**, which reads the thread, researches, drafts a
reply and stages fixes on a local branch — then stops.

It never sends and never pushes. You review, and can approve a drafted reply
from your phone. Meeting transcripts (via Fathom) are split per client and
delivered the same way.

Incoming messages are untrusted input, so sessions the dispatcher launches run
behind a `PreToolUse` hook that blocks every outbound and destructive tool
before it executes — sends, pushes, deploys, `rm -rf`. Sessions you start
yourself are untouched by it.

Requires macOS, tmux and Claude Code. See [wa-dispatch/README.md](wa-dispatch/README.md).

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

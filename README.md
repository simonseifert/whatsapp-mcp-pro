# whatsapp-mcp-pro

Self-hosted WhatsApp for AI agents: an MCP server with semantic search across your
whole history, local voice-note transcription, and the safety rails you need
before you point automation at a real account.

[![Go Tests](https://img.shields.io/github/actions/workflow/status/simonseifert/whatsapp-mcp-pro/go-test.yml?branch=main&label=Go%20Tests)](https://github.com/simonseifert/whatsapp-mcp-pro/actions/workflows/go-test.yml)
[![Lint](https://img.shields.io/github/actions/workflow/status/simonseifert/whatsapp-mcp-pro/lint.yml?branch=main&label=Lint)](https://github.com/simonseifert/whatsapp-mcp-pro/actions/workflows/lint.yml)
[![Python Tests](https://img.shields.io/github/actions/workflow/status/simonseifert/whatsapp-mcp-pro/python-test.yml?branch=main&label=Python%20Tests)](https://github.com/simonseifert/whatsapp-mcp-pro/actions/workflows/python-test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/simonseifert/whatsapp-mcp-pro?include_prereleases&sort=semver)](https://github.com/simonseifert/whatsapp-mcp-pro/releases)
[![Go](https://img.shields.io/github/go-mod/go-version/simonseifert/whatsapp-mcp-pro?filename=whatsapp-bridge%2Fgo.mod)](whatsapp-bridge/go.mod)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](whatsapp-mcp-server/pyproject.toml)

## What it is

A Go bridge pairs with your WhatsApp account as a linked device (the same way
WhatsApp Web does), stores your history in SQLite on your machine, and exposes a
local REST API. A Python MCP server sits on top and gives any MCP client (Claude
Code, Claude Desktop, others) 39 tools to read, search, send, and manage chats.
Nothing goes to a third party unless you choose a hosted transcription backend.

It is a fork of [FelixIsaac/whatsapp-mcp-extended](https://github.com/FelixIsaac/whatsapp-mcp-extended)
that adds a "pro" layer: recall, transcription, an identity directory, a shared
HTTP server with scoped tokens, and hardening from running it daily. See
[Credits](#credits-and-upstream) for how the two relate.

![Semantic recall demo: English questions find a Croatian deadline and a German price](docs/assets/demo-recall.gif)

*Asking in English finds the Croatian deadline and the German price: recall matches meaning, not words. Demo data is fictional.*

- **Set it up:** [Quickstart](#quickstart) below, or [SETUP.md](SETUP.md) for the
  full walkthrough. SETUP.md is written so an AI agent can follow it for you.
- **What changed:** [CHANGELOG.md](CHANGELOG.md). **Where it's going:** [ROADMAP.md](ROADMAP.md).

## Account safety

Read this before you pair a number you care about.

- **This is an unofficial client.** It speaks WhatsApp's linked-device protocol
  through [whatsmeow](https://github.com/tulir/whatsmeow), like every other
  self-hosted WhatsApp bridge. Using it breaks WhatsApp's terms of service, and
  accounts do get banned. 2025-26 saw ban waves that hit low-volume personal bots
  too. No unofficial client is safe, only quiet.
- **It takes one linked-device slot** (WhatsApp allows four). Your phone stays
  the primary device and keeps working normally.
- **What gets accounts banned:** first contact with people who don't have your
  number, bulk or templated sends, high send rates from a fresh number, and
  posting status from a server. Replying inside existing conversations is the
  low-risk pattern.
- **What this repo does about it:**
  - Two send allowlists (`SEND_ALLOWED_JIDS` at the HTTP layer,
    `WHATSAPP_ALLOWLIST_JIDS` at the send call), covering messages, reactions,
    polls, edits and deletes. Matching is exact, never substring. Set both
    before you let an agent send.
  - After WhatsApp's error 463 (temporarily barred from starting new chats),
    the bridge refuses first-contact sends until the lock lifts, instead of
    retrying into a ban.
  - Opt-in pacing (`ANTIBAN_ENABLED=true`): randomised delays, typing
    simulation, a warm-up cap for new numbers, a risk score that pauses sending.
    A corrupt warm-up file restarts warm-up at day 1 instead of switching it off.
  - Send caps, on by default: per chat (15/min, 60/h), overall (120/h), and 10
    first contacts per 24h, with a separate looser budget for reactions, edits
    and deletes. Over a cap the bridge answers 429 with a retry time instead of
    sending. All `ANTIBAN_*` knobs are in [SETUP.md](SETUP.md) and `.env.example`.
  - Presence stays offline by default and only goes online briefly around real
    sends, so your phone keeps its push notifications.
  - No status posting and no bulk-send tools, on purpose.
- **Advice:** if you're building something that sends a lot, use a dedicated
  number. Keep your own number for reading, recall and replies.

## Status

| Component | Status | What it is |
|---|---|---|
| [`whatsapp-bridge/`](whatsapp-bridge) | Stable, in daily use | Go daemon on whatsmeow. Linked device, SQLite store, REST API, HMAC-signed webhooks. |
| [`whatsapp-mcp-server/`](whatsapp-mcp-server) | Stable, in daily use | Python MCP server (mcp 2.x). stdio per session, or one shared HTTP server. |
| [`wa-client/`](wa-client) | Retired, unmaintained | Chat web UI that rode the bridge's session. Its `app.py` ignores `WA_STORE_PATH`, so it shows stale data once the store lives outside the repo. Has no login. |
| [`wa-assistant/`](wa-assistant) | Experimental, dormant | Text your own chat, a persistent Claude Code session replies. |
| [`wa-dispatch/`](wa-dispatch) | Experimental, dormant | An incoming message opens a Claude Code session in that client's project and drafts a reply. macOS + tmux. |
| [`whatsapp-web-ui/`](whatsapp-web-ui) | Upstream | Upstream's Next.js admin panel (pairing, webhooks). Not a chat client. |

## What the pro layer adds

Every WhatsApp MCP can send and read. These are the parts that aren't standard.

**Finding things in your history**

- **`recall`, semantic search.** Multilingual embeddings
  (paraphrase-multilingual-MiniLM, 50+ languages) over your full history, so
  *"what did we agree about the deadline six weeks ago?"* works without
  remembering the exact words, or the language they were in. Vectors live in the
  bridge's SQLite and a background indexer keeps them current. Runs on CPU and
  stays resident (about 700 MB), because on Apple Silicon the GPU path pinned a
  gigabyte of Metal buffers it never released.
- **`transcribe_audio`, voice notes.** Local transcription. With
  `AUTO_TRANSCRIBE_VOICE=true` incoming voice notes are transcribed in the
  background and written into the store, so speech is searchable by `recall`
  too. `WHISPER_BACKEND=auto` tries, in order:

  | Backend | Runs where | Notes |
  |---|---|---|
  | `whisper-server` | a whisper.cpp server at `WHISPER_SERVER_URL` | Fully local; good for offloading to a GPU box |
  | `mlx` | Apple Silicon | Fully local, mlx-whisper large-v3-turbo, about 1.5 GB RAM |
  | `faster-whisper` | any OS, CPU or GPU | Fully local, `uv sync --extra pro-cpu` |
  | `groq` | Groq's API | Needs `GROQ_API_KEY`; audio leaves the machine; near-zero RAM |

- **`check_inbox`.** "What arrived since I last asked." Claude Desktop can't be
  interrupted by an incoming message, so it asks instead. The first call returns
  nothing and pins a cursor to now, so one question never dumps your whole
  history into the conversation.

**Knowing who is who**

- **Identity directory.** WhatsApp files the same person under a phone JID
  (`…@s.whatsapp.net`) on some devices and a hidden LID (`…@lid`) on others, and
  its contact store keeps a separate row (often with a different name) for each.
  The bridge builds an `identities` table that joins them: one row per person,
  group, newsletter or broadcast list, with both JIDs, the phone number, every
  known name, and activity stats. Exposed as `lookup_identity` (accepts a JID,
  LID, phone number or name), `search_directory`, and the `whatsapp://directory`
  resource.
- **Naming unknown numbers.** `list_unnamed_contacts` ranks active people who
  only show up as a number (no address-book name or nickname) and returns their
  last few messages and shared groups, so you (via an agent) can say who they
  are; `set_contact_names` stores them as local nicknames (up to 50 per call,
  never sent to WhatsApp) and they show up at once in `list_chats`,
  `list_messages` and the directory. A stranger's own "this is your bank" is
  never taken as evidence: names you didn't confirm are stored as
  `agent_inferred`, shown as "(unverified)", can't replace a name you set or
  one from your address book, and are left out of the bridge's directory and
  webhook payloads.
- **LID chat merging.** A conversation split across a LID and a phone JID is
  folded into one chat (`MERGE_LID_CHATS=true`, the default), so search, recall
  and per-chat automation see all of it. Merged rows are kept and marked, so an
  old LID still resolves.

**Agents and remote access**

- **Shared HTTP server** (`serve_http.py`). One always-on streamable-HTTP MCP
  for all your sessions, instead of one stdio process per session. Per-session
  spawning leaked orphaned processes here: 82 of them, 44 GB of RAM, one kernel
  panic.
- **Scoped bearer tokens.** `WA_MCP_FULL_TOKEN` for trusted agents,
  `WA_MCP_READONLY_TOKEN` for dashboards. The read-only token can only list and
  call tools annotated `readOnlyHint=true`, enforced server-side.
- **OAuth for claude.ai and ChatGPT connectors** (`MCP_OAUTH_ENABLED=true`, off
  by default). Those connectors can't send a static header, so the shared
  server can act as its own single-user OAuth 2.1 server: PKCE, dynamic client
  registration, a consent page guarded by a passphrase, and a CLI approve path
  so failed guesses can never lock you out. It needs a public HTTPS URL, which
  puts your account on the internet behind that passphrase; grant read-only
  unless the connector has to send. See
  [SETUP.md](SETUP.md#remote-connector-via-oauth-optional).

**Safety**

- **Allowlists on everything that writes.** `SEND_ALLOWED_JIDS` and
  `WHATSAPP_ALLOWLIST_JIDS` gate reactions, polls, edits and deletes as well as
  messages, so an agent confined to one chat can't act in another.
- **Reachout timelock.** When WhatsApp answers a send with error 463, the
  account is temporarily barred from starting new chats, and pushing more
  first contacts through is how a lock becomes a ban. The bridge remembers the
  lock (WhatsApp's own end time when it sends one, else
  `REACHOUT_TIMELOCK_DEFAULT`, 24h), refuses sends to contacts with no history
  until then with 429 and `Retry-After`, and keeps replies and group messages
  working. `/api/connection` shows `reachout_timelock_until`.
- **Media guard.** `send_file` and `send_audio_message` read local paths only
  from `/tmp` and directories you list in `MEDIA_ALLOWED_DIRS`. Inside those it
  still refuses dotfiles and anything under a dot-directory, `.env`, `.pem`,
  `.key`, SSH keys and credential files, any SQLite database and its WAL/SHM
  sidecars, and files at the top level of a `store/` directory (where the session
  keys live). Both the bridge and the MCP server enforce this, and symlinks are
  resolved first. An agent reading untrusted message text can be prompt-injected;
  this is the last stop before a secret ends up in a chat.

**Reliability**

- **Sends are safe to retry.** `/api/send`, `/api/reaction` and
  `/api/poll/create` take an idempotency key (`idempotency_key` or an
  `Idempotency-Key` header). A repeat returns the stored result instead of
  sending again; a repeat after a failure reuses the same WhatsApp message ID.
  The MCP send tools use a fresh key per call, retry a timed-out request once
  with it, and return it, so an agent can retry a whole call without a double
  send. Failures come back as distinct codes (400/403/409/422/429/502/503/504)
  rather than a blanket 500 that invites blind retries.
- **Phone-offline warning.** A linked device stops working about 14 days after
  the phone was last online, and no restart fixes that. The bridge tracks when
  it last saw the phone and reports `phone_last_seen` and `phone_offline_days`
  in `/api/connection`, logs a WARN past 7 days and an ERROR past 12, and after
  `PHONE_OFFLINE_PING_AFTER` (7 days) sends one invisible request a day to your
  own phone to wake it (`PHONE_PING_ENABLED=false` to stop that). Alert on
  `phone_offline_days` in your health check.
- **Media retry** (`MEDIA_RETRY_ENABLED=true`, off by default). When media has
  expired from WhatsApp's CDN (403/404), the bridge asks the sender's phone to
  re-upload it. Requests are queued in the database and worked by a paced
  background loop (`MEDIA_RETRY_MAX_PER_HOUR`, default 20), so they survive a
  restart or an offline phone; a recovered file is saved and served from
  `store/media/`. `GET /api/media-requests` shows the queue. Each retry shows
  "Finished syncing" on your phone, which is why it is paced and opt-in. It
  only recovers media the phone still has.
- **One bridge per number.** To run a second number, run a second bridge process
  with its own data directory (its own `store/`), its own `API_PORT`, and its own
  service unit. The MCP server talks to one bridge (`BRIDGE_HOST`); run a second
  MCP server if you need tools on the second number.
- **Clean exit on "Stream replaced".** If another process takes over the session
  (usually a second copy of the same bridge), the bridge exits with code 3.
  Set `RestartPreventExitStatus=3` in systemd so the two copies don't keep
  kicking each other off. See the [systemd example](SETUP.md#7-keep-it-running).
- **Honest health check.** A bridge can be alive, bound to its port and linked
  while ingesting nothing; a cold boot before DNS works used to leave it that
  way. Check `connected` from `GET /api/connection` (or `/api/health`, which
  returns 503 when it is false), not the process or the port.
- **Deploy with rollback.** [`scripts/deploy-live.sh`](scripts/deploy-live.sh)
  builds to a side file, runs the Go and Python tests, backs up the store
  online, swaps the binary, restarts, and rolls back to the previous binary if any
  bridge doesn't report `connected` within two minutes. It is written for a
  systemd layout with code and data in separate directories; set `WA_DATA_DIR`
  and `OTHER_BRIDGES` for yours.

Everything upstream ships is here too: toolset gating, HMAC-signed webhooks with
trigger filters, group management, polls, newsletters, presence control, and
auto-download of media on receipt.

## Architecture

```mermaid
flowchart LR
    WA[WhatsApp servers]
    subgraph host[Your machine]
        B["whatsapp-bridge (Go, whatsmeow)<br/>REST :8080, webhooks"]
        DB[("store/<br/>whatsapp.db: session keys<br/>messages.db: history, identities, embeddings")]
        M["whatsapp-mcp-server (Python)<br/>stdio, or shared HTTP :8082"]
    end
    C[MCP clients<br/>Claude Code, Claude Desktop, ...]
    WA <-->|linked device| B
    B --> DB
    M -->|REST + API key| B
    M -->|reads| DB
    C -->|"stdio, or HTTP + bearer token"| M
```

The bridge owns all writes to the store. The MCP server reads SQLite directly for
queries and goes through the bridge's REST API for anything that touches
WhatsApp. More in [ARCHITECTURE.md](ARCHITECTURE.md).

## Quickstart

You need Go 1.26+ (a newer toolchain is downloaded automatically from `go.mod`),
Python 3.11+, [uv](https://docs.astral.sh/uv/), and your phone.
[SETUP.md](SETUP.md) has every step with checks and failure modes.

```bash
git clone https://github.com/simonseifert/whatsapp-mcp-pro && cd whatsapp-mcp-pro

# 1. Config. The bridge refuses to start without API_KEY.
grep -v '^API_KEY=' .env.example > .env
echo "API_KEY=$(openssl rand -hex 32)" >> .env

# 2. Bridge: build, then pair by scanning the QR code with your phone
#    (WhatsApp > Settings > Linked Devices > Link a Device).
#    The bridge doesn't read .env itself, so export it first.
cd whatsapp-bridge && go build -o whatsapp-bridge .
set -a; . ../.env; set +a; ./whatsapp-bridge

# 3. MCP server, in a second terminal
cd whatsapp-mcp-server
uv sync --extra pro-cpu     # Linux / Intel / CPU: recall + faster-whisper
# uv sync --extra pro       # Apple Silicon: recall + mlx-whisper
# uv sync                   # no recall or transcription, small install

# 4a. Claude Code, one stdio server per session
claude mcp add whatsapp -e WHATSAPP_MCP_TOOLSETS=all -- \
  uv --directory "$PWD" run main.py

# 4b. Or one shared HTTP server for many sessions
export WA_MCP_FULL_TOKEN=$(openssl rand -hex 24)
MCP_HOST=127.0.0.1 MCP_PORT=8082 .venv/bin/python serve_http.py &
claude mcp add -t http whatsapp http://127.0.0.1:8082/mcp \
  --header "Authorization: Bearer $WA_MCP_FULL_TOKEN"
```

Keep `MCP_HOST` on `127.0.0.1`. To reach it from another machine, bind it to a
VPN or tailnet address and set both tokens; never bind it to `0.0.0.0` or a
public interface. Clients must use the exact host you bound, since the server
rejects other `Host` headers.

To keep it running, see [SETUP.md step 7](SETUP.md#7-keep-it-running): a systemd
unit pair for Linux and a launchd example for macOS
([`scripts/com.example.whatsapp-bridge.plist`](scripts/com.example.whatsapp-bridge.plist)).

### Docker

Community-maintained and may lag behind the native setup; the maintainer runs
everything natively. [`docker-compose.yaml`](docker-compose.yaml) publishes, on
127.0.0.1 only:

| Service | Host port | Container port |
|---|---|---|
| `whatsapp-bridge` | 8180 | 8080 |
| `whatsapp-mcp` (streamable HTTP) | 8081 | 8081 |
| `web-ui` (upstream admin panel) | 8090 | 8080 |

## How it compares

As of Oct 2026, from each project's README and repository. "—" means we couldn't
confirm it either way.

| Project | Engine | MCP | Semantic recall | Local transcription | Multi-account | Maintained |
|---|---|---|---|---|---|---|
| **whatsapp-mcp-pro** (this) | whatsmeow (Go) | yes, stdio + HTTP with scoped tokens, optional OAuth 2.1 | yes | yes (whisper.cpp server, mlx, faster-whisper) | one process per number | yes |
| [lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp) | whatsmeow (Go) | yes, stdio | no | no | no | no, last push Jul 2025 |
| [FelixIsaac/whatsapp-mcp-extended](https://github.com/FelixIsaac/whatsapp-mcp-extended) | whatsmeow (Go) | yes, stdio + HTTP | reference plugin, not in core | reference plugin, not in core | — | yes (upstream of this fork) |
| [verygoodplugins/whatsapp-mcp](https://github.com/verygoodplugins/whatsapp-mcp) | whatsmeow (Go) | yes, stdio + HTTP/SSE | — | yes (whisper.cpp) | — | yes (maintained lharries successor) |
| [aldinokemal/go-whatsapp-web-multidevice](https://github.com/aldinokemal/go-whatsapp-web-multidevice) (GOWA) | whatsmeow (Go), REST gateway | yes, built in, optional OAuth 2.1 | — | — | yes, many devices in one process | yes |
| [openclaw/wacli](https://github.com/openclaw/wacli) | whatsmeow (Go), CLI | — | no, FTS5 keyword search | — | yes, named accounts | yes |

Pick GOWA if you need many numbers behind one REST gateway. Pick wacli if you want a terminal tool. Pick this if you
want to ask questions of your own history.

## Troubleshooting

Short answers; details in [SETUP.md](SETUP.md#troubleshooting).

- **Bridge is running but nothing arrives after a reboot.** It probably started
  before DNS was ready. Check `connected` from `/api/connection`; restart if it is
  false. Recent builds retry the first connect and hand failures to the watchdog,
  and a systemd unit with `After=network-online.target` avoids it.
- **Log says "Stream replaced" and the bridge exits with code 3.** Two processes
  are using the same session, usually two copies of the bridge on one store. Stop
  one. Use `RestartPreventExitStatus=3` so systemd doesn't restart into the fight.
- **Need to re-pair** (logged out, or running headless). Restart the bridge; while
  it waits for a scan it writes the current QR payload to `store/pair-qr.txt`.
  Render it with any QR tool, e.g. `qrencode -t ansiutf8 < store/pair-qr.txt`,
  and scan.
- **`download_media` fails with 403/404 on old media.** The CDN copy expired. Set
  `MEDIA_RETRY_ENABLED=true` to ask the phone to re-upload it; it only works if
  the phone still has the file. A request that isn't answered at once stays
  queued; check `GET /api/media-requests` and download again later.
- **A send returns 504.** The bridge gave up waiting (`SEND_TIMEOUT_SECONDS`,
  25 s; `SEND_MEDIA_TIMEOUT_SECONDS`, 150 s, for files). The message may still
  have gone out. Retry with the same `idempotency_key` (the MCP tools return
  it): if it was delivered, you get the stored result instead of a second copy.
- **A send to a new contact returns 429 with `Retry-After`.** WhatsApp put the
  account in a reachout timelock (error 463). Replies in existing chats still
  work; wait until `reachout_timelock_until` from `/api/connection` and don't
  work around it, since more first contacts during a lock invite a ban.
- **`phone_offline_days` keeps growing.** The phone hasn't been online. Open
  WhatsApp on it before about 14 days, or WhatsApp unlinks the bridge and you
  have to pair again.
- **"refusing sensitive file" or "media path outside allowed directories".** The
  media guard. Put the file in `/tmp` or a directory listed in
  `MEDIA_ALLOWED_DIRS`; secrets and databases are refused even there.

## Credits and upstream

- [lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp), the original
  WhatsApp MCP.
- [FelixIsaac/whatsapp-mcp-extended](https://github.com/FelixIsaac/whatsapp-mcp-extended),
  the maintained base this fork tracks.
- [tulir/whatsmeow](https://github.com/tulir/whatsmeow), the protocol library
  underneath all of it.

Upstream deliberately keeps ML out of its core: its architecture doc calls
transcription and vector search out of scope for a transport layer and points
here for them. So upstream stays a lean transport, and this fork is the
batteries-included build. Transcription and recall were contributed upstream from
here ([#55](https://github.com/FelixIsaac/whatsapp-mcp-extended/pull/55),
[#56](https://github.com/FelixIsaac/whatsapp-mcp-extended/pull/56)) and ship there
as reference plugins. `main` here tracks upstream's `main` plus the pro layer;
the current base is upstream v0.4.0.

Keep the bridge's whatsmeow current. Stale builds eventually fail with
"405 Client outdated", which is how many installs of the original repo broke.

MIT licensed, same as upstream.

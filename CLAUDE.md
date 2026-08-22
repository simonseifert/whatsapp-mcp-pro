# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

A self-hosted WhatsApp layer for AI agents: a Go bridge speaking the WhatsApp
Web protocol, a Python MCP server on top, and three optional interfaces built
around them. Everything runs on the user's own machine.

This is a **fork** of FelixIsaac/whatsapp-mcp-extended. Upstream deliberately
stays a lean transport primitive and keeps ML out of its core; this fork is the
batteries-included build. Transcription and recall were contributed upstream
from here (PRs #55, #56) and now exist in both. When merging upstream, expect
our versions of `lib/recall.py` and `lib/transcribe.py` to be supersets.

## Architecture

```
  WhatsApp servers
        ▲
        │ whatsmeow (linked device — one of your 4 slots)
        ▼
  whatsapp-bridge/ (Go)          REST :8080 · SQLite store · webhooks
        ▲            ▲                    ▲
        │ REST       │ REST+webhook       │ reads store directly
        │            │                    │
  whatsapp-mcp-      wa-client/       wa-dispatch/ · wa-assistant/
  server/ (Python)   (chat UI :8084)  (Claude Code sessions)
   stdio  or  :8082
        │
        ▼
  Claude Code / Desktop / any MCP client (N sessions over HTTP)
```

| Component | What it is |
|---|---|
| `whatsapp-bridge/` | Go daemon on whatsmeow. Pairs as a linked device, stores history in SQLite, exposes REST + webhooks. Packages: `api`, `whatsapp`, `webhook`, `database`, `config`, `antiban`, `security`, `types`. |
| `whatsapp-mcp-server/` | Python FastMCP. 34 tools (19 read-only). Run per-session via stdio (`main.py`) or as one shared HTTP server (`serve_http.py`). |
| `wa-client/` | Chat web UI riding the bridge's session — costs **zero** extra device slots. Default :8084 (`WA_WEB_PORT`). |
| `wa-assistant/` | Message your own chat, a persistent Claude Code session replies. Voice both ways. |
| `wa-dispatch/` | An incoming message from a routed chat opens/wakes a Claude session in that project. Drafts, never sends. macOS + tmux. |
| `whatsapp-web-ui/` | Upstream's Next.js admin panel (pairing, webhooks). Not a chat client. pnpm. |
| `plugins/` | From upstream. Currently a README only. |

**Ports:** 8080 bridge REST · 8082 shared MCP HTTP · 8084 wa-client.

## Layout that matters

```
whatsapp-mcp-server/
  main.py         MCP server, stdio. All tools registered via the @tool()
                  wrapper — NEVER @mcp.tool() directly, that bypasses toolset
                  gating and the annotations the tests assert on.
  serve_http.py   shared streamable-HTTP server + scoped bearer tokens
  whatsapp.py     core library: dataclasses, DB queries, bridge API calls
  lib/  bridge.py  recall.py  transcribe.py  utils.py
```

`lib/models.py` and `lib/database.py` do not exist — 16 of 17 functions in
`bridge.py` plus both modules were deleted in `d027775` as a parallel
implementation nothing called. Upstream still ships them; don't let a merge
bring them back.

## Commands

```bash
# Bridge (Go 1.25+)
cd whatsapp-bridge && go build -o whatsapp-bridge . && ./whatsapp-bridge
go build ./... && go vet ./... && go test ./...

# MCP server (Python 3.11+, uv)
cd whatsapp-mcp-server && uv sync          # + --extra pro   for recall/transcribe
uv run python check.py                     # fast preflight
uv run ruff check . && uv run ruff format --check .   # what CI runs
uv run pytest -q
```

CI runs `ruff check` **and** `ruff format --check`. `ruff check` exits first, so
a green check does not mean format is clean — run both. CI does not run mypy;
there are ~37 pre-existing mypy errors, mostly in tests.

Docker exists under `docker/` but the maintainer runs everything natively.
Prefer the native path unless asked.

**Live deployment: code here, data elsewhere (debian/systemd).** The services
run this repo directly — there is no separate deploy copy.

```
code   ~/Code/tools/whatsapp-mcp-pro          this repo
data   ~/.local/share/whatsapp-mcp-pro/       store/ (session + history) + .env
units  /etc/systemd/system/whatsapp-bridge.service   WorkingDirectory=<data>,
                                                      ExecStart=<repo>/…/whatsapp-bridge
       /etc/systemd/system/whatsapp-mcp.service       WA_STORE_PATH=<data>/store,
                                                      runs <repo>/…/.venv/bin/python
       wa-bridge-health.timer                          watchdog, restarts on stall
```

The bridge writes `store/` relative to its WorkingDirectory, so pointing that at
the data dir is what keeps session + history **out of the repo** — never commit a
`store/`, and there is no longer one in the tree. `scripts/deploy-live.sh` is the
sanctioned path: build + test, online store backup, `systemctl restart`, connection
check. `/usr/local/sbin/standby-sync.sh` mirrors the data dir to the M1 warm
standby and also points at `~/.local/share/whatsapp-mcp-pro`.

(History: the live install used to be a hand-copied `~/whatsapp-mcp-extended-pro`
snapshot that drifted from the repo for weeks; that copy was retired 2026-08-21
when code/data were split as above.)

## Things that bite

- **Two send allowlists, three env names.** `SEND_ALLOWED_JIDS` gates the HTTP
  API layer; `WHATSAPP_ALLOWLIST_JIDS` (alias `WHATSAPP_JID_ALLOWLIST`) gates
  the whatsmeow send call. They are not aliases of each other. Matching is
  exact by design — see `IsRecipientAllowed`; a substring match once let an
  entry of `net` allow every recipient.
- **LID vs phone JID.** The same conversation can be filed under
  `…@lid` on one device and `…@s.whatsapp.net` on another. Both identities live
  in `whatsmeow_device` (`jid`, `lid`); `/api/connection` exposes only the jid.
  Code that watches one identity will silently see nothing on some installs.
  The `identities` table is the join — resolve through it rather than matching
  a raw JID. `whatsmeow_contacts` is not that join: it keeps a *separate row per
  identity*, usually with the address-book name on the phone row and the push
  name on the LID row, so reading it directly returns the same human twice under
  two different names.
- **`connected` is the only honest health signal.** A bridge can be alive,
  port-bound and linked while ingesting nothing — a cold start where DNS was
  not ready leaves it stuck. Check `/api/connection`, not the process.
  `scripts/wabridge.sh` does this; `com.simon.wa-conn-guard` auto-restarts.
- **recall is model-scoped.** Every `message_embeddings` query filters on
  `model`. Changing `MODEL_NAME` must re-embed; without the filter the backfill
  thinks it is done and `recall` np.stacks mismatched dimensions.
- **Never commit a `.env`.** `.env` and `.env.*` are gitignored; `.env.example`
  is the reference and is kept in sync with what the code reads.

## Identity directory

`identities` in `messages.db` is the one place to answer "who or what is this
JID". One row per person, group, newsletter or broadcast list, keyed on a
canonical JID (the phone JID whenever it is known), carrying both JID forms, the
phone number, every name WhatsApp knows plus the local nickname, address-book
and business flags, message counts, activity window, and group metadata.

The bridge owns it (`internal/whatsapp/identity.go`, `internal/database/identities.go`).
It rebuilds on connect, on contact/push-name/business-name events, and every
`IDENTITY_SYNC_INTERVAL`. Events only mark it dirty — app-state sync delivers
hundreds in a burst and each rebuild is a full refresh, so a worker coalesces
them. The MCP server reads it and never writes it (`lib/identity.py`,
`lookup_identity` / `search_directory` / `whatsapp://directory`).

Every read degrades to empty when the table is missing or unsynced, and the
contact tools fall back to their old per-source queries — a store can be older
than the directory, and a bridge can be down.

`MergeLIDChats` runs before each rebuild and folds a LID-filed chat into its
phone-JID twin (`MERGE_LID_CHATS=false` to disable). `NormalizeChatJID` stops
new messages from splitting, but history written before it existed is already
split, and a chat whose LID mapping only arrives later splits until it lands.
Merged chat rows are kept and stamped with `chats.merged_into` rather than
deleted, so an old LID still resolves — readers listing chats must skip them.

## Database migrations

Schema changes ship as append-only SQL in `whatsapp-bridge/migrations/`,
sequentially named, idempotent (`IF NOT EXISTS`), never destructive. Run against
an existing store rather than rebuilding:

```bash
sqlite3 whatsapp-bridge/store/messages.db < whatsapp-bridge/migrations/00N_*.sql
```

Back up `store/messages.db` first. `scripts/wa-backup.sh` does online `.backup`
with retention.

## JID formats

```
individual  {phone}@s.whatsapp.net
group       {id}@g.us
LID         {id}@lid
broadcast   status@broadcast
device      {phone}:{N}@s.whatsapp.net    # :N is the linked device, strip it
```

## Code standards

**Go** — `logger`, not `fmt.Println`. Godoc on exported functions. Table-driven
tests. Wrap errors with `%w`. Fatal startup errors `os.Exit(1)`.

**Python** — `logger` from `lib.utils`, not `print()`. Type hints and docstrings
on public functions. Raise rather than returning empty on error.

**Comments explain why, not what.** Most comments in this repo record a decision
or a trap someone already hit. Preserve that when editing.

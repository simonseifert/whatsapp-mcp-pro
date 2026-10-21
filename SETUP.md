# Setup

Start-to-finish install. Written to be followed by an AI agent doing the setup
on someone's behalf, so it states the checks and the failure modes rather than
assuming.

**If you are an agent reading this: work top to bottom, run the verification
after each step, and do not skip step 4. It needs the human and their phone.**

---

## What you are installing

Two pieces, both required.

| Piece | What it does |
|---|---|
| `whatsapp-bridge` (Go) | Pairs as a WhatsApp linked device, stores history in SQLite, exposes a local REST API |
| `whatsapp-mcp-server` (Python) | The MCP server your Claude client talks to |

The bridge must be running for anything else to work. The other directories in
the repo are optional and mostly dormant; see the status table in the
[README](README.md#status).

---

## 0. Prerequisites

```bash
go version          # need 1.26+
python3 --version   # need 3.11+
uv --version        # https://docs.astral.sh/uv/
sqlite3 --version
```

`go.mod` pins a newer toolchain (`toolchain go1.27.3`). Any Go 1.26+ downloads
it automatically on first build, unless `GOTOOLCHAIN=local` is set in the
environment; unset it if the build complains about the toolchain.

macOS: `brew install go uv sqlite3`. Linux: use your package manager, or the
official Go tarball if the packaged Go is older than 1.26. The bridge needs CGO
(SQLite), so a C compiler must be present (`build-essential` on Debian/Ubuntu,
Xcode command line tools on macOS). Only the optional `mlx` transcription
backend is Apple-Silicon-only.

**The user needs their phone with WhatsApp installed and logged in.**

---

## 1. Clone and build the bridge

```bash
git clone https://github.com/simonseifert/whatsapp-mcp-pro
cd whatsapp-mcp-pro/whatsapp-bridge
go build -o whatsapp-bridge .
```

Verify: `ls -l whatsapp-bridge` shows an executable of a few tens of MB.

The first build downloads the toolchain and Go modules and takes a few minutes.
That is normal.

---

## 2. Configure

```bash
cd ..                      # repo root
grep -v '^API_KEY=' .env.example > .env
echo "API_KEY=$(openssl rand -hex 32)" >> .env
```

**The bridge refuses to start without `API_KEY`.** That is deliberate: the REST
API can send messages, so it is never left unauthenticated.

**The bridge does not read `.env` itself.** The MCP server does (it looks in the
repo root and the current directory), but the bridge only sees real environment
variables. Export the file before starting it (step 4), use
`scripts/run-bridge.sh`, which sources `../.env` for you, or use
`EnvironmentFile=` in systemd (step 7).

Defaults worth knowing (all in `.env.example`):

- Binds `127.0.0.1` only. Set `API_BIND_HOST` to a VPN or tailnet address
  **only** if something on another machine needs it, and understand you are
  exposing a send API. Never a public interface.
- `WA_PRESENCE_MODE=human` and `PRESENCE_PING_ENABLED=false`: the bridge stays
  offline and goes online only briefly around real sends. This keeps the
  user's **phone getting its push notifications**; a permanently online bridge
  silences them. Do not change it casually.
- No send allowlist is set, which means no restriction. Before pointing any
  automation at this, set `SEND_ALLOWED_JIDS` and `WHATSAPP_ALLOWLIST_JIDS`.
  They gate different layers and are not aliases of each other.

---

## 3. Install the Python server

```bash
cd whatsapp-mcp-server
uv sync                    # base install

# optional extras, pick by hardware:
uv sync --extra pro        # Apple Silicon: mlx-whisper (fully local, fast)
uv sync --extra pro-cpu    # anything else: faster-whisper (CPU/GPU, local)
```

Both extras add sentence-transformers for `recall`; they differ only in the
transcription backend. Picking `pro` on a non-Apple-Silicon machine installs
the search model but leaves you without a local transcriber, so use `pro-cpu`
there. Either is a large download (about 2 GB) and only needed for `recall` and
voice notes. Skip on a first install; add later.

Two transcription backends need no extra at all:

- `whisper-server`: a [whisper.cpp](https://github.com/ggml-org/whisper.cpp)
  server reachable at `WHISPER_SERVER_URL`, for example on a machine with a GPU.
  Audio stays on your network.
- `groq`: needs `GROQ_API_KEY`. Near-zero RAM, but the audio leaves the machine.

With `WHISPER_BACKEND=auto` (the default) the first available one wins, in this
order: whisper-server, mlx, faster-whisper, groq.

---

## 4. Pair the phone (THE HUMAN MUST DO THIS)

```bash
cd ../whatsapp-bridge
set -a; . ../.env; set +a
./whatsapp-bridge
```

A QR code prints in the terminal. **The user scans it** with their phone:
WhatsApp > Settings > Linked Devices > Link a Device.

An agent cannot do this step. Stop and ask them.

Among the output you should see these lines (other log lines, with timestamps,
are interleaved):

```
Starting REST API server on 127.0.0.1:8080...
✓ REST API server started on port 8080

Scan this QR code with your WhatsApp app:
<QR code>

Successfully connected and authenticated via QR code!
✓ Connected to WhatsApp!
```

If the bridge runs headless (a server, a service, an SSH session where the QR
renders badly), the current QR payload is also written to
`whatsapp-bridge/store/pair-qr.txt` while it waits, and deleted on success.
Render it with any QR tool, for example `qrencode -t ansiutf8 < store/pair-qr.txt`,
and scan that. The code rotates periodically; re-read the file if a scan fails.

History then syncs in the background, from minutes to tens of minutes depending
on account size. It is usable before sync finishes.

Verify from another terminal:

```bash
curl -s -H "X-API-Key: $API_KEY" http://127.0.0.1:8080/api/connection
```

`"connected":true` is what matters. A running process or a bound port does not
mean the bridge is receiving messages; `connected` does.

Leave the bridge running. To keep it running permanently see step 7.

---

## 5. Connect a Claude client

Pick the one the user actually uses. **Claude Desktop and Claude Code are
different applications with different config files.**

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows). Create it
if absent:

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/whatsapp-mcp-pro/whatsapp-mcp-server",
               "run", "main.py"],
      "env": { "WHATSAPP_MCP_TOOLSETS": "all" }
    }
  }
}
```

Use the **absolute** path, and the absolute path to `uv` (`which uv`) if
Desktop cannot find it. Desktop does not inherit a login shell's PATH.

**Fully quit and reopen Claude Desktop.** Reloading the window is not enough.

### Claude Code

```bash
claude mcp add whatsapp -e WHATSAPP_MCP_TOOLSETS=all -- \
  uv --directory /ABSOLUTE/PATH/whatsapp-mcp-server run main.py
```

### Shared HTTP server (many sessions, one process)

If the user runs several Claude sessions, one shared server avoids a process per
session:

```bash
cd whatsapp-mcp-server
export WA_MCP_FULL_TOKEN=$(openssl rand -hex 24)
MCP_HOST=127.0.0.1 MCP_PORT=8082 .venv/bin/python serve_http.py
claude mcp add -t http whatsapp http://127.0.0.1:8082/mcp \
  --header "Authorization: Bearer $WA_MCP_FULL_TOKEN"
```

Keep `MCP_HOST=127.0.0.1` unless other machines need it; then use a VPN or
tailnet address and set the tokens. Never `0.0.0.0`. With no token set, auth is
off and the bind address is the only protection. Clients must connect using
exactly the host you bound; the server answers other `Host` headers with
421 Misdirected Request.

### Verify any of them

Ask the client: *"list my whatsapp chats"*. It should return real chat names.
If it says it has no such tool, the config was not loaded; check the absolute
paths and that the app was fully restarted.

---

## 6. Toolsets

Tools are grouped, and the pro ones are **opt-in** under stdio via
`WHATSAPP_MCP_TOOLSETS` (comma-separated, or `all`). The shared HTTP server
enables `all` automatically.

| Toolset | Default | Notable tools |
|---|---|---|
| `core`, `send`, `media`, `history`, `groups`, ... | on | `list_messages`, `send_message`, `download_media`, `lookup_identity`, `search_directory` |
| `recall` | off | `recall`: multilingual semantic search over history |
| `audio` | off | `transcribe_audio`: voice-note transcription |
| `inbox` | off | `check_inbox`: "what arrived since I last asked" |

`inbox` is the one worth explaining to the user: Claude Desktop cannot be
interrupted by an incoming message, so instead it asks. The **first**
`check_inbox` call deliberately returns nothing and pins a cursor to now,
otherwise a single question would dump their whole history into the
conversation. Subsequent calls return only what is new.

`recall` needs `uv sync --extra pro` or `--extra pro-cpu`. `audio` needs one of
those extras or a `whisper-server`/`groq` backend.

---

## 7. Keep it running

The bridge stops when its terminal closes, and it exits on purpose when it has
been disconnected for more than about three minutes, expecting a supervisor to
restart it. Run it under one.

### Linux: systemd

Keeping code and data in separate directories means a `git pull` or rebuild
never touches the session or history. The bridge writes `store/` relative to its
working directory, so point `WorkingDirectory` at the data directory.

```bash
mkdir -p ~/.local/share/whatsapp-mcp-pro
cp .env ~/.local/share/whatsapp-mcp-pro/.env
chmod 600 ~/.local/share/whatsapp-mcp-pro/.env
# if you already paired in step 4, move that session over:
mv whatsapp-bridge/store ~/.local/share/whatsapp-mcp-pro/store
```

`/etc/systemd/system/whatsapp-bridge.service` (replace `YOU` and the paths):

```ini
[Unit]
Description=WhatsApp bridge
Wants=network-online.target
After=network-online.target

[Service]
User=YOU
# Data dir: store/ (session keys + history) lives here, not in the repo.
WorkingDirectory=/home/YOU/.local/share/whatsapp-mcp-pro
EnvironmentFile=/home/YOU/.local/share/whatsapp-mcp-pro/.env
ExecStart=/home/YOU/whatsapp-mcp-pro/whatsapp-bridge/whatsapp-bridge
Restart=always
RestartSec=5
# Exit code 3 means "Stream replaced": another process took this session.
# Restarting would just steal it back and start a ping-pong, so don't.
RestartPreventExitStatus=3

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/whatsapp-mcp.service`:

```ini
[Unit]
Description=WhatsApp MCP server (shared HTTP)
Wants=whatsapp-bridge.service
After=whatsapp-bridge.service

[Service]
User=YOU
WorkingDirectory=/home/YOU/.local/share/whatsapp-mcp-pro
EnvironmentFile=/home/YOU/.local/share/whatsapp-mcp-pro/.env
# The MCP server reads the store directly; tell it where the data dir put it.
Environment=WA_STORE_PATH=/home/YOU/.local/share/whatsapp-mcp-pro/store
ExecStart=/home/YOU/whatsapp-mcp-pro/whatsapp-mcp-server/.venv/bin/python /home/YOU/whatsapp-mcp-pro/whatsapp-mcp-server/serve_http.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Put `MCP_HOST`, `MCP_PORT` and the `WA_MCP_*_TOKEN` values in the same `.env`.
Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now whatsapp-bridge whatsapp-mcp
journalctl -u whatsapp-bridge -f      # QR shows here if not yet paired
```

**Health check.** `Restart=always` covers a crash, not a bridge that is alive
but disconnected. A timer that checks `connected` covers that:

```bash
# e.g. every 5 minutes from a systemd timer or cron
curl -sf -m 5 -H "X-API-Key: $API_KEY" http://127.0.0.1:8080/api/connection \
  | grep -q '"connected":true' || systemctl restart whatsapp-bridge
```

`scripts/deploy-live.sh` is the maintainer's build, test, back up, restart and
verify script for this layout. It rolls back to the previous binary if any bridge
fails to report `connected`. Set `WA_DATA_DIR` and `OTHER_BRIDGES` before
using it.

### macOS: launchd

```bash
cp scripts/com.example.whatsapp-bridge.plist ~/Library/LaunchAgents/
# edit the paths and API_KEY inside (launchd does not read .env), then:
launchctl load ~/Library/LaunchAgents/com.example.whatsapp-bridge.plist
```

### Anything else

`scripts/run-bridge.sh` is a restart loop that sources `../.env`; run it from
`whatsapp-bridge/` inside `tmux` or `screen`. On Windows, NSSM works.

### A second number

Run a second bridge with its own data directory, its own `.env` with a
different `API_PORT` (and its own `API_KEY`), and its own unit. Two bridges must
never share a `store/`; that is exactly what triggers "Stream replaced". The MCP
server talks to one bridge (`BRIDGE_HOST`) and one store (`WA_STORE_PATH`), so
run a second MCP server on another `MCP_PORT` if you want tools for the second
number.

---

## Troubleshooting

**"API_KEY environment variable is required"**: the bridge doesn't see the key.
It does not read `.env` on its own; export it (`set -a; . ../.env; set +a`), use
`scripts/run-bridge.sh`, or set `EnvironmentFile=` in the unit.

**Bridge running, but nothing arrives after a reboot**: it likely started
before the network or DNS was ready. Check `connected`:
`curl -s -H "X-API-Key: $API_KEY" http://127.0.0.1:8080/api/connection`.
If false, restart the bridge. Current builds retry the first connect and hand a
failure to the watchdog; `After=network-online.target` and the health check in
step 7 cover the rest.

**"✗ Stream replaced — another process took this session, exiting with code 3"**:
two processes are using one session, usually two copies of the bridge on the
same `store/` (a manual run next to the service, or a standby that started while
the primary was up). Stop one. Keep `RestartPreventExitStatus=3` in the unit so
systemd doesn't restart into the fight.

**Need to pair again** (the log says "✗ Device logged out - credentials wiped,
re-pairing required", or you unlinked it on the phone): restart the bridge and
pair as in step 4. On a headless machine use `store/pair-qr.txt` in the data
directory.

**QR code never appears / `Client outdated (405)`**: WhatsApp changed the
protocol and whatsmeow needs updating:
`cd whatsapp-bridge && go get -u go.mau.fi/whatsmeow@latest && go mod tidy && go build -o whatsapp-bridge .`

**`download_media` fails with 403 or 404 on older media**: the CDN copy has
expired. Set `MEDIA_RETRY_ENABLED=true` and restart the bridge; it then asks the
sender's phone to re-upload. Each retry shows "Finished syncing" on the user's
phone, and it only recovers media the phone still has.

**"refusing sensitive file" or "media path outside allowed directories"** when
sending a file: the media guard. Local paths are accepted only from `/tmp` and
directories listed in `MEDIA_ALLOWED_DIRS`. Inside those, dotfiles, anything
under a dot-directory, key and credential files, SQLite databases and their
sidecars, and files at the top of a `store/` directory are still refused. Copy
the file somewhere allowed rather than widening the list.

**Desktop: "Server disconnected" with `Non-HTTPS URLs are only allowed for
localhost`**: you pointed Desktop at a shared HTTP server on another host via
`mcp-remote`, which refuses plain HTTP to anything but localhost. Add
`--allow-http` to the args, right after the URL. Only do this on a private
network or VPN; the traffic is unencrypted.

**HTTP client gets 421 Misdirected Request**: the URL's host differs from
`MCP_HOST`. Use exactly the address the server is bound to.

**Client shows no WhatsApp tools**: config not loaded. Confirm the absolute
paths, that `uv` resolves (use its full path), and that the app was **fully
quit** and reopened. In Claude Code, `/mcp` lists connected servers.

**Tools appear but every call errors**: the bridge is not running, or is on a
different port than `BRIDGE_HOST`. Check `curl http://127.0.0.1:8080/api/health`.

**MCP server says the store or `messages.db` is missing**: it resolves the store
from `WA_STORE_PATH`, then `whatsapp-bridge/store` in the repo. If the bridge's
working directory is elsewhere (as in step 7), set `WA_STORE_PATH`.

**The phone stopped showing WhatsApp notifications**: something set
`PRESENCE_PING_ENABLED=true` or `WA_PRESENCE_MODE=always_online`. Set them back
and restart the bridge.

**`recall` says it is unavailable**: neither `--extra pro` nor `--extra pro-cpu`
was installed, or `WHATSAPP_MCP_TOOLSETS` does not include `recall`.

---

## Before telling the user it is done

- [ ] Bridge running, `/api/connection` reports `"connected":true`
- [ ] Their client lists real chats
- [ ] The bridge runs under a supervisor (step 7), or they know how to restart it
- [ ] They have read the [account safety section](README.md#account-safety)

That last one matters. This is an unofficial client; it violates WhatsApp's
terms and carries real ban risk. The user should decide knowingly, not
discover it later.

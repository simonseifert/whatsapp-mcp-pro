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
  They gate different layers and are not aliases of each other. Both cover
  messages, reactions, polls, edits and deletes.

Settings for sends, the phone and media retry (bridge side, all optional):

| Variable | Default | What it does |
|---|---|---|
| `SEND_TIMEOUT_SECONDS` | `25` | Upper bound on one send, including antiban pacing. Kept under the MCP server's 30 s HTTP timeout so the bridge answers 504 and records the outcome against the idempotency key before the client gives up. |
| `SEND_MEDIA_TIMEOUT_SECONDS` | `150` | The same for sends that upload a file first. |
| `REACHOUT_TIMELOCK_DEFAULT` | `24h` | How long sends to contacts with no history are refused after a WhatsApp error 463, when WhatsApp does not say when the lock ends. Replies and groups are not affected. |
| `PHONE_OFFLINE_PING_AFTER` | `7d` | After this much silence from the phone, send it one invisible request a day to wake it. `7d` or any Go duration. |
| `PHONE_PING_ENABLED` | `true` | `false` stops the ping. Tracking, warnings and the `/api/connection` fields stay on. |
| `MEDIA_RETRY_ENABLED` | `false` | Ask the sender's phone to re-upload expired media. Every request shows "Finished syncing" on the phone. |
| `MEDIA_RETRY_MAX_PER_HOUR` | `20` | Cap on retry requests per rolling hour, including those `/api/download` sends itself. |
| `MEDIA_RETRY_MAX_ATTEMPTS` | `3` | Requests per message before the queue gives up. A "not found" answer from the phone fails it at once. |

The OAuth variables (`MCP_OAUTH_*`, `MCP_PUBLIC_URL`, `MCP_TRUSTED_PROXY`) are
covered in [step 5](#remote-connector-via-oauth-optional).

**Sending from scripts.** `/api/send`, `/api/reaction` and `/api/poll/create`
accept an idempotency key, as `idempotency_key` in the JSON body or an
`Idempotency-Key` header. Generate a new random one per message (not one
derived from the text, or a deliberate second "ok" is swallowed) and reuse it
only to retry that message. A repeat after success returns the stored result
with the header `Idempotent-Replay: true` and sends nothing; a repeat while the
first is running gets 409; the same key with a different payload gets 422.
Keys are kept for 24 hours. Other codes: 400 bad input, 403 allowlist or media
path, 429 rate limit, antiban pause or reachout timelock (the timelock one carries
`Retry-After`), 502 WhatsApp error, 503 not connected, 504 timeout. The MCP tools handle all of
this themselves.

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

### Remote connector via OAuth (optional)

claude.ai custom connectors and ChatGPT cannot send a static bearer header;
they only speak OAuth. `MCP_OAUTH_ENABLED=true` makes the shared server its
own single-user OAuth 2.1 authorization server (PKCE, dynamic client
registration, protected-resource metadata, refresh-token rotation). It is off
by default and the static tokens above keep working either way.

**Only do this behind a public HTTPS URL**, and understand what it means: your
WhatsApp account becomes reachable from the internet, guarded by one
passphrase. A full-access grant lets whoever holds it send messages as you.
Pick **read-only** on the consent page unless the connector really has to
send, and set the send allowlists (`SEND_ALLOWED_JIDS`,
`WHATSAPP_ALLOWLIST_JIDS`) before granting full access.

1. Give the server a public HTTPS URL that forwards to `127.0.0.1:8082`
   (keep `MCP_HOST=127.0.0.1`; the tunnel or proxy connects locally):
   - Tailscale Funnel: `tailscale funnel --bg 8082` serves
     `https://<machine>.<tailnet>.ts.net`.
   - A reverse proxy (Caddy, nginx) with a real certificate. It must pass the
     public hostname through as `Host` (nginx: `proxy_set_header Host $host;`),
     or every request gets 421.
   - Either way every request then reaches the server from the proxy's
     address. Set `MCP_TRUSTED_PROXY=127.0.0.1` (the proxy's address) so the
     consent page's per-address backoff reads the real client from
     `X-Forwarded-For`; without it all visitors share one address. Never list
     an address untrusted clients can connect from: the header is forgeable.
2. In the server's `.env`:
   ```bash
   MCP_OAUTH_ENABLED=true
   MCP_PUBLIC_URL=https://<machine>.<tailnet>.ts.net   # origin only, no /mcp
   MCP_OAUTH_PASSPHRASE=$(openssl rand -base64 24)     # min 12 chars; falls back to WA_MCP_FULL_TOKEN
   ```
   The server refuses to start if the URL is not https (localhost excepted),
   has a path, or the passphrase is missing or short. Clients and hashed
   tokens persist in `oauth.db` next to the store dir (mode 600,
   `MCP_OAUTH_DB` to move it), so a restart does not log connectors out.

   Only clients whose redirect URI points at an allowed host can register or
   reach the consent page: `MCP_OAUTH_ALLOWED_REDIRECT_HOSTS`, default
   `claude.ai,claude.com,chatgpt.com,localhost,127.0.0.1`, exact host match.
   Add a host only for a client you trust; removing one cuts off its clients
   at the next sign-in.
3. Restart the MCP server and check
   `https://<host>/.well-known/oauth-protected-resource/mcp` returns JSON.
4. claude.ai: **Settings → Connectors → Add custom connector**, URL
   `https://<host>/mcp`, leave the OAuth client ID/secret empty, **Add**, then
   **Connect**. A consent page from your server opens: enter the passphrase,
   choose read-only or full, **Approve**. ChatGPT works the same way from its
   connector settings (developer mode), with OAuth as the auth type.

**Reading the consent page.** The large host is where access goes; trust that,
not the name, which any client can set to "Claude" when it registers (the page
marks it unverified). The page only works in the browser that started the
sign-in: a cookie set by `/authorize` is checked on approval, so a consent
link someone sends you cannot be approved. If you did not just click Connect
somewhere, close it.

**Wrong passphrases** cost the sender an increasing wait (2 s, 4 s, 8 s ... up
to 15 min, counted per IPv4 address or per IPv6 /64, and not cleared by a
success within the hour), and three wrong tries kill that consent request.
Against guessing from many addresses there is a global budget: after 20 wrong
passphrases in an hour from all sources (`MCP_OAUTH_FAILURE_BUDGET`,
`MCP_OAUTH_FAILURE_WINDOW`) the page stops accepting passphrases until the
window slides, and the server logs a warning. None of this can lock you out:
approving from a shell on the server always works. Run the command the page
shows, then press **Continue after server approval**:

```bash
cd whatsapp-mcp-server
.venv/bin/python -m lib.oauth approve <req_id> --scope read   # or --scope full
```

Access tokens live an hour (`MCP_OAUTH_ACCESS_TTL`), refresh tokens 30 days
(`MCP_OAUTH_REFRESH_TTL`) and rotate on every use; a reused old refresh token
revokes that whole grant. To see or cut off connectors:

```bash
.venv/bin/python -m lib.oauth list
.venv/bin/python -m lib.oauth revoke <client_id>    # takes effect immediately
```

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

**Phone offline.** A linked device keeps working only while your phone comes
online now and then; WhatsApp unlinks it after about 14 days without the phone,
and restarting the bridge does not help. `/api/connection` reports this too:

| Field | Meaning |
|---|---|
| `phone_last_seen` | RFC3339 time the bridge last saw traffic only the phone sends (its read receipts, messages typed on it, media re-uploads). `null` if not seen since tracking began. |
| `phone_offline_days` | Days since then (one decimal). When `phone_last_seen` is `null`, counted from when tracking began, so a lower bound. `null` only if tracking is not running. |
| `reachout_timelock_until` | RFC3339 end of a WhatsApp reachout timelock (error 463) while one is active, else `null`. Until then the bridge refuses sends to contacts with no history. |

Alert well before 14, for example at 10:

```bash
curl -s -H "X-API-Key: $API_KEY" http://127.0.0.1:8080/api/connection \
  | jq -e '(.phone_offline_days // 0) < 10' >/dev/null || echo "open WhatsApp on the phone"
```

The bridge also logs a WARN once a day past 7 days and an ERROR past 12, and
after `PHONE_OFFLINE_PING_AFTER` (default `7d`) sends the phone one invisible
request a day to wake it (`PHONE_PING_ENABLED=false` turns that off).

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
phone, and it only recovers media the phone still has. If the phone doesn't
answer in time the request stays queued and is retried later at a paced rate;
`curl -s -H "X-API-Key: $API_KEY" 'http://127.0.0.1:8080/api/media-requests?state=pending'`
lists what is waiting, and the next download serves the saved file.

**A send returns 504**: it ran past `SEND_TIMEOUT_SECONDS` (or
`SEND_MEDIA_TIMEOUT_SECONDS` for files). The message may still have been
delivered. Retry with the same idempotency key; if it went out, the bridge
returns the stored result instead of sending a second copy. The MCP send tools
already retry once this way and return the key they used.

**Sends to new contacts return 429 with `Retry-After`**: WhatsApp has put the
account in a reachout timelock (error 463) and the bridge is holding first
contacts until `reachout_timelock_until` in `/api/connection`. Replies in
existing chats still work. Wait it out; more first contacts during a lock are
how it becomes a ban.

**`phone_offline_days` keeps rising, or the log warns the phone is offline**:
open WhatsApp on the phone and let it connect. WhatsApp unlinks the bridge
after about 14 days without the phone, and then only pairing again (step 4)
helps.

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

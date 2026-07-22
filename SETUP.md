# Setup

Start-to-finish install. Written to be followed by an AI agent doing the setup
on someone's behalf, so it states the checks and the failure modes rather than
assuming.

**If you are an agent reading this: work top to bottom, run the verification
after each step, and do not skip step 4 — it needs the human and their phone.**

---

## What you are installing

Three pieces. Only the first two are required.

| Piece | Required | What it does |
|---|---|---|
| `whatsapp-bridge` (Go) | yes | Pairs as a WhatsApp linked device, stores history in SQLite, exposes a local REST API |
| `whatsapp-mcp-server` (Python) | yes | The MCP server your Claude client talks to |
| `wa-client` (Python) | no | A self-hosted web chat UI |

The bridge must be running for anything else to work.

---

## 0. Prerequisites

```bash
go version          # need 1.24+
python3 --version   # need 3.11+
uv --version        # https://github.com/astral-sh/uv
sqlite3 --version
```

macOS: `brew install go uv sqlite3`. Linux: use your package manager; the Go
bridge and Python server are cross-platform. Only the optional `mlx`
transcription backend is Apple-Silicon-only.

**The user needs their phone with WhatsApp installed and logged in.**

---

## 1. Clone and build the bridge

```bash
git clone https://github.com/simonseifert/whatsapp-mcp-pro
cd whatsapp-mcp-pro/whatsapp-bridge
go build -o whatsapp-bridge .
```

Verify: `ls -l whatsapp-bridge` shows an executable ~25-30 MB.

The first build downloads Go modules and takes a few minutes. That is normal.

---

## 2. Configure

```bash
cd ..                      # repo root
cp .env.example .env
echo "API_KEY=$(openssl rand -hex 32)" >> .env
```

**The bridge refuses to start without `API_KEY`.** That is deliberate — the
REST API can send messages, so it is never left unauthenticated.

Defaults worth knowing (all in `.env.example`):

- Binds `127.0.0.1` only. Set `API_BIND_HOST=0.0.0.0` **only** if something on
  another machine needs it, and understand you are exposing a send API.
- `PRESENCE_PING_ENABLED=false` — the bridge does not broadcast "online".
  Leaving this off means the user's **phone keeps its push notifications**;
  turning it on silences them. Do not enable it casually.

---

## 3. Install the Python server

```bash
cd whatsapp-mcp-server
uv sync                    # base install

# optional extras — pick by hardware:
uv sync --extra pro        # Apple Silicon: mlx-whisper (fully local, fast)
uv sync --extra pro-cpu    # anything else: faster-whisper (CPU/GPU, local)
```

Both add sentence-transformers for `recall`; they differ only in the
transcription backend. Picking `pro` on a non-Apple-Silicon machine installs
the search model but leaves you without a working transcriber — use `pro-cpu`
there. Either is a large download (~2 GB) and only needed for `recall` and
voice notes. Skip on a first install; add later.

There is also a `groq` backend (`WHISPER_BACKEND=groq`, needs `GROQ_API_KEY`)
which sends audio to Groq rather than transcribing locally — near-zero RAM,
but the audio leaves the machine.

---

## 4. Pair the phone — THE HUMAN MUST DO THIS

```bash
cd ../whatsapp-bridge && ./whatsapp-bridge
```

A QR code prints in the terminal. **The user scans it** with their phone:
WhatsApp → Settings → Linked Devices → Link a Device.

An agent cannot do this step. Stop and ask them.

Success looks like:

```
✓ Successfully authenticated
✓ Connected to WhatsApp!
Starting REST API server on 127.0.0.1:8080...
```

History then syncs in the background — minutes to tens of minutes depending on
account size. It is usable before sync finishes.

Verify from another terminal:

```bash
curl -H "X-API-Key: $API_KEY" http://127.0.0.1:8080/api/health
```

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
Desktop cannot find it — Desktop does not inherit a login shell's PATH.

**Fully quit and reopen Claude Desktop.** Reloading the window is not enough.

### Claude Code

```bash
claude mcp add whatsapp -- uv --directory /ABSOLUTE/PATH/whatsapp-mcp-server run main.py
```

### Verify either one

Ask the client: *"list my whatsapp chats"*. It should return real chat names.
If it says it has no such tool, the config was not loaded — check the absolute
paths and that the app was fully restarted.

---

## 6. Toolsets

Tools are grouped, and the pro ones are **opt-in** via
`WHATSAPP_MCP_TOOLSETS` (comma-separated, or `all`):

| Toolset | Default | Notable tools |
|---|---|---|
| `core`, `send`, `media`, `history`, `groups`, … | on | `list_messages`, `send_message`, `download_media` |
| `recall` | off | `recall` — multilingual semantic search over history |
| `audio` | off | `transcribe_audio` — local voice-note transcription |
| `inbox` | off | `check_inbox` — "what arrived since I last asked" |

`inbox` is the one worth explaining to the user: Claude Desktop cannot be
interrupted by an incoming message, so instead it asks. The **first**
`check_inbox` call deliberately returns nothing and pins a cursor to now —
otherwise a single question would dump their whole history into the
conversation. Subsequent calls return only what is new.

`recall` and `audio` require `uv sync --extra pro`.

---

## 7. Keep it running (optional)

The bridge stops when its terminal closes. On macOS, a launchd agent:

```bash
cp scripts/com.example.whatsapp-bridge.plist ~/Library/LaunchAgents/
# edit the paths inside, then:
launchctl load ~/Library/LaunchAgents/com.example.whatsapp-bridge.plist
```

Or simply run it inside `tmux`/`screen`. See `scripts/` for examples.

---

## 8. Optional: the web client

```bash
cd wa-client && WA_WEB_HOST=127.0.0.1 ../whatsapp-mcp-server/.venv/bin/python app.py
```

Open <http://127.0.0.1:8084>. It rides the bridge's existing device session, so
it costs **no additional linked-device slot**.

**It has no login of its own.** Bind it to `127.0.0.1` or a VPN address only.
Never expose it to the internet.

---

## Troubleshooting

**"API_KEY environment variable is required"** — step 2 was skipped, or the
bridge was started from a directory where `.env` is not visible. Run it from
`whatsapp-bridge/` with `.env` in the repo root, or export `API_KEY` directly.

**QR code never appears / `Client outdated (405)`** — WhatsApp changed the
protocol and whatsmeow needs updating:
`cd whatsapp-bridge && go get -u go.mau.fi/whatsmeow@latest && go mod tidy && go build -o whatsapp-bridge .`

**Desktop: "Server disconnected" with `Non-HTTPS URLs are only allowed for
localhost`** — you pointed Desktop at a shared HTTP server on another host via
`mcp-remote`, which refuses plain HTTP to anything but localhost. Add
`--allow-http` to the args, right after the URL. (Only do this on a private
network or VPN; the traffic is unencrypted.)

**Client shows no WhatsApp tools** — config not loaded. Confirm the absolute
paths, that `uv` resolves (use its full path), and that the app was **fully
quit** and reopened. In Claude Code, `/mcp` lists connected servers.

**Tools appear but every call errors** — the bridge is not running, or is on a
different port. Check `curl http://127.0.0.1:8080/api/health`.

**Messages stop arriving after a while** — the bridge lost its websocket. It
exits on a watchdog so a supervisor can restart it; if you are running it by
hand, restart it. This is what step 7 solves.

**The phone stopped showing WhatsApp notifications** — something set
`PRESENCE_PING_ENABLED=true`. Set it back to `false` and restart the bridge.

**`recall` says it is unavailable** — `uv sync --extra pro` was not run, or
`WHATSAPP_MCP_TOOLSETS` does not include `recall`.

---

## Before telling the user it is done

- [ ] Bridge running, `/api/health` responds
- [ ] Their client lists real chats
- [ ] They know the bridge must keep running, and how to restart it
- [ ] They have read the account-safety note in [README](README.md#account-safety-honestly)

That last one matters. This is an unofficial client; it violates WhatsApp's
terms and carries real ban risk. The user should decide knowingly, not
discover it later.

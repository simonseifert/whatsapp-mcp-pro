# wa-app

A self-hosted, login-protected chat PWA that rides the whatsapp-mcp-pro bridge. Visual contract: [DESIGN.md](DESIGN.md).

Status: M2, read, reply and AI drafts (chat list, chat view, media, live updates, read state, sending, drafts you can use or ignore). Install and screenshots: see the repo [README](../README.md#app).

## Layout

- `server/` Starlette app (`wa-app` console script): JSON API, session auth, serves `web/dist`.
- `web/` Vite + React + TypeScript + Tailwind v4 front end. `src/styles/tokens.css` is generated from `DESIGN.md`, never edited by hand.
- `deploy/wa-app.service` systemd user unit template.

## Configuration

| Variable | Default | Meaning |
|-|-|-|
| `WA_APP_HOST` | `127.0.0.1` | Bind address |
| `WA_APP_PORT` | `8085` | Bind port |
| `WA_APP_DATA` | `~/.local/share/wa-app` | Where `app.db` lives |
| `WA_STORE_PATH` | same rule as `whatsapp-mcp-server` | Bridge store dir holding `messages.db` |
| `WA_APP_CLAUDE_BIN` | the `claude` on `PATH` | Path to the `claude` CLI, for the CLI backend |
| `WA_APP_INSECURE_COOKIES` | unset | `1` drops the `Secure` cookie flag, for local http development only |

The login rate limit keys on the client address; behind a loopback proxy (tailscale serve) it uses the last `X-Forwarded-For` hop (the proxy appends the real client; earlier hops are client-controlled), trusted only from a loopback peer.

Expose it with `tailscale serve` (never Funnel by default). Session cookies are `Secure`, so they need https.

## AI drafts

The app can suggest a reply in chats you switch on. A draft shows as a card above the composer with one or two alternatives. Tap one to put it in the composer, edit it, and send it yourself. **Nothing is sent without your tap**: the server has no path from a draft to a send.

### Backends

Add one or more under Settings, AI, and pick a default.

- **`claude` CLI.** Runs your own `claude` binary on your own login. The app never reads or stores Claude credentials.
- **Anthropic API.** Needs an API key.
- **OpenAI-compatible.** Any endpoint that speaks the chat completions API, such as local Ollama (`http://127.0.0.1:11434/v1`). A key is optional.

Each backend has a Test button. Keys are encrypted at rest in `ai.key` inside `WA_APP_DATA` (mode 600), are never shown again after you save them, and are never logged. The API only says whether a key exists. **Back up `ai.key` together with `app.db`**: without it the saved keys cannot be read and must be entered again.

### What is stored

`app.db` keeps copies of drafts and of the texts you sent after one (tables `drafts`, `draft_outcomes`), because learning needs them. The nightly sweep (03:30) deletes drafts, and their outcomes, older than 90 days.

### Per chat

The AI sheet in a chat header sets, for that chat:

- **Drafts in this chat:** the on switch. Chats start off.
- **AI:** a backend, or the default.
- **Local only:** the chat may only use an OpenAI-compatible endpoint whose host is loopback or a private address (every resolved address is checked). Anything else is refused before any request, and the card says "This chat is local only". Chat content never leaves your network.
- **Note:** anything the AI should know about the chat.
- **Rules:** plain text, at most 12 lines, learned from your replies. Edit freely; your edit is the base for the next round.

### The claude CLI

The server runs:

```sh
claude -p --output-format json --model <model> --system-prompt <fixed text> \
  --tools "" --disallowedTools "*" --max-turns 1 \
  --strict-mcp-config --setting-sources ""
```

Only the fixed instructions go in `--system-prompt`; everything derived from the chat (note, rules, examples, messages) goes in on stdin, so it never shows in the process list. It runs from an empty working directory, with a 120 s timeout. `--strict-mcp-config --setting-sources ""` matters for speed: without them the CLI starts every MCP server, hook and plugin you have configured. A draft took about 2.6 s with the flags and 26 to 29 s without. Set `WA_APP_CLAUDE_BIN` if `claude` is not on the server's `PATH`.

### Costs

- `claude` CLI: counts against your own Claude plan limits.
- Anthropic API: billed per call.
- Local (Ollama and similar): free.

### What triggers a draft

A new incoming text message, or a voice note or media with a caption, in an opted-in chat (stickers, polls, contact cards and media without a caption never trigger; the Draft button still accepts them), at most 10 minutes old when first seen. History sync, backfill and imports never trigger. Announcement groups only when you are an admin.

- App open: drafts at once.
- App closed: waits for 20 s without a new incoming message in that chat, then drafts once. A burst of messages makes one draft, for the last one.
- A new triggering message restarts the draft. Drafts expire after 24 h. In groups the draft quotes the message it answers.
- Any reply you send from anywhere, phone included, clears the card. Failures show "Couldn't draft · Try again" and never retry on their own.

You can also tap Draft in a chat to ask for one.

### Learning

Your next own message in the chat, within 15 minutes of the draft, is compared with the closest alternative: **used** (about identical), **edited** (close) or **ignored** (different, which teaches nothing). Edited outcomes become examples for later drafts, next to your last 30 replies in the chat. After every 10 new used or edited outcomes, and nightly at 03:30 for chats with new ones, the model distils the chat's rules from them.

## Develop

```sh
cd web && pnpm install && pnpm build   # builds web/dist
cd ../server && uv sync && WA_APP_INSECURE_COOKIES=1 uv run wa-app
```

## Tests

```sh
cd server && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy .
cd web && pnpm lint && pnpm exec tsc -b && pnpm test && pnpm build
cd web && pnpm exec playwright install chromium webkit && pnpm e2e   # uses WA_APP_PORT (default 18085)
```

## Regenerating tokens

```sh
cd web && pnpm exec design.md export ../DESIGN.md --format css-tailwind > src/styles/tokens.css
```

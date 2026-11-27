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
| `WA_APP_AGENT_TOKEN` | unset | Bearer token for `/api/agent/*` (drafts from Claude). Unset means those routes answer 404 |
| `WA_APP_INSECURE_COOKIES` | unset | `1` drops the `Secure` cookie flag, for local http development only |

The login rate limit keys on the client address; behind a loopback proxy (tailscale serve) it uses the last `X-Forwarded-For` hop (the proxy appends the real client; earlier hops are client-controlled), trusted only from a loopback peer.

Expose it with `tailscale serve` (never Funnel by default). Session cookies are `Secure`, so they need https.

### Give wa-app its own hostname

Cookies are not isolated by port: every service on the same hostname (`host.tailnet.ts.net:8445` next to `:8443`,
`:10000` and the rest) receives the same cookies and counts as the same site. A bug in any of them could read or record
wa-app's session cookie, or frame wa-app. So serve wa-app on a hostname of its own (a Tailscale Service or a separate
tsnet node) and not on a shared one. The cookie is `__Host-wa_session` (Secure, `Path=/`, no
`Domain`), which stops subdomains and plain-http pages from setting it, but it does not isolate ports: other services on
the same host still receive it and can set it. **This finding stays open until wa-app has its own hostname.**
The old `wa_session` name is accepted for one release so existing logins survive; the next login sets the new name.
Plain-http development (`WA_APP_INSECURE_COOKIES=1`) keeps the old name, because `__Host-` needs `Secure`.

## Security headers and limits

Every response carries `Content-Security-Policy` (`default-src 'self'`, images and media from `self`, `blob:` and
`data:`, no inline script or style, `frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`,
`Referrer-Policy: same-origin`, `X-Frame-Options: DENY` and `Cross-Origin-Opener-Policy: same-origin`. The media routes
keep their own stricter `sandbox` CSP. The theme bootstrap is a file (`web/public/theme-init.js`) for this reason.
Request bodies over 64 KB get `413` before they are read (`auth.MAX_JSON_BODY`; `auth.UPLOAD_LIMITS` holds per-path caps
for any future upload route).

## Voice notes and local-only chats

A voice note from a chat marked local only is only ever transcribed by a local backend (whisper server, mlx,
faster-whisper). If none works the request fails with "Local transcription unavailable"; it never falls back to Groq.
`TRANSCRIBE_CLOUD_FALLBACK=0` in the shared `.env` turns the Groq fallback off for every chat.

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
- **Local only:** the chat may only use an OpenAI-compatible endpoint on loopback or a private address. A hostname must use `http://`: it is resolved once, every address must be loopback or private, and the call connects to that checked address. A hostname over `https://` is refused (a pinned address would break the certificate check); a private IP address over `https://` is fine. Anything else is refused before any request, and the card says "This chat is local only". Chat content never leaves your network.
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

## History and draft context

Drafts are only as good as the messages wa-app has stored. **Settings → History** shows what it has:

- **Coverage**: total messages and chats, the oldest month, and a row per chat (the 50 most recently active, archived ones included) with message count, how many of those are yours, and the oldest month stored.
- **Thin**: a chat with fewer than 30 of your own text messages stored. There is too little of your writing for the AI to copy your style, so drafts there read generic.
- **Fill**: pick how far back (Last year, 3 years, Everything; 3 years is the default), then **Fill thin chats** (or **Fill all 50**). wa-app asks your phone for older messages, one chat at a time, in batches. Your phone shows a "Finished syncing" notice for each batch, so keep it online and charging. Progress and a result per chat show on the same screen: Done, More available (run again), Failed, or Skipped (a chat with no stored message gives the phone nothing to anchor on). If the phone sends nothing back for two chats in a row (it is probably offline), the rest of the queue is paused and shown as skipped. A chat that stops making progress for five minutes is marked Failed once the phone is done with it. Stop drops the chats still waiting; the one running finishes.
- **Load older messages**: the same for a single chat, in the chat's AI sheet. It always goes as far back as the phone allows.

To check by hand, open an old chat and scroll up: after a fill the older messages are there, and the oldest month in Settings → History moves back.

What a draft sees is set in **Settings → Draft context**: **About me** (up to 1500 characters on who you are and how you write, used for every draft), how many recent messages a draft reads (10 to 100, default 30) and how many of your past replies it learns from (0 to 100, default 30). About me goes in as quoted data, never as instructions. The first-link history size is a bridge setting, see [SETUP.md](../SETUP.md).

## Drafts from Claude

Tell Claude "draft a reply to Ana saying I'll be there at 7". Claude pushes the text into wa-app; it shows on that chat's draft card, live, labelled **Draft · from Claude**. Tap it to fill the composer, then edit, check and send it yourself. **Nothing is ever sent by Claude**: the agent routes and the MCP tools have no path to sending, and a pushed draft makes no AI call. A new push replaces the chat's current draft. Claude can also switch a chat's automatic drafts on or off (the switch in the chat's AI sheet).

Setup:

1. Make a token (`openssl rand -hex 32`) and put the same value in `WA_APP_AGENT_TOKEN` in both services' environments: wa-app's, and the MCP server's (stdio session env or the shared server's unit). Restart both. Without it in wa-app, `/api/agent/*` answers 404.
2. Enable the opt-in toolset on the MCP server: add `drafts` to `WHATSAPP_MCP_TOOLSETS` (the shared HTTP server already enables `all`). If wa-app is not on `http://127.0.0.1:8085`, set `WA_APP_URL` for the MCP server; keep it loopback (or an https URL), because the token travels in clear over plain http. The token must be at least 32 characters or wa-app refuses to start.
3. Ask Claude for a draft. Tools: `push_draft(chat_jid, text, reply_to?)`, `set_auto_drafts(chat_jid, enabled)`, `get_auto_drafts(chat_jid)`.

The routes (`Authorization: Bearer <token>`, no cookie, no Origin check since browsers never attach a bearer token by themselves): `POST /api/agent/drafts` with `{"chat", "text", "reply_to"?}` and `GET`/`PUT /api/agent/chats/{jid}/ai` with `{"enabled"}`. `reply_to` must be an incoming message of the chat; without it the draft answers the chat's latest incoming message. Everything else under `/api/*` still needs a login session.

A pushed draft counts like any ready draft for learning: if your next own message in the chat is close to it, the outcome is recorded as used or edited.

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

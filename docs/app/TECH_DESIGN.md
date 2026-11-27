# wa-app: technical design

Status: approved by Simon, 2026-11-21. "Verified" means checked against this repo's code
or a live test on 2026-11-21. Everything else is a proposal.

## Approaches considered

| | Approach | Trade-off | Verdict |
|---|---|---|---|
| A | New Python app server next to the MCP server, plus a separate PWA front end | One more service, but it reuses the Python features (recall, transcription, directory, naming) directly and keeps the bridge key server-side | **Picked** (Simon, 2026-11-21) |
| B | Extend `whatsapp-web-ui` (upstream Next.js admin panel) | It calls the bridge from the browser, which exposes the API key. It's a static export with no server for drafts or push. It's upstream code, so merges get harder | Rejected |
| C | Build it into the Go bridge | Recall, transcription and drafting would all need rewriting in Go | Rejected |

`wa-client/` (retired, D2) followed A's shape. It was replaced, not revived (no login,
ignored `WA_STORE_PATH`) and deleted in M1 (2026-11-22). Working name: `wa-app`; the public name never contains "WhatsApp".

## Shape

```
phone (PWA, React)  ──HTTPS (tailscale serve or own TLS)──>  wa-app server (Python, Starlette)
                                                                  │  ├─ reads messages.db read-only (WAL)   [verified safe]
                                                                  │  ├─ imports whatsapp.py + lib/*          [verified]
                                                                  │  ├─ own app.db: drafts, learning, settings, read state
                                                                  │  └─ calls bridge REST with X-API-Key     [verified]
                                                                  └─ AI backends: OpenAI-compatible / Anthropic / claude CLI
```

- **Location:** a new top-level `wa-app/`, with `server/` (Python, its own uv project
  depending on `whatsapp-mcp-server` as a path dependency) and `web/` (Vite + React +
  Tailwind, pnpm).
- **Service:** one process serves the built PWA, a JSON API and an SSE stream. Default
  port 8085, bound to 127.0.0.1. Exposed via `tailscale serve` or a reverse proxy.

## Data

- **Read path:** `messages.db` is opened `mode=ro` (WAL, verified). The latest-rowid
  poll takes 0.01 ms and the chat list query 5 ms on a 39k-message store (measured).
- **Live updates:** poll the max rowid about every 1 s, plus `message_edits`/`deleted_at`
  changes via `lib/changes.py`, and push deltas to open clients over SSE.
  - No bridge webhook is needed. That avoids the SSRF allowlist config. Fallback:
    register a webhook with `WEBHOOK_ALLOWED_ADDRS=127.0.0.1:8085`.
- **Own store, `app.db`** (SQLite):
  - `chat_settings`: drafts on/off, backend, local-only, note.
  - `read_state`: last-read rowid per chat, for unread counts.
  - `drafts`: id, chat, trigger message, alternatives, status, created/sent.
  - `draft_outcomes`: the draft vs the text actually sent, edit distance, used/edited/
    discarded.
  - `chat_rules`: distilled style rules per chat, editable.
  - `transcripts`: message id and text, when transcribed on demand.
  - `push_subscriptions`, `sessions`.
- **Never** write app state into the bridge's tables. The library's own existing
  writes are allowed, because they are already the MCP's sanctioned writes and the app
  reuses those functions: recall's `message_embeddings`, nicknames, and auto-transcribe.
  The app's raw reader connection stays `mode=ro`. *(Builder-check call, 2026-11-21.)*

## Gaps in the bridge (verified 2026-11-21) and what M0 does about them

| Gap | Today | M0 change |
|---|---|---|
| Reactions | Stored as a message with content `[Reaction: 👍]`, no link to the target (`internal/whatsapp/media.go:80`). `GetMessageReactionSummary` is a stub | Store reactions in a `message_reactions` table (target id, sender, emoji, time; removal clears) |
| Pinned, muted, archived | Write-only app-state patches; state never stored | Store app-state Pin/Mute/Archive events per chat |
| Read state | No last-read marker; `/api/read` sends receipts (verified) | The app keeps its own read state and sends receipts via `/api/read` when a chat is opened |
| Communities | Only available live from `GET /api/group/{jid}` (`is_community`, `community_jid`, `is_announcement_group`); not stored | Cache group metadata (parent link, announce, admins) in `identities` or a new `groups` table on group events |
| Stickers | Stored as `[Sticker]` with no media | Treat sticker as downloadable media (webp) |
| Thumbnails | Not stored | The app generates thumbnails from downloaded media (Pillow, ffmpeg for video) |
| Profile pictures | Live WhatsApp call per request, returns a CDN URL | The app caches avatars on disk with a TTL and serves them itself |
| Transcripts | Auto mode overwrites `content` with a `🎤` prefix; on-demand transcription returns text only | On demand, the app calls `lib/transcribe.transcribe_file` and stores the result in `app.db` |
| Security scan | govulncheck fails on Go 1.27.3 (`GO-2026-6859`) | Bump the toolchain to 1.27.4 first |

Migrations follow the repo rules (next is `011`). Upstream PRs for reactions and
communities are offered later.

## Sending

- Everything goes through the bridge's `POST /api/send` with a stable
  `idempotency_key` per draft or compose action (verified: text, `quoted_message_id`,
  `media_path`, `mentioned_jids`).
- Reactions, edits, deletes and read receipts use the existing endpoints (verified).
- The bridge's allowlists, caps and first-contact lock apply unchanged. A 429 shows as
  "not sent: limit".
- Voice notes:
  - Record in the browser, feature-detecting `audio/webm;codecs=opus` then
    `audio/mp4`.
  - Convert server-side with the existing ffmpeg/libopus helper
    (`audio.convert_to_opus_ogg_temp`, verified), then send as a voice note.

## AI drafting

- **Backend interface:** `draft(context) -> list[str]`. Three implementations:
  1. **OpenAI-compatible:** Ollama, LM Studio, llama.cpp, OpenAI, OpenRouter. Base
     URL, model and key. JSON via `response_format`.
  2. **Anthropic API:** user's key, prompt caching on the system block.
  3. **Claude Code CLI:** the user's own logged-in, unmodified `claude`, as allowed by
     Anthropic's terms:
     - Command: `claude -p --output-format json --model <m> --system-prompt … --tools "" --disallowedTools "*" --max-turns 1`.
     - Verified working in 9 s. The repo never reads or stores Claude credentials.
- **Context per draft:**
  - The last N messages, with transcripts, as untrusted data framed like
    `wa-dispatch/prepare-frame.md`.
  - The chat's examples: your past replies in the chat, plus recent edited drafts.
  - The chat's rules and the per-chat note.
  - The language of the last incoming message.
- **Local-only chats** may only use backend 1 pointing at a loopback or private
  address. Enforced server-side.
- **Latency measured 2026-11-21** (made-up Croatian chat, maintainer's server, GPU
  shared with other jobs):

  | Backend | Time per draft |
  |---|---|
  | claude CLI (sonnet) | 9 s |
  | gemma4 26B (local) | 27 s |
  | qwen3.6 35B (local) | 90 to 95 s |

  The quality of all three was usable. Drafts therefore stream into the card, and
  "instant" means "as soon as the model answers".
- **Learning loop:**
  - When a sent message follows a draft in the same chat, record the outcome.
  - After every 10 outcomes in a chat, or nightly, run one distillation call that
    updates that chat's rules (CIPHER-style, arXiv 2404.15269).
  - Cold start uses your last about 30 replies in the chat as few-shot examples.

## Notifications

- Web Push with VAPID via `pywebpush`.
  - iOS requirements are verified from WebKit/Apple docs: home-screen install,
    `display: standalone`, permission from a tap, `userVisibleOnly`, and a clean
    `mailto:` `sub`.
  - No action buttons on iOS, so tapping opens the chat with its draft.
  - pywebpush against Apple's push service passed the spike on an iPhone Home Screen app (2026-11-27).
  - The VAPID key pair is created on first use in the data directory (`vapid.pem`, mode 600). The `sub` claim comes
    from `WA_APP_PUSH_SUBJECT`; without it push stays off.
  - Routes: `GET /api/push/key`, `POST /api/push/subscribe`, `POST /api/push/unsubscribe`, `POST /api/push/test`.
- The server needs outbound HTTPS to `*.push.apple.com` and the other push services.
- **When it notifies (decided 2026-11-25, replaces the 2026-11-21 rule of draft-ready only):** every incoming
  message, like WhatsApp. See DECISIONS D25.
  - Skipped: own and system messages, deleted messages, call-log entries, muted chats, the chat open in a visible
    app window, messages older than 120 s, and sessions that are no longer live (only live sessions' subscriptions
    get a push).
  - Title is the sender, or the group and sender. Body is the text, or a label for media (photo, voice note, file
    and so on), cut to 180 characters. A tag per chat means a new message replaces the previous notification of
    that chat.
  - Payloads are encrypted end to end (RFC 8291), so Apple and Google relay ciphertext only. Turn off the phone
    WhatsApp's own notifications to avoid doubles.
  - iOS delivers push only to the Home Screen app, not to Safari tabs.

## Uploads

- `POST /api/chats/{jid}/media` takes the file as a raw request body, up to 64 MiB. It is streamed to a private
  temp directory under `/tmp` and removed after the send, and at most 2 uploads run at once.
- Headers: `X-Filename` (percent-encoded), `X-Client-Key` (idempotency, as for text sends), `X-Caption` (up to 1024
  characters and 4 KB encoded), `X-Reply-To`, `X-Reply-Chat` and `X-Kind` (`file` or `voice`).
- Voice notes are recorded in the browser (`MediaRecorder`, webm/opus or mp4, 15 minute cap) and converted to Ogg
  Opus on the MCP side before the bridge sends them.
- A private reply to a group message sends with the bridge's `quoted_chat_jid`; the bridge refuses when it cannot
  resolve the quoted message.

## Auth and exposure

- One user. Passphrase set at first run (argon2 hash in `app.db`), then a long-lived,
  HttpOnly, Secure, SameSite=Strict session cookie.
- Bound to 127.0.0.1 by default. The README shows `tailscale serve` for HTTPS on the
  private network.
  - The public internet (Funnel) is not recommended.
  - If someone enables it anyway, login rate-limiting applies.
- CSRF: SameSite=Strict, plus an `Origin` check on state-changing requests.

## Search

- Keyword search: the existing `list_messages(query=)`.
- Search by meaning: `lib.recall.recall()`, imported in-process.
  - It loads its own model copy (about 470 MB on disk, about 700 MB RAM) on first use.
  - It unloads after idle via `RECALL_MODEL_IDLE_UNLOAD_SECONDS`.
  - It is optional: hidden when the `pro` extra isn't installed.

## Front end

- Vite + React + Tailwind, a PWA manifest and a service worker (shell cache only; the
  server is the source of truth).
- `viewport-fit=cover` with safe-area insets, and `visualViewport` for the keyboard.
- Reconnect and refetch on `visibilitychange`, because iOS kills SSE in the background.
- Layout follows WhatsApp: bottom tabs (Chats, Communities, Calls, Settings), a chats
  list with filter chips, the chat view with bubbles, swipe-to-reply, long-press
  actions, voice-note bubbles with "Show text", and the composer with the draft card.
- Colours, icons and wallpaper are its own. The name contains no "WhatsApp" mark.

## Calls made after the builder check (2026-11-21)

- **"App open"** means at least one SSE client is connected and its page is visible.
  A backgrounded PWA counts as closed.
- **Linking a send to a draft:** the first own message in that chat within 15 minutes
  of a draft is its outcome, wherever it was sent from.
  - Below a similarity floor it is recorded as "ignored", not "edited", so unrelated
    replies don't teach anything.
  - Drafts expire after 24 hours.
- **AI settings** live in `app.db` and are edited on a Settings screen. API keys are
  encrypted at rest with a key file next to `app.db` (mode 600) and are never sent
  back to the browser.
  - One global default backend, overridable per chat.
  - The `claude` CLI runs on the server host as the server's OS user, with that user's
    own login.
  - Every call that carries chat content (drafts, distillation) goes through one
    policy gate that enforces local-only.
- **Draft worker:** one queue.
  - Concurrency is 1 for local backends and 3 for cloud backends.
  - A new message in the same chat cancels and restarts that chat's pending draft.
  - Messages from history sync or backfill, and anything older than 10 minutes when
    first seen, never trigger a draft.
- **Unread state** follows the phone.
  - M0 stores your own devices' read events (read receipts from your own devices and
    the app-state "mark as read").
  - The app marks a chat read when its own read marker or the phone's moves past the
    last message.
- **Topology:** same host as the bridge only. It reads the store from the filesystem.
  A second bridge (another number) and a standby machine are out of scope for v1.
- **Pinned, muted, archived:** filled from app-state sync. A fresh install may show
  none until WhatsApp syncs them, so the UI degrades gracefully. This is not an M0
  gate.

## Left to the builder

- Library choices inside the stack (router, state, SSE client, the Starlette or FastAPI
  flavour).
- The exact poll interval and backoff.
- Thumbnail sizes and the cache layout.
- Test structure: follow the repo (pytest, table-driven Go tests, CI lint).

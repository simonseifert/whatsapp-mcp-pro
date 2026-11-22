# wa-app M1: the chat app, read and reply — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A login-protected PWA in `wa-app/` that lists every chat and lets the maintainer read and answer his day from his phone, riding the existing bridge.

**Architecture:** One Python process (Starlette + uvicorn) on 127.0.0.1:8085 serves the built React PWA, a JSON API and one SSE stream. It reads the bridge's `messages.db` read-only, keeps its own `app.db`, imports `whatsapp.py`/`lib/*` from `whatsapp-mcp-server` for names, sends and media, and sends through the bridge REST API so every allowlist, cap and lock applies. The front end is Vite + React + TypeScript + Tailwind v4, styled only from `wa-app/DESIGN.md` tokens.

**Tech Stack:** Python 3.11+, uv, Starlette, uvicorn, argon2-cffi, Pillow, pytest, httpx (test client); pnpm, Vite, React 19, TypeScript, Tailwind v4, React Router, TanStack Query, Lucide icons, vitest, Playwright (chromium + webkit); Go for the one bridge task.

**Spec:** `docs/app/PRD.md`, `docs/app/TECH_DESIGN.md`, `docs/app/MVP.md` (M1 section), `wa-app/DESIGN.md` (locked, tag `design-lock`). Picked screens to match: `designs/wa-app-directions/a-familiar/` and `wa-app/docs/components/index.html`.

## Global Constraints

- Default bind `127.0.0.1:8085`; env `WA_APP_HOST`, `WA_APP_PORT`. Exposure is `tailscale serve`, documented, never Funnel by default.
- `messages.db` is opened `file:<path>?mode=ro` with `uri=True`. The app never writes bridge tables; it may call library functions whose existing writes are already sanctioned (TECH_DESIGN "Data").
- App state lives in `app.db` (SQLite) in `WA_APP_DATA` (default `~/.local/share/wa-app/`), never in the repo.
- Login: passphrase set at first run, argon2 hash in `app.db`; session cookie `HttpOnly; Secure; SameSite=Strict`, 30-day lifetime; every state-changing request checks `Origin` against the request host; login rate limit 5 failures per 15 minutes per IP.
- Every send goes through `whatsapp.send_message` / `send_reaction` / `mark_messages_read` with a fresh idempotency key per compose action (a retry of the same action reuses it). A bridge 429 shows as "Not sent: limit". Never route around the reachout lock.
- `chats.merged_into IS NOT NULL` rows are skipped in every listing (CLAUDE.md identity directory).
- Display names come from `lib.naming` (`resolve_display_names`, `identities_source`), never `identities.display_name` raw.
- Message ids starting `import-` never get a reaction, reply quote or read receipt (`database.IsImportedMessageID` rule).
- UI: only `DESIGN.md` tokens (exported, never hand-copied), Geist self-hosted, message text 16 px everywhere, 44 px touch targets, both themes, no WhatsApp green, no wallpaper. The UI never shows the word "WhatsApp"; the app's display name is the working name `wa-app` until a public name is picked.
- Public repo: tests and fixtures use fictional data only (`designs/wa-app-directions/sample-data.json` style); no real numbers, names, hosts or paths.
- Python: `logger` from `lib.utils`-style logging, type hints, ruff + ruff format + mypy clean, like `whatsapp-mcp-server`. Web: `tsc --noEmit`, eslint, vitest clean.
- The draft card is M2. M1 ships no AI calls.

## Review Focus

1. **LID/phone split chats:** a person whose messages sit under both `…@lid` and `…@s.whatsapp.net` shows as ONE chat with one name, and opening it shows both halves in time order. (Task 2 and Task 3 tests.)
2. **A send that fails or times out** must never show as sent: the bubble stays "sending", then "Not sent: <reason>" with Retry reusing the same idempotency key, and a double tap never sends twice. (Task 7 tests.)
3. **iOS backgrounding:** the PWA is suspended and its SSE dies silently; on return it must refetch and show messages that arrived meanwhile without a reload. (Task 5 tests.)
4. **Unread that disagrees with the phone:** a chat read on the phone must not stay unread in the app, and a chat the user marked unread on the phone shows a dot. (Task 6 tests.)
5. **Hostile or odd content:** message text with HTML/script, a 5,000-character message, RTL text, media with no file on disk, a deleted message that is quoted later. Rendered as text, never as HTML; missing media shows a placeholder, not a broken image. (Task 3 and Task 4 tests.)

---

## File Structure

```
wa-app/
  DESIGN.md                      (locked)
  docs/brand/icons/              (from the design step; copied into web/public at build)
  server/
    pyproject.toml               uv project, path dep on ../../whatsapp-mcp-server
    wa_app/
      __init__.py
      config.py                  env → Settings dataclass
      appdb.py                   app.db schema + migrations + queries
      auth.py                    passphrase, sessions, rate limit, Origin check, middleware
      store.py                   read-only messages.db reader: chats, messages, reactions, state
      unread.py                  unread counts from chat_state + app read markers
      media.py                   media fetch, thumbnails, avatar cache
      transcripts.py             on-demand voice-note transcription into app.db
      live.py                    change poller + SSE fan-out
      sending.py                 text/reply/reaction/read through whatsapp.py
      api.py                     Starlette routes
      main.py                    app factory + uvicorn entry (`wa-app` console script)
    tests/
      conftest.py                fictional store fixture builder, app client, logged-in client
      test_*.py                  one per module
  web/
    package.json  vite.config.ts  index.html  tsconfig.json  eslint.config.js
    src/
      styles/tokens.css          generated: npx @google/design.md export ../DESIGN.md --format css-tailwind
      styles/app.css             theme mapping (-light tokens), base styles, fonts
      main.tsx  App.tsx  api.ts  live.ts  types.ts  format.ts
      screens/Login.tsx  ChatList.tsx  Chat.tsx  Settings.tsx
      components/ChatRow.tsx  FilterChips.tsx  Bubble.tsx  VoiceNote.tsx  Composer.tsx
                 TabBar.tsx  Avatar.tsx  ReactionBar.tsx  Poll.tsx  ContactCard.tsx
      sw.ts                      shell-only service worker
    public/                      icons + manifest (copied from docs/brand/icons)
    e2e/                         Playwright specs (chromium + webkit, 375 and 1440)
  deploy/wa-app.service          systemd user unit (template, fictional paths)
whatsapp-bridge/internal/whatsapp/media.go   contact card text (Task 9)
```

---

### Task 1: First slice: server, login and an empty shell, deployed

**Files:**
- Create: `wa-app/server/pyproject.toml`, `wa_app/{__init__,config,appdb,auth,api,main}.py`, `tests/{conftest,test_auth,test_appdb}.py`
- Create: `wa-app/web/` scaffold (Vite React TS), `src/styles/tokens.css` (generated), `src/styles/app.css`, `src/screens/Login.tsx`, `src/App.tsx`, `src/api.ts`, `public/` icons
- Create: `wa-app/deploy/wa-app.service`, `wa-app/README.md`
- Modify: `.github/workflows/` CI: add a `wa-app` job (uv sync, ruff, ruff format --check, mypy, pytest; pnpm install, tsc, eslint, vitest, build)

**Interfaces:**
- Produces: `Settings` (fields `host: str, port: int, data_dir: Path, store_path: Path, secure_cookies: bool`), `config.load() -> Settings` (store path from `WA_STORE_PATH`, same rule as `lib/utils.py`).
- Produces: `appdb.connect(settings) -> sqlite3.Connection` (creates schema, WAL), tables `meta(key PRIMARY KEY, value)`, `sessions(token_hash PRIMARY KEY, created_at, expires_at, user_agent)`, `login_failures(ip, at)`, `read_state(chat_jid PRIMARY KEY, read_at)`, `transcripts(message_id, chat_jid, text, language, created_at, PRIMARY KEY(message_id, chat_jid))`.
- Produces: `auth.set_passphrase(conn, passphrase: str) -> None` (min 10 chars), `auth.check_passphrase(conn, passphrase) -> bool`, `auth.new_session(conn, ua) -> str`, `auth.session_valid(conn, token) -> bool`, `auth.end_session(conn, token)`, `AuthMiddleware` (401 JSON on `/api/*` without a valid session, except `/api/auth/*` and `/api/health`).
- Produces routes: `GET /api/health` → `{"ok": true}`; `GET /api/auth/state` → `{"setup_needed": bool, "logged_in": bool}`; `POST /api/auth/setup` `{passphrase}` (only while none is set); `POST /api/auth/login` `{passphrase}`; `POST /api/auth/logout`. Static: the built `web/dist` with SPA fallback to `index.html`.
- Web produces: `api.ts` `apiFetch<T>(path, init?) : Promise<T>` that throws `ApiError {status, message}` and redirects to `/login` on 401.

- [ ] **Step 1: Write failing tests** in `tests/test_auth.py`: `test_setup_only_once` (second setup → 409), `test_login_wrong_then_right` (wrong → 401 with body `{"error":"Wrong passphrase"}`, right → 200 + `Set-Cookie` containing `HttpOnly`, `SameSite=Strict`, `Secure`), `test_rate_limit` (6th failure within 15 min → 429 even with the right passphrase), `test_api_requires_session` (`GET /api/chats` → 401), `test_origin_checked` (POST with `Origin: https://evil.example` → 403), `test_logout_kills_server_session` (old cookie replayed after logout → 401). `tests/test_appdb.py`: `test_schema_idempotent` (connect twice, no error).
- [ ] **Step 2: Run** `cd wa-app/server && uv run pytest -q` → FAIL (modules missing).
- [ ] **Step 3: Implement** config, appdb, auth, api, main. Session token: `secrets.token_urlsafe(32)`, stored as SHA-256. `secure_cookies` defaults true; `WA_APP_INSECURE_COOKIES=1` only for local http dev.
- [ ] **Step 4: Run** tests → PASS; `uv run ruff check . && uv run ruff format --check . && uv run mypy .` clean.
- [ ] **Step 5: Web scaffold.** `pnpm create vite web --template react-ts`, add Tailwind v4, generate `tokens.css` with `npx @google/design.md export ../DESIGN.md --format css-tailwind > src/styles/tokens.css`, map `-light` tokens in `app.css` under `[data-theme=light]` and `@media (prefers-color-scheme: light)`, self-host Geist (`@fontsource-variable/geist`). Login screen per DESIGN.md (body text, primary button, input), first-run variant asks the passphrase twice. Copy `docs/brand/icons/*` into `public/`; manifest `display: standalone`, `name`/`short_name` "wa-app", `theme_color` `#0b0b0c`.
- [ ] **Step 6: Playwright** `e2e/login.spec.ts` against `uv run wa-app` with a temp data dir and the fixture store: setup → login → shell shows "Chats" header; reload keeps the session; logout returns to login. Projects chromium and webkit at 375×812.
- [ ] **Step 7: Deploy the slice.** `deploy/wa-app.service` user unit (`ExecStart=%h/…/.venv/bin/wa-app`, `Environment=WA_STORE_PATH=…` placeholders, fictional paths). On the server: install it as a user unit, start it, `curl -s 127.0.0.1:8085/api/health` → `{"ok":true}`. `tailscale serve` setup is **asked of Simon first** (it changes what the tailnet sees); until then, test through an SSH tunnel.
- [ ] **Step 8: Commit** `feat(wa-app): server, login and app shell`.

### Task 2: Chats list

**Files:**
- Create: `wa_app/store.py`, `wa_app/unread.py` (count part only; Task 6 finishes it), `tests/test_store_chats.py`, `web/src/screens/ChatList.tsx`, `components/{ChatRow,FilterChips,Avatar,TabBar}.tsx`, `web/src/format.ts`, `format.test.ts`
- Modify: `api.py`

**Interfaces:**
- Consumes: `lib.naming.resolve_display_names`, `lib.changes.chat_forms`, `whatsapp._resolve_equivalent_jids` (or the identities join) for LID/phone forms.
- Produces: `store.open_ro(settings) -> sqlite3.Connection`; `store.list_chats(conn, appconn, *, filter: Literal["all","unread","favourites","groups"]="all", archived: bool=False, query: str|None=None, limit=200) -> list[ChatSummary]`.
- `ChatSummary` (dataclass → JSON): `jid, name, kind: "private"|"group"|"community_group"|"announcement"|"newsletter", avatar_url, last_message: {id, sender_name|None, from_me, preview, media_type|None, deleted, timestamp_iso, ticks|None}, unread: int, marked_unread: bool, pinned: bool, muted: bool, archived: bool, community_name|None`.
- Routes: `GET /api/chats?filter=&archived=&q=` → `{chats: ChatSummary[], archived_count: int}`.
- "Favourites" in M1 = pinned chats (no separate favourites store exists yet); recorded in DECISIONS.md.

- [ ] **Step 1: Fixture builder** in `conftest.py`: `make_store(tmp_path) -> Path` creating `messages`, `chats`, `chat_state`, `message_reactions`, `identities`, `contact_nicknames` with the columns the live schema has (copy the `CREATE TABLE` statements from `whatsapp-bridge/migrations` and `internal/database`), filled with the fictional chats of `designs/wa-app-directions/sample-data.json`, plus one person split across `111@lid` and `4915112345678@s.whatsapp.net` linked in `identities`, and one merged chat row (`merged_into` set).
- [ ] **Step 2: Failing tests:** `test_order_pinned_first_then_recent`, `test_archived_hidden_and_counted` (archived_count == number archived, not in list unless `archived=True`), `test_merged_rows_skipped`, `test_lid_and_phone_are_one_chat` (one row, name from nickname, last message = the newer of both forms), `test_filters` (unread/groups/favourites), `test_query_matches_name_with_diacritics` (`"durdica"` and `"Đurđica"` both match), `test_preview_kinds` (voice → "🎤 Voice message (0:42)", deleted → "Message deleted", sticker → "Sticker", photo → "📷 Photo", sender prefix in groups), `test_muted_when_muted_until_in_future`.
- [ ] **Step 3: Run** → FAIL. **Step 4: Implement** `store.list_chats` with SQL over the read-only connection, names via `resolve_display_names`. **Step 5: Run** → PASS.
- [ ] **Step 6: Web** ChatList at 375 and in the left pane ≥900 px: large title, search field (client filter + `q` to server), chips, archived row, rows per DESIGN.md "List row", tab bar (Chats active; Communities and Calls show "Coming in a later version" empty states; Settings works). `format.ts`: `formatListTime(iso, now): string` (today → "21:14", yesterday → "Yesterday", this week → weekday, else "12.11."), vitest cases for each plus the midnight boundary.
- [ ] **Step 7: Playwright** `e2e/chatlist.spec.ts`: pinned first, long name truncates without horizontal scroll at 320 and 375 (`document.documentElement.scrollWidth <= innerWidth`), archived opens the archived list.
- [ ] **Step 8: Commit** `feat(wa-app): chats list`.

### Task 3: Chat view (read)

**Files:**
- Modify: `store.py`, `api.py`; Create: `tests/test_store_messages.py`, `web/src/screens/Chat.tsx`, `components/{Bubble,ReactionBar,Poll,ContactCard}.tsx`

**Interfaces:**
- Produces: `store.list_messages(conn, chat_jid, *, before_ts: str|None=None, limit=60) -> list[MessageView]` (oldest→newest within the page; all JID forms of the chat merged).
- `MessageView`: `id, chat_jid, sender_jid, sender_name|None, from_me, timestamp_iso, text|None, media: {type, filename|None, duration_s|None, has_file: bool}|None, reply_to: {id, sender_name|None, preview}|None, forwarded: bool, edited: bool, deleted: bool, system: bool, reactions: [{emoji, count, mine: bool}], poll: {question, options: [{text, votes}], total}|None, contact_card: {name, phones: [str]}|None, ticks: "sent"|"delivered"|"read"|None`.
- Routes: `GET /api/chats/{jid}` → `{chat: ChatSummary, members_line: str|None}`; `GET /api/chats/{jid}/messages?before=` → `{messages: MessageView[], has_more: bool}`.

- [ ] **Step 1: Failing tests:** `test_pages_backwards` (60 per page, `has_more`), `test_merges_lid_and_phone_halves_in_time_order`, `test_reactions_summary_excludes_tombstones` (empty-emoji rows ignored, `mine` set for own reaction), `test_reply_preview_of_deleted_quoted_message` (preview "Message deleted"), `test_edited_and_forwarded_flags`, `test_poll_from_get_poll_results` (monkeypatched), `test_text_returned_raw` (`<script>` comes back as the literal string; escaping is the renderer's job), `test_contact_card_parsed` (content written by Task 9's format → `contact_card`), `test_import_messages_have_no_reply_affordance` (flag `importable: false` or equivalent: decide one field `actionable: bool`, false for `import-` ids).
- [ ] **Step 2–5:** run → FAIL, implement, run → PASS.
- [ ] **Step 6: Web** Chat screen per `a-familiar/chat.html`: header (back on phones, avatar, name, members line), day separators ("Today", "Yesterday", weekday, date), bubbles with tail only on the last of a run, sender names in groups, reply quote block, forwarded and edited labels, deleted outline bubble, reactions under the bubble, poll block, contact card block, scroll anchored at the bottom, "load older" on scroll to top keeping position. Text rendered as React text nodes only (no `dangerouslySetInnerHTML` anywhere in the app; eslint rule `react/no-danger` on). Links auto-detected and opened with `rel="noopener noreferrer"`.
- [ ] **Step 7: Playwright** `e2e/chat.spec.ts`: open "Ekipa za vikend" from the list, see every message kind; a 5,000-character message wraps with no horizontal scroll; `<b>hi</b>` shows literally.
- [ ] **Step 8: Commit** `feat(wa-app): chat view`.

### Task 4: Media, avatars and voice notes with "Show text"

**Files:**
- Create: `wa_app/media.py`, `wa_app/transcripts.py`, `tests/test_media.py`, `tests/test_transcripts.py`, `web/src/components/VoiceNote.tsx`
- Modify: `api.py`, `Bubble.tsx`, `Avatar.tsx`

**Interfaces:**
- Consumes: `whatsapp.download_media(message_id, chat_jid) -> str|None`, `whatsapp.get_profile_picture(jid, preview)`, `lib.transcribe.transcribe_file(path, language) -> dict`.
- Produces: `media.file_for(message_id, chat_jid) -> Path|None` (download_media, cached path), `media.thumbnail(path, max_px=480) -> Path` (Pillow for images/webp stickers, `ffmpeg -frames:v 1` for video, cached under `data_dir/thumbs/`), `media.avatar(jid) -> Path|None` (disk cache under `data_dir/avatars/`, TTL 24 h, negative cache 6 h).
- Produces: `transcripts.get_or_make(appconn, message_id, chat_jid) -> {text, language}|{error}` (stored in `app.db.transcripts`; never writes `messages.content`).
- Routes: `GET /api/media/{chat}/{id}` (file, correct `Content-Type`, `Content-Disposition: inline` for image/audio/video, `attachment` otherwise, `X-Content-Type-Options: nosniff`), `GET /api/media/{chat}/{id}/thumb`, `GET /api/avatar/{jid}`, `POST /api/transcripts/{chat}/{id}`.

- [ ] **Step 1: Failing tests:** `test_media_missing_returns_404_json` (download_media → None), `test_media_path_must_stay_in_store` (a path outside the store from a patched download_media → 404, never served), `test_thumbnail_cached` (second call doesn't reopen the source), `test_avatar_negative_cache`, `test_transcript_stored_once` (transcribe_file called once for two requests), `test_transcript_backend_missing` → `{"error": "No transcription backend"}` with 503.
- [ ] **Step 2–5:** run → FAIL, implement, run → PASS.
- [ ] **Step 6: Web** images and stickers via thumb then full on tap (simple full-screen viewer with close), video poster + native player, documents as a file row with size, missing file → neutral placeholder "Media not available". VoiceNote: play/pause, waveform drawn from duration (no real waveform data yet), duration, playback speed 1×/1.5×/2×, "Show text"/"Hide text" calling the transcript route with a spinner and an error line.
- [ ] **Step 7: Commit** `feat(wa-app): media, avatars, voice notes with text`.

### Task 5: Live updates over SSE

**Files:**
- Create: `wa_app/live.py`, `tests/test_live.py`, `web/src/live.ts`, `live.test.ts`
- Modify: `api.py`, `main.py` (start/stop the poller with the app lifespan)

**Interfaces:**
- Produces: `live.Poller(settings)` polling every 1.0 s: max `rowid` of `messages`, plus new `message_edits`/`deleted_at`, `message_reactions.reacted_at`, and `chat_state` `*_changed_at` since the last tick. Emits events `message` `{chat_jid, message: MessageView}`, `message_changed` `{chat_jid, id}`, `reaction` `{chat_jid, message_id}`, `chat` `{chat: ChatSummary}`.
- Route: `GET /api/live` (SSE, `retry: 3000`, heartbeat comment every 15 s, event ids = a monotonic cursor so `Last-Event-ID` resumes).
- `live.visible_clients() -> int` (clients report visibility with `POST /api/live/visibility {visible}`; M2's "app open" uses it).
- Web: `live.ts` `connectLive(onEvent): () => void` with reconnect backoff 1 s→30 s; on `visibilitychange` to visible: reconnect and invalidate the chats and open-chat queries.

- [ ] **Step 1: Failing tests:** `test_new_message_emitted_once`, `test_edit_and_delete_emitted`, `test_reaction_emitted`, `test_chat_state_change_emitted`, `test_resume_with_last_event_id_replays_missed` (keep the last 500 events in memory), `test_poll_survives_store_locked` (sqlite busy → logs and tries next tick).
- [ ] **Step 2–5:** run → FAIL, implement, run → PASS.
- [ ] **Step 6: Web** wire events into TanStack Query caches: new message appends to the open chat (rise-in per DESIGN.md Motion only when scrolled to bottom; otherwise a "New messages" pill), list reorders. vitest for the reconnect backoff and the visibility refetch.
- [ ] **Step 7: Playwright** `e2e/live.spec.ts`: insert a row into the fixture store while the chat is open → it appears within 3 s; emulate hidden → insert → visible → it appears.
- [ ] **Step 8: Commit** `feat(wa-app): live updates`.

### Task 6: Read state and unread counts that follow the phone

**Files:**
- Modify: `wa_app/unread.py`, `store.py`, `api.py`, `sending.py` (create here if Task 7 hasn't), `tests/test_unread.py`

**Interfaces:**
- Produces: `unread.read_marker(conn, appconn, chat_jid) -> datetime|None` = the later of `chat_state.last_read_at` (phone) and `app.db.read_state.read_at` (app). `unread.count(conn, appconn, chat_jid) -> int` = incoming, non-system, non-deleted messages after the marker; when there is no marker at all, `chat_state.history_unread_count` or 0. `marked_unread` passes through as a dot.
- Produces: `POST /api/chats/{jid}/read` → sets `read_state.read_at` to the newest message time, sends read receipts for the incoming unread ids through `whatsapp.mark_messages_read` (grouped per sender in groups, `import-` ids skipped, at most 50 ids per call), returns `{unread: 0}`.

- [ ] **Step 1: Failing tests:** `test_phone_read_clears_unread` (chat_state.last_read_at after the last message → 0), `test_app_read_clears_unread`, `test_marked_unread_shows_dot`, `test_no_marker_uses_history_count`, `test_receipts_grouped_by_sender_in_groups`, `test_import_ids_never_get_receipts`, `test_read_is_idempotent` (second call sends nothing).
- [ ] **Step 2–5:** run → FAIL, implement, run → PASS.
- [ ] **Step 6: Web** opening a chat (visible) calls read once the newest message is on screen; badges update from the response and from `chat` live events.
- [ ] **Step 7: Commit** `feat(wa-app): read state follows the phone`.

### Task 7: Sending: text, replies, reactions

**Files:**
- Create/modify: `wa_app/sending.py`, `tests/test_sending.py`, `web/src/components/Composer.tsx`, `Bubble.tsx` (swipe-to-reply, long-press menu), `ReactionBar.tsx`

**Interfaces:**
- Consumes: `whatsapp.send_message(recipient, message, quoted_message_id=, idempotency_key=)`, `whatsapp.send_reaction(chat_jid, message_id, emoji, idempotency_key=)`.
- Produces: `POST /api/chats/{jid}/send {text, reply_to?, client_key}` → `{ok, message_id}` or `{ok:false, error, retryable}`; `POST /api/chats/{jid}/react {message_id, emoji, client_key}` (`emoji: ""` removes). The client generates `client_key` (UUID) per compose action and reuses it on Retry; the server passes it as the idempotency key.
- Error mapping (copy fixed): 429 → "Not sent: limit"; reachout lock → "Not sent: new chats are paused for now"; allowlist 403 → "Not sent: this chat isn't allowed"; timeout/connection → "Not sent: bridge didn't answer"; text over 4,096 chars → 422 "Message too long" before calling the bridge.

- [ ] **Step 1: Failing tests:** `test_send_passes_client_key_as_idempotency_key`, `test_retry_reuses_key`, `test_error_mapping` (table-driven over the five cases), `test_reply_to_import_id_rejected`, `test_empty_text_rejected`, `test_react_remove_with_empty_emoji`.
- [ ] **Step 2–5:** run → FAIL, implement, run → PASS.
- [ ] **Step 6: Web** composer per DESIGN.md (16 px, pill, attach button disabled with "In a later version" tooltip, mic hidden in M1, send replaces mic when text exists, Enter sends on desktop, Shift+Enter newline; `visualViewport` keeps it above the keyboard). Optimistic bubble with "sending" state; failure shows the mapped line and Retry; the live `message` event with the same id replaces the optimistic one (no duplicate). Swipe right on a bubble (or a reply button on desktop hover) sets the reply quote; long press (or right click) opens reactions 👍 ❤️ 😂 😮 😢 🙏 and Reply/Copy.
- [ ] **Step 7: Playwright** `e2e/send.spec.ts` with the bridge mocked by a tiny stub server: send → optimistic → confirmed; stub 429 → "Not sent: limit" → Retry sends the same key; double-click send sends once.
- [ ] **Step 8: Commit** `feat(wa-app): send text, replies and reactions`.

### Task 8: Desktop two-pane, settings, PWA polish

**Files:**
- Modify: `App.tsx` (layout), create `screens/Settings.tsx`, `src/sw.ts`, `e2e/layout.spec.ts`

**Interfaces:**
- Layout: ≥900 px two panes (list 380 px, chat column max 760 px), <900 px one pane with the chat sliding over (DESIGN.md Motion "Chat pane"), URL `/chat/:jid` in both.
- Settings: theme (System, Dark, Light; stored in `localStorage`, applied as `data-theme`), "Log out", "Log out everywhere" (`POST /api/auth/logout-all` deletes every session), version line.
- Service worker: caches the app shell only (index, JS, CSS, fonts, icons); API and media are never cached.

- [ ] **Step 1: Playwright** `layout.spec.ts`: 1440 shows both panes; 375 shows one and back returns to the list with scroll position kept; theme switch persists over reload; both themes have no horizontal overflow at 320; reduced motion → no slide.
- [ ] **Step 2: Implement**; **Step 3: Run** all e2e + vitest → PASS.
- [ ] **Step 4: Commit** `feat(wa-app): desktop layout, settings, PWA shell`.

### Task 9: Bridge: contact cards keep their details

**Files:**
- Modify: `whatsapp-bridge/internal/whatsapp/media.go:60-66` (content for `ContactMessage` and `ContactsArrayMessage`), test in `media_test.go`; `CHANGELOG.md`

**Interfaces:**
- Produces stored content, exactly:
  `[Contact: <display name>]` followed by one line per phone in the vCard, `\n📞 <number as written in TEL>`; for a contacts array, one block per contact separated by a blank line. Task 3's `contact_card` parser reads this format.

- [ ] **Step 1: Failing table test** `TestContactCardContent`: name + two TEL lines; no TEL; name missing (falls back to the vCard FN); contacts array of two; a vCard with a malicious huge payload is capped at 20 phones.
- [ ] **Step 2–4:** `go test ./internal/whatsapp -run TestContactCardContent` → FAIL, implement (parse `TEL` lines from `contact.GetVcard()`), → PASS; `go build ./... && go vet ./... && go test ./...` clean.
- [ ] **Step 5: Commit** `feat(bridge): keep contact card numbers as text`. Deploy with the M1 release through `OTHER_BRIDGES='|' ./scripts/deploy-live.sh`.

### Task 10: Retire wa-client, docs, release

**Files:**
- Delete: `wa-client/` (decided 2026-11-21); remove its rows from README status table, CLAUDE.md architecture table and ports line (replace with `wa-app` :8085).
- Modify: `README.md` (an "App" section: what it is, screenshots from the fixture data only, install via the systemd unit, `tailscale serve` example), `SETUP.md`, `CHANGELOG.md`, `docs/app/MVP.md` (tick M1 items), `docs/progress.md` (create; updated after every task from Task 1 on).

- [ ] **Step 1:** `grep -rn "wa-client\|8084" --exclude-dir=.git .` → only CHANGELOG history and DECISIONS remain.
- [ ] **Step 2:** full checks: server pytest/ruff/mypy, web tsc/eslint/vitest/build, e2e chromium + webkit, Go tests.
- [ ] **Step 3: Commit** `chore: retire wa-client, document wa-app`.

## Milestone review (after Task 10, not per task)

Per the project skill's app-build "Verify each milestone": a fresh-context reviewer gets PRD, MVP, DESIGN.md, the M1 Done-when line and `git diff --stat design-lock..HEAD`; runs `/security-review` and reads every route for access control and input validation (session required, Origin check, path containment in media, no HTML rendering); `skill:qa` at 320–1440 including webkit; `impeccable critique` against DESIGN.md. Findings go to `docs/reviews/milestone-1.md`. Then Simon uses it on his phone for a week (Done when).

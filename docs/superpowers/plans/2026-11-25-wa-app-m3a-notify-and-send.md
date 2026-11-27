# wa-app M3a: notify and send Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The app notifies like WhatsApp does and can send everything a phone can: photos, files, voice notes, and a private reply to a group message.

**Architecture:** A `Notifier` listens to the existing poller's `message` events and sends Web Push (VAPID, `pywebpush`) to subscriptions stored in `app.db`. Media goes up as a raw request body to a new route, is written to a private temp dir and handed to the MCP library's `send_file` / `send_audio_message`, which gain caption and quote arguments. Private reply adds `quoted_chat_jid` through MCP lib → bridge `/api/send` → `ContextInfo.RemoteJID`.

**Tech Stack:** Python 3.11 Starlette (wa-app server), `pywebpush` 2.5.x (new, on PyPI since 2016, web-push-libs), React 19 + Vite PWA, Go bridge (whatsmeow), ffmpeg (already on the server).

**Spec:** `docs/app/MVP.md` (M3), `docs/app/TECH_DESIGN.md` (Notifications), `wa-app/DESIGN.md`. Ruling 2026-11-25 (Simon): notify on every incoming message with sender and text, except muted chats and the chat open in a visible app window. This supersedes the 21 Nov "drafts only" rule; record it as DECISIONS D25 in Task 7. M3 is split: this plan (M3a) is notifications, media sending and private reply; search, contacts/groups/communities, call log and polls follow in M3b.

## Global Constraints

- Every send goes through the bridge's safety checks (caps, pacing, allowlists). Never call whatsmeow or the bridge around the MCP library.
- Push payloads: `{"chat_jid", "title", "body", "tag", "url"}`, body at most 180 characters, whole JSON at most 3 KB. Never log message text or push payloads (use `logsafe`).
- Push endpoints accepted only on https hosts ending in `push.apple.com`, `fcm.googleapis.com`, `push.services.mozilla.com`, `notify.windows.com`. Anything else: 422.
- VAPID private key at `<data_dir>/vapid.pem`, mode 0600, created on first start. Subject from `WA_APP_PUSH_SUBJECT` (a `mailto:` or `https:` URL); without it push stays off and `/api/push/key` answers 503 with `"Push not configured"`.
- Upload cap: 64 MiB per file (`MAX_UPLOAD = 64 * 1024 * 1024`) on `/api/chats/{jid}/media` only. Every other route keeps the 64 KB JSON cap.
- Temp files: `tempfile.mkdtemp(prefix="wa-app-up-", dir="/tmp")` (mode 0700), removed after the send whatever the result. /tmp because the bridge allowlist always accepts it and the bridge's systemd sandbox keeps /tmp shared.
- DESIGN.md rules 1-6 apply: no WhatsApp green, one amber accent, conventional layout (rule 4), messy data in tests (rule 5), 16 px text including captions (rule 6).
- Permission prompts (notifications, microphone) only from a tap, after one line saying why.
- Tests: `cd wa-app/server && uv run pytest && uv run mypy . && uv run ruff check .`; `cd wa-app/web && pnpm test && pnpm lint && pnpm build`; bridge `cd whatsapp-bridge && go test ./...`; MCP `cd whatsapp-mcp-server && uv run --extra pro pytest`. Never `uv sync` without `--extra pro` in whatsapp-mcp-server.
- Live services: never `pkill`; restart only `systemctl --user restart wa-app`, `sudo systemctl restart whatsapp-mcp`, and the bridge via `OTHER_BRIDGES='|' ./scripts/deploy-live.sh`.

## Review Focus

1. **History sync and re-inserts must not notify.** Backfill inserts old messages one by one (under the 100-per-tick flood limit), and INSERT OR REPLACE re-inserts known ones with a new rowid. Expected: only messages whose timestamp is within 120 s of now, and each (chat, id) at most once. Tests in Task 2.
2. **The open chat on a second device.** The chat is open on the laptop but the phone is locked. Expected: no push at all while any visible client has that chat open (that is how WhatsApp Web behaves), push again as soon as it is hidden or another chat is opened. Test in Task 2.
3. **Dead subscriptions.** A reinstalled PWA leaves an old endpoint that answers 404/410. Expected: deleted on the first such answer, never retried; other errors are counted and the row is dropped after 5 consecutive failures. Test in Task 1.
4. **A huge or wrong upload.** A 200 MB video, an empty body, a filename with `../` or `č ć š ž đ`. Expected: 413 before buffering, 422 for empty, the stored name sanitised but the WhatsApp filename keeps the Croatian letters. Tests in Task 4.
5. **Private reply to a sender with no direct chat yet.** Expected: the direct chat opens empty with the quote bar, and the first send creates it; a sender the store only knows as an unmapped LID gets "Can't reply privately to this person yet", never a send to a LID. Test in Task 6.

---

### Task 1: Push spike: keys, subscriptions, test notification

The first deliverable is a test push on Simon's iPhone. If Apple refuses pywebpush's request, stop here and bring the error to Simon before Task 2.

**Files:**
- Create: `wa-app/server/wa_app/push.py`
- Modify: `wa-app/server/wa_app/appdb.py` (table), `wa-app/server/wa_app/config.py` (`push_subject`), `wa-app/server/wa_app/api.py` (routes, logout hooks), `wa-app/server/pyproject.toml` (`pywebpush>=2.5,<3`)
- Modify: `wa-app/web/src/sw.ts` (push, notificationclick), `wa-app/web/src/screens/Settings.tsx` (Notifications row)
- Create: `wa-app/web/src/push.ts`, `wa-app/web/src/push.test.ts`
- Test: `wa-app/server/tests/test_push.py`

**Interfaces:**
- Produces (server, `push.py`):
  - `ALLOWED_PUSH_HOSTS: tuple[str, ...]` (the four suffixes above)
  - `vapid_keys(settings: Settings) -> tuple[str, str]` returns (path to the private PEM, public key as URL-safe base64 of the uncompressed point)
  - `save_subscription(appconn, *, endpoint: str, p256dh: str, auth: str, session_hash: str) -> None` (upsert on endpoint)
  - `drop_subscription(appconn, endpoint: str) -> None`; `drop_for_session(appconn, session_hash: str) -> None`; `drop_all(appconn) -> None`
  - `send(settings, appconn, payload: dict) -> int` (blocking; returns how many accepted; handles 404/410 and the failure counter)
- Table: `push_subscriptions (endpoint TEXT PRIMARY KEY, p256dh TEXT NOT NULL, auth TEXT NOT NULL, session_hash TEXT NOT NULL, created_at INTEGER NOT NULL, failures INTEGER NOT NULL DEFAULT 0)`
- Routes: `GET /api/push/key` → `{"key": str}`; `POST /api/push/subscribe` body `{"endpoint", "keys": {"p256dh", "auth"}}`; `POST /api/push/unsubscribe` `{"endpoint"}`; `POST /api/push/test` → `{"sent": int}` with title "wa-app", body "Notifications work."
- Produces (web, `push.ts`): `pushSupport(): 'ok' | 'install-first' | 'unsupported'`, `enablePush(): Promise<'on' | 'denied' | 'failed'>`, `disablePush(): Promise<void>`, `pushState(): Promise<'on' | 'off' | 'denied'>`

- [ ] **Step 1: Write failing server tests** in `test_push.py`:
  - `test_subscribe_rejects_foreign_host`: POST with endpoint `https://evil.example/push` → 422; with `http://web.push.apple.com/x` → 422.
  - `test_subscribe_upserts_and_binds_session`: two POSTs with the same endpoint leave one row whose `session_hash` is the caller's.
  - `test_logout_drops_that_sessions_subscriptions` and `test_logout_all_drops_every_subscription`.
  - `test_send_drops_gone_endpoint`: patch `pywebpush.webpush` to raise `WebPushException` with a response of status 410 → row deleted, return 0.
  - `test_send_drops_after_five_failures`: status 500 five times → row gone after the fifth, present after the fourth.
  - `test_key_503_without_subject` and `test_vapid_file_is_0600`.
- [ ] **Step 2: Run** `cd wa-app/server && uv run pytest tests/test_push.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement `push.py`, the table, config and routes.** Keys via `py_vapid.Vapid().generate_keys()` / `save_key`; `webpush(subscription_info, data=json.dumps(payload), vapid_private_key=path, vapid_claims={"sub": subject}, ttl=3600, headers={"Urgency": "high"})`; run `send` in a thread from routes (`run_in_threadpool`). Wire `drop_for_session` into the existing logout and `drop_all` into logout-all in `api.py`.
- [ ] **Step 4: Run** the same pytest. Expected: PASS.
- [ ] **Step 5: Write failing web tests** in `push.test.ts`: `pushSupport()` is `'install-first'` when `PushManager` exists but `display-mode: standalone` does not match and the UA is iOS; `enablePush()` returns `'denied'` when `Notification.requestPermission` resolves `'denied'` and posts nothing.
- [ ] **Step 6: Implement** `push.ts` (subscribe with `userVisibleOnly: true`, `applicationServerKey` from `/api/push/key`), the Settings row ("Notifications": switch; under it "Add wa-app to your Home Screen first" when `install-first`; "Send a test" button when on), and the service worker handlers: `push` shows `showNotification(title, {body, tag, data: {url}})`; `notificationclick` focuses an open client and navigates it to `data.url`, else `clients.openWindow(data.url)`. Payload without a title is ignored.
- [ ] **Step 7: Run** `cd wa-app/web && pnpm test && pnpm lint && pnpm build`. Expected: PASS.
- [ ] **Step 8: Spike on real devices.** Deploy to the live app (server `uv sync`, web `pnpm build`, `systemctl --user restart wa-app`), set `WA_APP_PUSH_SUBJECT` in the shared `.env` (a mailto Simon picks; never commit it). Simon, on the iPhone home-screen app: Settings → Notifications on → "Send a test". Also test in desktop Chrome. Expected: the test notification arrives on both and tapping it opens the app. Record the result (and any Apple error body) in `docs/progress.md`.
- [ ] **Step 9: Commit** `feat(wa-app): web push subscriptions and test notification`.

### Task 2: Notify on every incoming message

**Files:**
- Create: `wa-app/server/wa_app/notify.py`, `wa-app/server/tests/test_notify.py`
- Modify: `wa-app/server/wa_app/live.py` (`Client.open_chat`, visibility body), `wa-app/server/wa_app/main.py` (register the listener), `wa-app/web/src/live.ts` (report the open chat), `wa-app/web/src/screens/AppShell.tsx` (pass the open jid)

**Interfaces:**
- Consumes: `push.send` (Task 1), `live.Event`, `Hub`, `store.get_chat` (`ChatSummary.muted`, `.name`, `.kind`), `MessageView` fields `from_me`, `system`, `timestamp_iso`, `sender_name`, `text`, `media.type`.
- Produces:
  - `Hub.chat_open(chat_jid: str) -> bool` (true when any visible client reports that chat)
  - visibility body gains `"chat": str | null`; `Hub.set_visible(client_id, visible, chat=None)`
  - `class Notifier: __init__(settings, appconn, hub, now=time.time)`, `on_events(events: list[Event]) -> None` (thread-safe, hands work to a single worker thread), `close()`
  - `payload_for(chat: ChatSummary, message: dict) -> dict | None` (pure)
- Notification text (exact copy): private chat: title = chat name, body = text. Group: title = chat name, body = `"{sender_name}: {text}"`. Media without text: `📷 Photo`, `🎥 Video`, `🎤 Voice message`, `📄 {filename or "Document"}`, `🎵 Audio`, `Sticker`; with caption: the icon then the caption. Deleted or system messages: no push. `tag` = chat jid (one notification per chat, replaced by the newest), `url` = `/chat/{encodeURIComponent(jid)}`.

- [ ] **Step 1: Write failing tests** in `test_notify.py` (fake `push.send` recording payloads, a fake hub):
  - `test_notifies_fresh_incoming_message` (private chat "Ana Kovačić", text "Stižem za 5 min" → one payload with those exact title and body)
  - `test_group_body_has_sender`
  - `test_skips_own_system_deleted`
  - `test_skips_muted_chat`
  - `test_skips_old_message`: timestamp 121 s before `now` → nothing; 119 s → one
  - `test_same_message_twice_notifies_once`
  - `test_skips_when_chat_open_in_visible_client` and `test_notifies_when_open_chat_is_hidden`
  - `test_media_bodies` (photo without caption → "📷 Photo"; document "Račun_studeni.pdf" → "📄 Račun_studeni.pdf")
  - `test_long_text_truncated_to_180_chars` (ends with "…")
  - `test_resync_event_notifies_nothing`
- [ ] **Step 2: Run** `uv run pytest tests/test_notify.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `notify.py`. Dedupe with an ordered dict of the last 2000 `(chat_jid, id)` keys. Read chat state with its own read-only store connection inside the worker thread. Register `poller.on_events.append(notifier.on_events)` in `main.create_app` and close it in lifespan shutdown.
- [ ] **Step 4: Implement the open-chat report.** `live.ts` sends `chat` with every visibility report and again when the route's jid changes; `AppShell` passes the matched jid. Server test in `test_live.py`: `test_visibility_records_open_chat`.
- [ ] **Step 5: Run** server and web test suites. Expected: PASS.
- [ ] **Step 6: Commit** `feat(wa-app): notify on incoming messages`.

### Task 3: Bridge and MCP: captions, quotes on media, quoting across chats

**Files:**
- Modify: `whatsapp-bridge/internal/whatsapp/messages.go` (`SendOptions.QuotedChatJID`), `whatsapp-bridge/internal/api/` send handler (decode `quoted_chat_jid`), `docs/REFERENCE.md` if it lists `/api/send` fields
- Modify: `whatsapp-mcp-server/whatsapp.py` (`send_message(..., quoted_chat_jid=None)`, `send_file(..., caption=None, quoted_message_id=None)`, `send_audio_message(..., quoted_message_id=None)`)
- Modify: `wa-app/server/wa_app/mcp_lib.py` (pass-throughs `send_file`, `send_audio_message`, the new kwarg on `send_message`)
- Test: bridge `internal/whatsapp/messages_test.go`; `whatsapp-mcp-server/tests/test_send_media.py` (or the existing send test file)

**Interfaces:**
- Bridge `/api/send` JSON gains `quoted_chat_jid` (optional). When set and different from the recipient, the quote's `ContextInfo.RemoteJID` is that chat and the participant is the quoted message's sender in that chat; the quoted message is looked up by id **and** chat.
- MCP payload: `send_file` puts `caption` in `"message"` and `quoted_message_id` in the payload; `send_audio_message` adds `quoted_message_id`.
- `mcp_lib.send_file(recipient: str, *, file_path: str, filename: str, caption: str | None, quoted_message_id: str | None, quoted_chat_jid: str | None, idempotency_key: str) -> dict`; `mcp_lib.send_audio_message(recipient: str, *, file_path: str, quoted_message_id: str | None, idempotency_key: str) -> dict`

- [ ] **Step 1: Failing Go test** `TestQuoteAcrossChatsSetsRemoteJID`: a stored group message from sender S, send to S's DM with `QuotedChatJID` = the group → built message has `ContextInfo.RemoteJID` = group JID, `Participant` = S, `StanzaID` = the id. Plus `TestQuoteSameChatHasNoRemoteJID` (unchanged behaviour).
- [ ] **Step 2: Run** `go test ./internal/whatsapp/ -run Quote -v`. Expected: FAIL.
- [ ] **Step 3: Implement** in the quote block of the send path; add a store lookup by (id, chat) and use it when `QuotedChatJID` is set.
- [ ] **Step 4: Failing Python tests**: `test_send_file_sends_caption_and_quote` (payload has `message` and `quoted_message_id`), `test_send_message_passes_quoted_chat_jid`, `test_send_audio_passes_quote`.
- [ ] **Step 5: Implement**, run `go test ./...` and `uv run --extra pro pytest`. Expected: PASS.
- [ ] **Step 6: Deploy the bridge** with `OTHER_BRIDGES='|' ./scripts/deploy-live.sh`, restart the MCP (`sudo systemctl restart whatsapp-mcp`), check `/api/health` connected. CHANGELOG entry.
- [ ] **Step 7: Commit** `feat(bridge,mcp): captions and quotes on media, quoting across chats`.

### Task 4: Send photos and files

**Files:**
- Modify: `wa-app/server/wa_app/sending.py` (`send_media`), `wa-app/server/wa_app/api.py` (route), `wa-app/server/wa_app/auth.py` (`body_limit` for the media route)
- Create: `wa-app/web/src/components/AttachSheet.tsx` (+ test), modify `Composer.tsx`, `screens/Chat.tsx`, `api.ts`
- Test: `wa-app/server/tests/test_sending_media.py`

**Interfaces:**
- Consumes: `mcp_lib.send_file` (Task 3), `valid_key`, `_recipient_for`, `map_failure`, `Outcome`.
- Route: `POST /api/chats/{jid}/media`, raw body = the file. Headers: `Content-Type`, `X-Filename` (percent-encoded UTF-8), `X-Client-Key` (UUID), optional `X-Caption` (percent-encoded, at most 1024 characters), `X-Reply-To`, `X-Kind: file|voice`. Answer as `chat_send`: `{"ok": true, "message_id"}` or the mapped failure.
- `sending.send_media(settings, appconn, jid: str, *, data: bytes, filename: str, caption: str | None, reply_to: str | None, client_key: str, kind: Literal["file", "voice"]) -> Outcome`
- `safe_filename(name: str) -> str`: strips path parts and control characters, keeps letters with diacritics, max 120 characters, `"file"` when empty.

- [ ] **Step 1: Failing server tests**: `test_media_route_413_over_64_mib` (declared Content-Length 64 MiB + 1 → 413 with no read), `test_media_route_empty_body_422`, `test_safe_filename` (`"../../etc/passwd"` → `"passwd"`; `"Račun čćšžđ.pdf"` unchanged), `test_send_media_writes_private_temp_and_removes_it` (dir mode 0700 under /tmp, gone after success and after failure), `test_send_media_passes_caption_and_quote` (fake `mcp_lib.send_file` sees them), `test_other_routes_keep_64kb_cap`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the route (stream the body into the temp file, counting bytes), `send_media`, `safe_filename`, and `body_limit`.
- [ ] **Step 4: Run** server suite. Expected: PASS.
- [ ] **Step 5: Web.** Composer gets a paperclip button left of the box (conventional placement, DESIGN rule 4), opening a hidden `<input type="file">` (any type; on phones the system sheet offers camera and photos). Picking opens `AttachSheet`: image preview (or a file row with name and size for anything else), caption field (16 px), Send. Over 64 MiB: the sheet says "Too big to send (max 64 MB)" and Send is disabled. While uploading, the message shows in the chat as pending with a progress line (XHR `upload.onprogress`); failure shows Retry like text sends. Tests: `AttachSheet.test.tsx` (`shows the size limit message for a 65 MB file`, `sends caption and reply id`), long Croatian filename renders without overflow.
- [ ] **Step 6: Run** web suite and build. Expected: PASS.
- [ ] **Step 7: Live check:** send a photo with a caption and a PDF to Simon's own chat ("Message yourself") from the app; they arrive on the phone with caption and correct names.
- [ ] **Step 8: Commit** `feat(wa-app): send photos and files`.

### Task 5: Record and send voice notes

**Files:**
- Create: `wa-app/web/src/useRecorder.ts` (+ test), `wa-app/web/src/components/RecordBar.tsx` (+ test)
- Modify: `Composer.tsx`, `screens/Chat.tsx`, `api.ts`; server only reuses Task 4 with `X-Kind: voice` (`send_media` calls `mcp_lib.send_audio_message`, which converts to Ogg Opus)
- Test: `wa-app/server/tests/test_sending_media.py::test_voice_kind_uses_audio_send`

**Interfaces:**
- `useRecorder(): { state: 'idle' | 'asking' | 'recording' | 'stopped' | 'denied', seconds: number, start(): Promise<void>, stop(): void, cancel(): void, blob: Blob | null }` using `MediaRecorder` with the first supported of `audio/webm;codecs=opus`, `audio/mp4`. Hard stop at 15 minutes.
- Composer: with an empty box the send button is a mic button (WhatsApp convention). Tap mic → RecordBar replaces the box: red dot, timer, trash (cancel), stop. After stop: play/pause preview with duration, trash, Send. Denied permission: inline "Microphone access is off. Allow it in Settings to record." Nothing is uploaded before Send.

- [ ] **Step 1: Failing tests**: `useRecorder` picks `audio/mp4` when webm is unsupported; cancel discards the blob; `RecordBar` shows the timer as `0:07`; server `test_voice_kind_uses_audio_send` (calls `send_audio_message`, not `send_file`, with the reply id).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** suites and build. Expected: PASS.
- [ ] **Step 5: Live check on the iPhone home-screen app:** record 5 s, preview, send to "Message yourself"; the phone shows it as a voice note (waveform, not a file) and it plays.
- [ ] **Step 6: Commit** `feat(wa-app): record and send voice notes`.

### Task 6: Reply privately to a group message

**Files:**
- Modify: `wa-app/web/src/components/MessageMenu.tsx`, `screens/Chat.tsx`, `outbox.ts` (carry `reply_chat`), `api.ts`
- Modify: `wa-app/server/wa_app/sending.py` (`send_text` and `send_media` accept `reply_chat`), `wa-app/server/wa_app/store.py` (`direct_chat_for(ro, appconn, sender_jid: str) -> ChatSummary | None`, a placeholder summary when the person has no chat row yet)
- Modify: `wa-app/server/wa_app/api.py` (`GET /api/chats/{jid}` serves the placeholder for a known person)
- Test: `wa-app/server/tests/test_private_reply.py`, `wa-app/web/src/screens/Chat.private.test.tsx`

**Interfaces:**
- Consumes: `mcp_lib.send_message(..., quoted_chat_jid=)` (Task 3).
- Send body gains `"reply_chat": str` (the group jid). The server checks the quoted message exists in that chat, is actionable and is not from me, and that the recipient is the quoted sender's direct chat; otherwise 422 "That message can't be replied to privately".
- Menu item "Reply privately" on others' messages in groups only. It navigates to `/chat/{sender direct jid}?reply={id}&from={group jid}`; the chat screen opens with the quote bar labelled with the group name.

- [ ] **Step 1: Failing server tests**: `test_private_reply_sends_quote_with_group` (fake `send_message` gets `quoted_chat_jid` = group); `test_private_reply_rejects_own_message`; `test_private_reply_rejects_other_recipient`; `test_direct_chat_placeholder_for_known_person_without_chat`; `test_unmapped_lid_has_no_direct_chat` (→ 422 "Can't reply privately to this person yet").
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** server side.
- [ ] **Step 4: Failing web test**: from a group message menu, "Reply privately" opens the sender's chat with a quote bar reading "{sender} · {group name}" and the send carries `reply_chat`.
- [ ] **Step 5: Implement** web side, run both suites and build. Expected: PASS.
- [ ] **Step 6: Live check:** reply privately to a message in a group Simon picks; the recipient's phone shows the quote with the group name.
- [ ] **Step 7: Commit** `feat(wa-app): reply privately to group messages`.

### Task 7: Docs, deploy, milestone review

**Files:**
- Modify: `DECISIONS.md` (D25: the notification ruling and the M3 split), `docs/app/TECH_DESIGN.md` (Notifications section), `docs/app/MVP.md` (tick M3a items), `wa-app/README.md` (push setup, `WA_APP_PUSH_SUBJECT`, upload limit), `.env.example`, `CHANGELOG.md`, `docs/progress.md`
- Create: `docs/reviews/milestone-3a.md`

- [ ] **Step 1:** Update the docs above. `.env.example` lists `WA_APP_PUSH_SUBJECT=` with no value.
- [ ] **Step 2:** Full test suites (server, web, bridge, MCP) green; deploy (`uv sync` in wa-app/server, `pnpm build`, `systemctl --user restart wa-app`; health 200, no errors in `journalctl --user -u wa-app`).
- [ ] **Step 3:** Fresh-context review: Codex (or an Opus reviewer) on `git diff main...HEAD`, `/security-review` (new routes: push subscribe, raw upload), `skill:qa` at 320-430 px on the composer, attach sheet, record bar and Settings. Findings in `docs/reviews/milestone-3a.md`; at most three fix rounds.
- [ ] **Step 4:** PR, CI green, merge, deploy again from main.
- [ ] **Step 5:** Done when Simon has had a day of real notifications on his iPhone and sent a photo, a file, a voice note and a private reply from the app.

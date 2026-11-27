# M3b: Find and Organise Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The WhatsApp features the app still lacks for daily use, first batch: message search, custom lists with a "Mentions and replies" filter, message actions (edit, delete for everyone, star, forward), chat actions (pin, mute, archive, mark unread), quick replies, and a per-chat AI mode sheet (Off / Suggest).

**Architecture:** Most of it is wiring what the bridge already does into wa-app (edit, revoke, pin, mute, archive have bridge routes and `whatsapp.py` wrappers). The bridge gains four small things: star (appstate `BuildStar` + `events.Star` + table), mark unread (`BuildMarkChatAsRead`), forwarded sends (`ContextInfo.IsForwarded`) and stored mentions (`ContextInfo.MentionedJID` → new column). Search is an FTS5 index in wa-app's own app.db (the bridge db is read-only to the app), filled by a backfill and the live poller. Lists and quick replies are app-local (app.db).

**Tech Stack:** Go (whatsmeow) bridge, Python 3.12 / Starlette / SQLite FTS5, React + TypeScript + Vitest.

**Spec:** this document. Sources: the feature gap list agreed with Simon on 2026-11-27 (DECISIONS.md D26), the bridge capability inventory of the same day, and WhatsApp Business's Meta Business Agent per-chat sheet (My reply / Suggestions / AI agent), which the AI mode sheet mirrors.

**Order:** after M3a Tasks 2-7 (notifications on messages, photos and files, voice, private reply). M3c (polls, contact and group info, Communities, catch-me-up summaries, translation) and M3d (call log, status viewing, Auto mode replies) get their own plans.

## Global Constraints

- Branch per task series from `origin/main` (`feat/m3b`), worktree `~/.cache/wa-tools/wt/m3b`. Never commit on main.
- Public repo: no real numbers, JIDs, names, hosts, IPs, emails or personal paths in tracked files; tests use the fixture JIDs.
- UI copy: plain language, sentence case, no em dashes.
- Every new app route goes through the session + Origin middleware. Nothing that sends goes through `/api/agent/*`.
- Every send (edit, revoke, forward, quick reply) goes through the existing send path with its idempotency key; nothing here sends without a tap.
- Bridge deploy: `OTHER_BRIDGES='|' ./scripts/deploy-live.sh`. Bridge migrations are additive only (`ALTER TABLE ... ADD COLUMN` / `CREATE TABLE IF NOT EXISTS`), following `internal/database/store.go runMigrations`.
- Limits copied from WhatsApp: edit own text messages up to 15 minutes after sending; delete for everyone own messages up to 48 hours; forward to at most 5 chats at once; quick reply shortcut 1-24 chars `[a-z0-9_-]`, text max 4096; list name max 40 chars, at most 20 lists.
- Search: FTS5 with `tokenize = "unicode61 remove_diacritics 2"` (Croatian: "cesto" finds "često"); results newest first, 30 per page; text messages, captions and stored voice transcripts are indexed; local-only chats are indexed too (the index never leaves the server).
- Server checks: `cd wa-app/server && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy .`; web: `cd wa-app/web && pnpm lint && pnpm exec tsc -b && pnpm test && pnpm build`; bridge: `cd whatsapp-bridge && go build ./... && go vet ./... && go test ./... && test -z "$(gofmt -l .)"`; MCP: `cd whatsapp-mcp-server && uv run pytest -q && uv run ruff check .`.

## Review Focus

1. **Edited or deleted messages in search.** An edit replaces the indexed text; a revoke removes the row from the index; results never show text the sender deleted.
2. **Acting on the wrong message form.** Star, edit, revoke and forward resolve the chat and sender JID forms (phone vs LID) the same way the existing send and react paths do; a message from a LID sender in a group can be starred and forwarded.
3. **Time limits at the edge.** Edit at 14:59 after sending works, at 15:01 the menu item is gone and the server answers 409 even if the client sends it anyway; same for delete at 48 h.
4. **Forward of media that was never downloaded.** Forwarding a photo the server has no local copy of downloads it first (same path as viewing); a download failure reports per target chat, never a silent partial forward.
5. **List and filter state after a chat changes form or is archived.** List membership keys on the canonical chat JID (`store.canonical`), so a chat that moves from phone JID to LID stays in its lists; archived chats still show in a list filter.

---

### Task 1: Bridge additions: star, mark unread, forwarded sends, stored mentions

**Files:**
- Modify: `whatsapp-bridge/internal/database/store.go` (migrations), new `whatsapp-bridge/internal/database/stars.go` (+ test)
- Modify: `whatsapp-bridge/internal/whatsapp/chat_state.go` (handle `events.Star`), `client.go` (`StarMessage`, `MarkChatUnread`), `handlers.go` + `media.go` (`ExtractReplyContext` also returns mentioned JIDs; store them)
- Modify: `whatsapp-bridge/internal/api/server.go`, `handlers.go`, `internal/types` (`SendMessageRequest.Forwarded bool`)
- Modify: `whatsapp-mcp-server/whatsapp.py` (+ tests): `star_message`, `mark_chat_unread`, `send_message(..., forwarded=False)`, `send_file(..., forwarded=False)`
- Test: Go tests next to each file; `whatsapp-mcp-server/tests/`

**Interfaces:**
- Produces (db): table `starred_messages(chat_jid TEXT, message_id TEXT, starred_at TIMESTAMP, PRIMARY KEY(chat_jid, message_id))`; column `messages.mentioned_jids TEXT` (JSON array of bare user JIDs, NULL when none).
- Produces (HTTP): `POST /api/star {chat_jid, message_id, sender_jid?, from_me, star: bool}`; `POST /api/mark-unread {chat_jid, unread: bool}`; `/api/send` accepts `"forwarded": true` and sets `ContextInfo.IsForwarded` (and `ForwardingScore` 1).
- Produces (Python): `star_message(chat_jid, message_id, star=True, sender_jid=None, from_me=False) -> dict`, `mark_chat_unread(chat_jid, unread=True) -> dict`, `forwarded` kwarg on `send_message` / `send_file`.

- [ ] Failing tests: migration adds the column and table; `events.Star` with `Starred=true/false` upserts/deletes the row; star from this device writes the row before the appstate echo; incoming message with `ContextInfo.MentionedJID` stores the JSON array (bare JIDs, LID kept as LID); forwarded send sets the flag in the built proto; Python wrappers post the right JSON.
- [ ] Implement; run bridge and MCP checks; commit `feat(bridge): star, mark unread, forwarded sends and stored mentions`.

### Task 2: Message search

**Files:**
- Create: `wa-app/server/wa_app/search.py` (+ `tests/test_search.py`)
- Modify: `wa-app/server/wa_app/appdb.py` (FTS5 table + cursor in `meta`), `main.py` (backfill task + poller hook), `api.py` (routes)
- Create: `wa-app/web/src/screens/Search.tsx` (+ test); Modify: `ChatList.tsx` (search field opens it), `Chat.tsx` (in-chat search bar, jump to message), `api.ts`, `api-types.ts`

**Interfaces:**
- Produces: `CREATE VIRTUAL TABLE msg_fts USING fts5(body, chat_jid UNINDEXED, message_id UNINDEXED, ts UNINDEXED, tokenize="unicode61 remove_diacritics 2")`.
- Produces: `index_messages(appconn, ro, *, since_rowid: int | None) -> int` (backfill in batches of 2000, resumable via meta key `search_rowid`), `on_events(events)` (insert new, replace edited, delete revoked), `search(appconn, ro, query: str, *, chat_jid: str | None, before: str | None, limit=30) -> list[Hit]` where `Hit = {chat_jid, chat_name, message_id, ts, sender_name, snippet}` (snippet via FTS5 `snippet()` with `[` `]` markers, escaped client-side).
- Produces routes: `GET /api/search?q=&chat=&before=` → `{"hits": [...], "next": before|null}`; `GET /api/chats/{jid}/messages?around={id}` (window of 30 each side, for jump-to-message).
- Query handling: user text is tokenised and each token quoted for FTS5 (no raw FTS syntax from the user); 1-char queries return nothing; prefix match on the last token.

- [ ] Failing tests: diacritics ("cesto" finds "često"), prefix, phrase-safety (input `"` `*` `NEAR(` does not error), edit replaces text, revoke removes, transcript text indexed, chat filter, pagination, backfill resumes after a restart, `around` returns the target in the window.
- [ ] Web tests: results list groups by chat with highlighted snippet; tapping opens the chat scrolled to and briefly highlighting the message; in-chat search steps up/down through hits.
- [ ] Implement; checks; commit `feat(wa-app): message search`.

### Task 3: Lists and the Mentions and replies filter

**Files:**
- Create: `wa-app/server/wa_app/lists.py` (+ test); Modify: `appdb.py`, `api.py`, `store.py` (filter by list; mentions/replies predicate using Task 1's `mentioned_jids` and existing `quoted_*`)
- Create: `wa-app/web/src/screens/Lists.tsx` (+ test); Modify: `FilterChips.tsx` (chips from server: All, Unread, Favourites, Groups, Mentions, then custom lists in saved order), `ChatMenu.tsx` ("Add to list"), `Settings.tsx` (entry "Lists")

**Interfaces:**
- Produces (app.db): `lists(id INTEGER PK, name TEXT NOT NULL, position INTEGER NOT NULL)`, `list_chats(list_id, chat_jid, PRIMARY KEY(list_id, chat_jid))` keyed on canonical JIDs.
- Produces routes: `GET/POST /api/lists`, `PUT/DELETE /api/lists/{id}` (rename, reorder via `position`), `PUT /api/lists/{id}/chats` `{add: [...], remove: [...]}`; `GET /api/chats?filter=list:{id}|mentions|...`.
- Mentions filter: unread incoming group messages where `mentioned_jids` contains any own JID form, or that quote one of your messages, or `@all`/`@everyone` mentions (WhatsApp's definition).

- [ ] Failing tests: CRUD + limits (40 chars, 20 lists, duplicate name 409), membership survives a phone→LID form change, archived chat appears in its list, mentions predicate (direct mention, reply to you, @all, not a mention of someone else), chips order.
- [ ] Implement; checks; commit `feat(wa-app): custom lists and mentions filter`.

### Task 4: Message actions

**Files:**
- Modify: `wa-app/server/wa_app/sending.py` (edit, revoke, forward), `api.py`, `mcp_lib.py` (pass-throughs for `edit_message`, `delete_message`, `star_message`, forwarded sends)
- Create: `wa-app/web/src/components/ForwardSheet.tsx`, `wa-app/web/src/screens/Starred.tsx` (+ tests); Modify: `MessageMenu.tsx`, `Bubble.tsx` (edited label, forwarded label, star mark), `Composer.tsx` (edit mode)

**Interfaces:**
- Routes: `POST /api/chats/{jid}/messages/{id}/edit {text, client_key}`, `POST .../revoke {client_key}`, `POST .../star {star: bool}`, `POST /api/forward {from_chat, message_ids: [...], to: [...], client_key}` (≤5 targets, ≤10 messages), `GET /api/starred?before=`.
- Edit/revoke only for own messages within the Global Constraints limits (server enforces, 409 `{"error": "too_late"}`); revoke in groups where you are admin is out of scope.
- Forward: text re-sent with `forwarded: true`; media downloaded through the existing media path then sent with M3a Task 4's media send and `forwarded: true`; result per target `{to, ok, error?}`.

- [ ] Failing tests: limits at the edges (Review Focus 3), LID sender star, forward partial failure reporting, media forward downloads first, starred list order, edit updates the bubble live (SSE event), revoked bubble shows "You deleted this message".
- [ ] Implement; checks; commit `feat(wa-app): edit, delete for everyone, star and forward`.

### Task 5: Chat actions

**Files:**
- Modify: `wa-app/server/wa_app/api.py`, `mcp_lib.py` (pin/mute/archive/mark unread pass-throughs), `whatsapp-mcp-server/main.py` (expose `pin_chat`, `mute_chat`, `archive_chat`, `mark_chat_unread` as tools in the `chats` toolset)
- Modify: `wa-app/web/src/components/ChatMenu.tsx`, `ChatRow.tsx` (swipe or long-press menu), `screens/Chat.tsx` (header menu)

**Interfaces:**
- Route: `POST /api/chats/{jid}/state {pin?: bool, mute?: "8h"|"1w"|"forever"|false, archive?: bool, unread?: bool}` → the new chat summary; at most 3 pinned chats (WhatsApp's limit), 409 beyond.
- The chat list updates from the bridge's `chat_state` echo; the route returns optimistic state.

- [ ] Failing tests: each action calls the right wrapper, pin limit, mute options map to bridge durations, muted chats stop notifying (M3a notifier reads the same state), mark unread shows the dot.
- [ ] Implement; checks; commit `feat(wa-app): pin, mute, archive and mark unread`.

### Task 6: Quick replies and the AI mode sheet

**Files:**
- Create: `wa-app/server/wa_app/quick_replies.py` (+ test); Modify: `appdb.py`, `api.py`
- Create: `wa-app/web/src/components/QuickReplyPicker.tsx` (+ test); Modify: `Composer.tsx` (typing `/` at the start opens the picker, filtered as you type; choosing inserts the text, it never sends), `Settings.tsx` (Quick replies section), `ChatAiSheet.tsx` (mode selector)

**Interfaces:**
- app.db `quick_replies(shortcut TEXT PRIMARY KEY, text TEXT NOT NULL, used INTEGER DEFAULT 0)`; routes `GET/POST /api/quick-replies`, `PUT/DELETE /api/quick-replies/{shortcut}`; picker sorts by `used` then shortcut.
- AI mode sheet: three rows like WhatsApp Business: "My reply" (No AI help), "Suggestions" (AI drafts, you send), "Auto" (shown disabled with "Coming later"). Maps onto the existing `chat_ai.enabled`; no new server state.

- [ ] Failing tests: shortcut validation, picker filtering and insertion, usage count, mode sheet reads and writes `enabled`, Auto row not selectable.
- [ ] Implement; checks; commit `feat(wa-app): quick replies and AI mode sheet`.

### Task 7: Docs, review, ship

- [ ] README sections (search, lists, actions, quick replies), MVP.md M3 checklist, DECISIONS.md, CHANGELOG.
- [ ] Final whole-branch review (most capable model), one fix wave.
- [ ] PR; CI green; merge with Simon's OK; deploy bridge (`OTHER_BRIDGES='|' ./scripts/deploy-live.sh`) then app; run the search backfill on the live store and report its time and index size.

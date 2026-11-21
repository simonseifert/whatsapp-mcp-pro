# wa-app M0: Bridge Groundwork Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Go bridge store what the chat app needs to read straight from
`messages.db`: reactions linked to their message, pinned/muted/archived/read state
per chat, community metadata, and stickers as media. Also fix the failing security
scan.

**Architecture:** Every change is additive. New tables and columns come with numbered
SQL files in `whatsapp-bridge/migrations/` and Go mirrors run at startup (the
existing `runMigrations` pattern). Live whatsmeow events and history sync both
write to them, and a timestamp guard keeps an older replay from overwriting a newer
live value. No new REST endpoints: the app reads the store read-only.

**Tech Stack:** Go (whatsmeow, mattn/go-sqlite3), table-driven `go test`, SQLite.

**Spec:** `docs/app/TECH_DESIGN.md` (section "Gaps in the bridge" and "Calls made
after the builder check") and `docs/app/MVP.md` (M0).

## Global Constraints

- Go toolchain `go1.27.4` (fixes GO-2026-6859). `go` directive stays `1.26.0`.
- Upstream's 2 commits (`0afd380`, `be73e8c`) are merged first, as their own commit,
  before any M0 change.
- Migrations are numbered from `011`. Each SQL file has the repo's header (Target,
  Date, Backward compatible) and a Go mirror that is idempotent (`CREATE ... IF NOT
  EXISTS`, ALTERs tolerated via the duplicate-column check).
- Backward compatible: existing columns, rows and MCP tool output keep working.
  Reaction messages are still stored as `[Reaction: X]` rows as today; the new table
  is in addition.
- Chat JIDs are normalised with `c.NormalizeChatJID(...)`, senders with
  `StoredSender(...)`, exactly as `HandleMessage` does.
- Public repo: no phone numbers, IPs, host names or personal paths in code, tests,
  docs or commit messages. Test JIDs use `123@s.whatsapp.net`-style fakes.
- Deploy only with `OTHER_BRIDGES='|' ./scripts/deploy-live.sh`. Never touch the
  second bridge (`whatsapp-bridge-friday`).
- Never force-push `main`. Work on branch `feat/m0-bridge-groundwork`.

## Review Focus

1. **Reaction removed or changed:** an empty reaction text removes that sender's
   reaction; a new emoji from the same sender replaces the old one. (Task 3 tests.)
2. **Out-of-order replay:** history sync or a full app-state sync delivers an older
   pin/mute/archive/reaction after a newer live one; the newer value must survive.
   (Tasks 3 and 4 tests.)
3. **Reaction before its target:** in history sync a reaction can arrive before the
   message it points at, or for a message never stored. It is stored anyway (no
   foreign key) and the summary is correct once the message exists. (Task 3 test.)
4. **Same person, two JIDs:** a reaction from a sender's LID and later from their
   phone-number JID must count as one sender after `StoredSender`. (Task 3 test.)
5. **Running on the live store twice:** every migration runs cleanly on a store that
   already has it, and on a store from before M0. (Task 2 test, repeated per task.)

---

### Task 1: Merge upstream and bump the Go toolchain

**Files:**
- Modify: `whatsapp-bridge/go.mod`, `whatsapp-bridge/go.sum`, `.github/workflows/*.yml`
  (via the merge), `CHANGELOG.md`

**Interfaces:**
- Produces: a green `go test ./...`, `go vet ./...` and `govulncheck ./...` on
  `go1.27.4`.

- [ ] **Step 1: Branch and merge upstream**

```bash
git checkout main && git pull --ff-only
git checkout -b feat/m0-bridge-groundwork
git fetch upstream && git merge --no-ff upstream/main -m "Merge upstream (group rename #103, govulncheck #102)"
```
On conflicts keep this fork's versions of dependencies where they are newer, and
upstream's logic changes (`be73e8c` group rename). Expected: merge commit created.

- [ ] **Step 2: Run the suite on the merged tree**

Run: `cd whatsapp-bridge && go build ./... && go test ./...`
Expected: PASS. Fix only what the merge broke.

- [ ] **Step 3: Bump the toolchain**

Set `toolchain go1.27.4` in `whatsapp-bridge/go.mod`; set the workflows' Go version
to `1.27.4` (or `1.27.x` where upstream already uses that form).

- [ ] **Step 4: Verify**

Run: `cd whatsapp-bridge && go version && go test ./... && go run golang.org/x/vuln/cmd/govulncheck@latest ./...`
Expected: `go1.27.4`, tests PASS, govulncheck "No vulnerabilities found".

- [ ] **Step 5: Commit**

```bash
git add whatsapp-bridge/go.mod whatsapp-bridge/go.sum .github/workflows CHANGELOG.md
git commit -m "build: Go toolchain 1.27.4 (fixes GO-2026-6859 in net/http)"
```
CHANGELOG under `[Unreleased] / Security`.

---

### Task 2: Migration harness test for M0

**Files:**
- Create: `whatsapp-bridge/internal/database/m0_migrations_test.go`

**Interfaces:**
- Consumes: `newTestMessageStore(t)` (`messages_test.go`), `runMigrations(db)`.
- Produces: `assertTableColumns(t *testing.T, db *sql.DB, table string, want ...string)`
  used by Tasks 3 to 5 to add one line each.

- [ ] **Step 1: Write the test**

`TestM0MigrationsIdempotent`: build a store with `newTestMessageStore`, call
`runMigrations` a second time, expect no error. `assertTableColumns` reads
`PRAGMA table_info(<table>)` and fails listing any missing column. The test starts
with no M0 assertions; each later task appends its own.

- [ ] **Step 2: Run**

Run: `cd whatsapp-bridge && go test ./internal/database -run TestM0MigrationsIdempotent -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add whatsapp-bridge/internal/database/m0_migrations_test.go
git commit -m "test: migration idempotency harness for M0"
```

---

### Task 3: Reactions linked to their message (migration 011)

**Files:**
- Create: `whatsapp-bridge/migrations/011_message_reactions.sql`
- Create: `whatsapp-bridge/internal/database/reactions.go`, `reactions_test.go`
- Create: `whatsapp-bridge/internal/whatsapp/reactions.go`, `reactions_test.go`
- Modify: `whatsapp-bridge/internal/database/store.go` (`runMigrations` calls
  `createReactionsTable`), `internal/database/messages.go:338` (replace the stub),
  `internal/whatsapp/handlers.go` (`HandleMessage` and `HandleHistorySync`),
  the `/api/reaction` handler in `internal/api/handlers.go` (store own reactions sent
  through the bridge), `m0_migrations_test.go`

**Interfaces:**
- Produces (database):
  - `type Reaction struct { ChatJID, TargetID, Sender, Emoji string; At time.Time }`
  - `func (s *MessageStore) StoreReaction(r Reaction) error`: empty `Emoji` deletes
    the row for (chat, target, sender); a row whose stored `At` is newer is never
    overwritten or deleted by an older event.
  - `func (s *MessageStore) GetMessageReactionSummary(chatJID, msgID string) (map[string]int, error)`
    (existing signature, now real).
- Produces (whatsapp):
  - `func reactionFromMessage(chatJID, sender string, msg *events.Message) (database.Reaction, bool)`
  - `func reactionsFromHistory(chatJID string, wmi *waWeb.WebMessageInfo) []database.Reaction`
    (from `wmi.GetReactions()`, plus a `ReactionMessage` inside `wmi.Message`).

Schema (011 and the Go mirror, identical):
```sql
CREATE TABLE IF NOT EXISTS message_reactions (
    chat_jid   TEXT NOT NULL,
    message_id TEXT NOT NULL,      -- the message reacted to
    sender     TEXT NOT NULL,      -- StoredSender form
    emoji      TEXT NOT NULL,
    reacted_at TIMESTAMP NOT NULL,
    PRIMARY KEY (chat_jid, message_id, sender)
);
CREATE INDEX IF NOT EXISTS idx_message_reactions_msg ON message_reactions(chat_jid, message_id);
```

- [ ] **Step 1: Failing database tests** (`reactions_test.go`, table-driven)
  - add 👍 from A, ❤️ from B on `M1` → summary `{"👍":1,"❤️":1}`
  - A changes to 😂 → `{"😂":1,"❤️":1}`
  - A sends empty emoji → `{"❤️":1}`
  - older event (At earlier than stored) for B with "👎" → still `{"❤️":1}`
  - older empty emoji for B → still `{"❤️":1}`
  - reaction on `M9` with no such message → stored; summary for `M9` is `{"👍":1}`
  - unknown message with no reactions → empty map, no error
- [ ] **Step 2: Run, expect FAIL** (`go test ./internal/database -run Reaction -v`)
- [ ] **Step 3: Implement** migration file, `createReactionsTable(db *sql.DB) error`,
  `StoreReaction` (single `INSERT ... ON CONFLICT DO UPDATE ... WHERE excluded.reacted_at >= reacted_at`;
  delete as `DELETE ... WHERE reacted_at <= ?`), real summary (`GROUP BY emoji`).
  Append `assertTableColumns(t, db, "message_reactions", "chat_jid","message_id","sender","emoji","reacted_at")`
  to the Task 2 test.
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Failing whatsapp tests** (`reactions_test.go`)
  - `reactionFromMessage` on a message with `ReactionMessage{Key.ID:"M1", Text:"👍", SenderTimestampMS: t}`
    → `Reaction{TargetID:"M1", Emoji:"👍", At:t}`, ok
  - empty text → ok with `Emoji:""` (a removal)
  - a non-reaction message → not ok
  - `reactionsFromHistory` with two `Reaction` entries on a `WebMessageInfo` → two
    reactions targeting that message's key ID, senders passed through `StoredSender`
    (LID and PN of the same fake person end up equal when the store's LID map has
    them; use the existing sender-normalisation test fixture from `senders_test.go`)
- [ ] **Step 6: Run, expect FAIL**, then implement, then PASS.
- [ ] **Step 7: Wire it in**
  - `HandleMessage`: before `ExtractTextContent`, if `reactionFromMessage` is ok,
    call `StoreReaction`. Keep storing the `[Reaction: X]` row as today (when the
    emoji is non-empty) so existing tools see no change.
  - `HandleHistorySync`: for each `msg.Message`, store `reactionsFromHistory(...)`.
  - `/api/reaction`: after a successful send, `StoreReaction` with the own JID as
    sender (whatsmeow does not echo own sends back as events).
- [ ] **Step 8: Run the full suite** `go test ./...` → PASS
- [ ] **Step 9: Commit** `feat(bridge): store reactions linked to their message (migration 011)`

---

### Task 4: Chat state: pinned, muted, archived, read (migration 012)

**Files:**
- Create: `whatsapp-bridge/migrations/012_chat_state.sql`
- Create: `whatsapp-bridge/internal/database/chat_state.go`, `chat_state_test.go`
- Create: `whatsapp-bridge/internal/whatsapp/chat_state.go`, `chat_state_test.go`
- Modify: `internal/database/store.go`, `main.go:133` event switch,
  `internal/whatsapp/handlers.go` (`HandleHistorySync`), `m0_migrations_test.go`

**Interfaces:**
- Produces (database):
  - `type ChatState struct { ChatJID string; Pinned, Archived, MarkedUnread bool; MutedUntil int64; LastReadAt *time.Time; HistoryUnreadCount *int }`
  - `func (s *MessageStore) SetChatPinned(chatJID string, pinned bool, at time.Time) error`
  - `func (s *MessageStore) SetChatMuted(chatJID string, mutedUntil int64, at time.Time) error`
    (`mutedUntil` unix seconds; `-1` = forever; `0` = not muted)
  - `func (s *MessageStore) SetChatArchived(chatJID string, archived bool, at time.Time) error`
  - `func (s *MessageStore) SetChatMarkedUnread(chatJID string, unread bool, at time.Time) error`
  - `func (s *MessageStore) MarkChatReadAt(chatJID string, at time.Time) error`
    (only moves forward; also clears `marked_unread` when `at` is newer than its change)
  - `func (s *MessageStore) SeedChatStateFromHistory(chatJID string, pinned, archived, markedUnread bool, mutedUntil int64, unreadCount int, latestMsg time.Time) error`
    (fills only fields that have never been set; if `unreadCount == 0` it also sets
    `last_read_at = latestMsg`)
  - `func (s *MessageStore) GetChatState(chatJID string) (ChatState, error)`
- Produces (whatsapp):
  - `func (c *Client) HandleChatStateEvent(store *database.MessageStore, evt any)` for
    `*events.Pin`, `*events.Mute`, `*events.Archive`, `*events.MarkChatAsRead`,
    `*events.Receipt`
  - `func ownReadReceipt(r *events.Receipt) (at time.Time, ok bool)`: true when
    `r.IsFromMe` and `r.Type` is `types.ReceiptTypeRead` or `types.ReceiptTypeReadSelf`

Schema: table `chat_state` with `chat_jid TEXT PRIMARY KEY` and, per field, a value
column plus a `<field>_changed_at TIMESTAMP`: `pinned`, `muted_until`, `archived`,
`marked_unread`; plus `last_read_at TIMESTAMP` and `history_unread_count INTEGER`.

Note: `EmitAppStateEventsOnFullSync` is off in this bridge, so a fresh pairing
gets no Pin/Mute/Archive events for existing state. History sync's `Conversation`
fields (`GetPinned`, `GetArchived`, `GetMuteEndTime`, `GetUnreadCount`,
`GetMarkedAsUnread`) seed it instead. Do not turn that flag on: it would replay
every app-state action (contacts too) through existing handlers.

- [ ] **Step 1: Failing database tests** (table-driven)
  - pin at t2, then a history seed saying unpinned → still pinned
  - pin at t2, then unpin event at t1 (older) → still pinned; unpin at t3 → unpinned
  - mute until −1 → `MutedUntil == -1`; unmute (0) at a later time → 0
  - `MarkChatReadAt(t5)` then `MarkChatReadAt(t4)` → `LastReadAt == t5`
  - marked unread at t6, then read at t7 → `MarkedUnread == false`; read at t5 → stays true
  - seed with `unreadCount 0` → `LastReadAt == latestMsg`; seed with 3 → `LastReadAt` nil, `HistoryUnreadCount == 3`
  - unknown chat → zero `ChatState`, no error
- [ ] **Step 2: Run, FAIL. Step 3: implement** (each setter one upsert guarded by
  `WHERE excluded.<field>_changed_at >= <field>_changed_at OR <field>_changed_at IS NULL`).
  Append the `assertTableColumns` line. **Step 4: PASS.**
- [ ] **Step 5: Failing whatsapp tests**
  - `ownReadReceipt`: IsFromMe + read → ok; IsFromMe + read-self → ok; not from me → not ok; IsFromMe + delivered → not ok
  - `HandleChatStateEvent` with `*events.Pin{Action: Pinned true}` → `GetChatState` pinned; same for Mute (assert the unit of `MuteAction.MuteEndTimestamp` against whatsmeow's proto comment and convert to seconds in one helper `muteEndSeconds(int64) int64`), Archive, MarkChatAsRead(read=false → marked unread, read=true → `MarkChatReadAt(evt.Timestamp)`)
  - own read receipt in chat `G@g.us` → `LastReadAt == receipt.Timestamp`
- [ ] **Step 6: FAIL, implement, PASS.**
- [ ] **Step 7: Wire it in:** add the five event types to the `main.go` switch,
  calling `client.HandleChatStateEvent(messageStore, evt)`; in `HandleHistorySync`
  call `SeedChatStateFromHistory` per conversation (convert `GetMuteEndTime` with the
  same helper; `GetPinned() > 0` means pinned).
- [ ] **Step 8: `go test ./...` PASS. Step 9: Commit**
  `feat(bridge): store pinned, muted, archived and read state per chat (migration 012)`

---

### Task 5: Community and group metadata (migration 013)

**Files:**
- Create: `whatsapp-bridge/migrations/013_group_community.sql`
- Modify: `internal/database/identities.go` (struct fields, upsert column list),
  `internal/database/store.go` (`runMigrations` ALTERs),
  `internal/whatsapp/identity.go:240` (`enrichGroups`), tests in
  `internal/whatsapp/identity_community_test.go`, `m0_migrations_test.go`

**Interfaces:**
- Produces: `Identity` gains `IsCommunity bool`, `CommunityJID string`,
  `IsDefaultSubgroup bool`, `IAmAdmin bool`; columns `is_community`, `community_jid`,
  `is_default_subgroup`, `i_am_admin` on `identities`.
- Produces: `func communityFields(g *types.GroupInfo, own ...types.JID) (isCommunity bool, communityJID string, isDefaultSubgroup bool, iAmAdmin bool)`
  (`g.IsParent`, `g.LinkedParentJID`, `g.IsDefaultSubGroup`, and admin when a
  participant matching any `own` JID, PN or LID, non-AD, has `IsAdmin || IsSuperAdmin`).

- [ ] **Step 1: Failing test** `TestCommunityFields`, table-driven:
  parent community → `(true, "", false, x)`; linked subgroup → `(false, "P@g.us", false, x)`;
  default announcement subgroup → `IsDefaultSubgroup true`; own LID listed as admin →
  `iAmAdmin true`; own PN listed as plain member → `false`; empty participants → `false`.
- [ ] **Step 2: FAIL. Step 3: implement** `communityFields`; the 013 ALTERs in the SQL
  file and as `runMigrations` entries; upsert writes the four columns; `enrichGroups`
  fills them, with own JIDs from `c.Store.ID` and `c.Store.LID`.
  Append the `assertTableColumns(..., "identities", "is_community", "community_jid", "is_default_subgroup", "i_am_admin")` line.
- [ ] **Step 4: `go test ./...` PASS. Step 5: Commit**
  `feat(bridge): store community links and own admin status (migration 013)`

---

### Task 6: Stickers as downloadable media

**Files:**
- Modify: `internal/whatsapp/media.go` (`ExtractMediaInfo`), `internal/whatsapp/download.go:82`
  (media-type switch), `internal/whatsapp/media_test.go`, `download_test.go`

**Interfaces:**
- Produces: `ExtractMediaInfo` returns `mediaType "sticker"`, filename
  `sticker_<20060102_150405>.webp` for `StickerMessage`; the download switch maps
  `"sticker"` to `whatsmeow.MediaImage`. Content stays `[Sticker]`.

- [ ] **Step 1: Failing tests:** `ExtractMediaInfo` on a `StickerMessage` with URL,
  DirectPath, MediaKey, hashes and length returns all of them and type `sticker`;
  the download type helper maps `sticker` → `whatsmeow.MediaImage`; an unknown type
  still errors as before.
- [ ] **Step 2: FAIL. Step 3: implement. Step 4: `go test ./...` PASS** (media sweep
  and retry queue tests included: a sticker row must behave like an image row).
- [ ] **Step 5: Commit** `feat(bridge): download stickers as media`

---

### Task 7: Docs, deploy and live check

**Files:**
- Modify: `CHANGELOG.md`, `docs/DATABASE_DESIGN.md`, `ARCHITECTURE.md` (only if it
  lists tables)

- [ ] **Step 1: Docs.** CHANGELOG `[Unreleased] / Added`: one line each for
  reactions table, chat state, community columns, stickers. DATABASE_DESIGN: the
  three schema additions and the "newer change wins" rule.
- [ ] **Step 2: Full local check**

Run: `cd whatsapp-bridge && go vet ./... && go test ./...` and
`cd ../whatsapp-mcp-server && uv run pytest -q`
Expected: all PASS (the Python side is untouched; this proves it).

- [ ] **Step 3: Commit** `docs: M0 schema additions`
- [ ] **Step 4: Push and open a PR** against `main`; wait for CI (tests, lint,
  Security Scan) to be green. Merge only with Simon's ok.
- [ ] **Step 5: Deploy** after merge: `OTHER_BRIDGES='|' ./scripts/deploy-live.sh`.
  Check the bridge reconnects (`/api/health` or the service log).
- [ ] **Step 6: Live check** with a read-only connection to the live store, printing
  counts only (never message content or JIDs):
  - `SELECT COUNT(*) FROM message_reactions` grows after a test reaction from the phone
  - `SELECT COUNT(*) FROM chat_state WHERE pinned=1` matches the phone's pinned chats
    after pinning/unpinning one chat on the phone
  - `last_read_at` moves for a chat after reading it on the phone
  - `SELECT COUNT(*) FROM identities WHERE is_community=1` > 0 after the next identity rebuild
  - a sticker received after deploy has a file on disk
  - MCP `list_messages` and `get_chat` still return as before

**Done when (from MVP.md):** the MCP tools and existing consumers still work, and
the new tables fill on the live store.

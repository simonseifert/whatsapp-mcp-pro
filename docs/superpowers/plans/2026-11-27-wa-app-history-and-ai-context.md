# History and AI Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A user who just installed wa-app can see how much of their WhatsApp history is stored, pull older history with one tap, and get drafts that sound like them: more context per draft and an app-wide "About me" note.

**Architecture:** wa-app reads coverage straight from the bridge's `messages.db` (read-only, as everywhere else). One-tap fill is a small in-process queue in wa-app that starts the bridge's existing per-chat backfill job (`POST /api/history/backfill`) one chat at a time and polls its status (`GET /api/history/backfill`), through `mcp_lib` pass-throughs to `whatsapp.start_history_backfill` / `history_backfill_status`. Draft context settings live in app.db `meta` and feed `build_draft_prompt`.

**Tech Stack:** Python 3.12 / Starlette / SQLite (server), React + TypeScript + Vitest (web). No bridge (Go) change.

**Spec:** this document. Origin: Nirmal's fresh install drafts sounded generic; diagnosis 2026-11-27 in the session: the draft prompt only sees 30 messages, up to 30 of the user's own replies in that chat, the chat note and learned rules; a freshly linked bridge often has thin history, so there is nothing to imitate.

## Global Constraints

- Branch `feat/history-context`, stacked on `feat/m3a-notify-send` (5470862). Worktree `~/.cache/wa-tools/wt/history`. Never commit on main.
- Public repo: no phone numbers, JIDs of real people, host names, IPs, emails or personal paths in tracked files, tests included (use the fixture JIDs in `tests/conftest.py`).
- UI copy: plain language, sentence case, no em dashes.
- Every state-changing route goes through the existing session + Origin middleware (`auth.py`); nothing new under `/api/agent/`.
- Backfills run **one chat at a time**. Never start a second bridge job while one is running from this queue.
- Settings bounds: `context_messages` 10..100, default 30; `own_examples` 0..100, default 30; `about_me` max 1500 chars, default empty.
- Thin chat: fewer than 30 of the user's own text messages stored (`THIN_OWN = 30`).
- Fill depths: `"year"` → `until` = today minus 365 days, `max_batches` 200; `"3y"` → today minus 1095 days, 200; `"all"` → no `until`, 200.
- Fill scope: the 50 most recently active chats (`FILL_CHATS = 50`), skipping `status@broadcast`, `@newsletter` and `@broadcast` JIDs; scope `"thin"` keeps only thin ones, `"all"` keeps all 50.
- Server checks: `cd wa-app/server && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy .`
- Web checks: `cd wa-app/web && pnpm lint && pnpm exec tsc -b && pnpm test && pnpm build`

## Review Focus

1. **Bridge down or phone offline mid-fill.** A start call that fails or a job that ends with `stopped_by == "error"` marks that chat failed with the error text and the queue moves on; the queue never stalls on a job that stays "running" forever (per-chat watchdog 30 min).
2. **A chat with several JID forms (phone and LID).** Coverage merges forms into one row (`store._forms_of`); the backfill targets the form with the most stored messages, not whatever form the URL had.
3. **A chat with zero stored messages.** The bridge cannot anchor a request (`no messages stored`); coverage shows 0 and fill skips it with reason "nothing to anchor on" instead of calling the bridge.
4. **Server restart while a fill runs.** The queue is in memory; after a restart status shows idle, and pressing Fill again is safe (the bridge answers 409 for a chat it is still backfilling; treat 409 as "already running", wait for it, then continue).
5. **Hostile or huge About me.** Over 1500 chars is a 422; the text goes in a fence like the chat note (`clean_line`), never into the fixed system prompt, so it cannot close the fence or forge `⟦you⟧`.

---

### Task 1: Draft context settings and About me

**Files:**
- Create: `wa-app/server/wa_app/ai/context.py`
- Modify: `wa-app/server/wa_app/ai/prompt.py` (`_own_replies`, `build_draft_prompt`)
- Modify: `wa-app/server/wa_app/ai/drafts.py:~626` (pass settings)
- Modify: `wa-app/server/wa_app/api.py` (route `/api/ai/context`)
- Modify: `wa-app/web/src/api.ts`, `wa-app/web/src/api-types.ts`, `wa-app/web/src/screens/Settings.tsx`
- Test: `wa-app/server/tests/test_ai_context.py`, `wa-app/server/tests/test_ai_prompt.py`, `wa-app/web/src/screens/Settings.context.test.tsx`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class DraftContext: context_messages: int; own_examples: int; about_me: str`
- Produces: `def get_context(conn: sqlite3.Connection) -> DraftContext` (defaults when unset; reads meta keys `ai_context_messages`, `ai_own_examples`, `ai_about_me`)
- Produces: `def save_context(conn: sqlite3.Connection, body: dict) -> DraftContext` (partial update; raises `ai_config.ConfigError` subclass with a message on bad input)
- Produces: `build_draft_prompt(..., chat_ai, *, n_messages: int = 30, own_examples: int = 30, about_me: str = "") -> Prompt`
- Produces: `GET /api/ai/context` → `{"context": {"context_messages", "own_examples", "about_me"}}`; `PUT` same body (partial) → same shape; 422 `{"error": ...}` on bad input.
- Produces (web): `fetchDraftContext(): Promise<DraftContext>`, `saveDraftContext(patch: Partial<DraftContext>): Promise<DraftContext>`

- [ ] **Step 1: Write failing server tests** in `tests/test_ai_context.py`:
  - `test_defaults`: fresh app db → `DraftContext(30, 30, "")`.
  - `test_save_partial`: `save_context(conn, {"own_examples": 80})` → `own_examples == 80`, others default; then `{"about_me": "Simon, 27"}` keeps 80.
  - `test_bounds`: `context_messages` 9 and 101, `own_examples` -1 and 101, `about_me` of 1501 chars, a bool `True` for an int field, a non-string about_me → each raises.
  - `test_route_roundtrip(logged_in)`: PUT `{"context_messages": 60, "about_me": "x"}` → 200 with those values; GET returns them; PUT `{"context_messages": 500}` → 422.
  - In `test_ai_prompt.py`: `test_own_examples_limit` (`own_examples=2` → exactly 2 own lines in the examples fence; `own_examples=0` → no "The user's own replies" header), `test_about_me_fenced` (`about_me="I write lowercase"` → `chat_system` contains "```about" and the text; `fixed_system` does not), `test_about_me_hostile` (about_me with "```" and "⟦you⟧" → fence not closed early, label brackets stripped, same assertions style as the existing hostile-rules test).
- [ ] **Step 2: Run** `uv run pytest tests/test_ai_context.py tests/test_ai_prompt.py -q` → FAIL (module missing / unexpected kwarg).
- [ ] **Step 3: Implement** `ai/context.py`; add `own_examples` to `_own_replies(ro, chat_jid, limit)`; in `build_draft_prompt` put the about block **first** in `chat_parts` as ```` ```about\nAbout the user (written by the user):\n{clean_line(about_me, 1500)}\n``` ```` only when non-blank; route in `api.py` next to `/api/ai/default` using `body_of`, `appdb.db_lock`, `ai_failure`. In `drafts.py` `_prepare`, read `context.get_context(self._appconn)` under the same `db_lock` block and pass `n_messages=ctx.context_messages, own_examples=ctx.own_examples, about_me=ctx.about_me`.
- [ ] **Step 4: Run** the server tests → PASS.
- [ ] **Step 5: Web.** In `Settings.tsx` add `DraftContextSection` inside the AI area (after `<AiSection />`): label "Draft context"; a textarea "About me" (placeholder "Who you are and how you write. Used for every draft.", counter `n/1500`, maxLength 1500); two range inputs with visible values: "Messages a draft reads" (10..100, step 10) and "Your past replies it learns from" (0..100, step 10); one Save button, disabled while unchanged or saving; inline error text from the 422. Test `Settings.context.test.tsx` (mock `apiFetch` as `Settings.ai.test.tsx` does): loads values into the fields; changing a slider enables Save; Save sends only changed fields; a 422 shows its error.
- [ ] **Step 6: Run** web checks → PASS.
- [ ] **Step 7: Commit** `feat(wa-app): draft context settings and About me note`

### Task 2: History coverage

**Files:**
- Create: `wa-app/server/wa_app/history.py`
- Modify: `wa-app/server/wa_app/api.py` (route `/api/history/coverage`)
- Test: `wa-app/server/tests/test_history_coverage.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class ChatCoverage: jid: str; name: str; kind: str; messages: int; own_texts: int; oldest: str | None; last_active: str | None; thin: bool; backfill_jid: str | None` (`backfill_jid` = the stored form with the most messages, None when 0 messages)
- Produces: `def coverage(ro: sqlite3.Connection, appconn: sqlite3.Connection, *, limit: int = FILL_CHATS) -> list[ChatCoverage]` most recently active first, skipping broadcast/status/newsletter JIDs.
- Produces: `def totals(ro: sqlite3.Connection) -> dict` → `{"messages": int, "chats": int, "oldest": str | None}`
- Produces: constants `THIN_OWN = 30`, `FILL_CHATS = 50`.
- Produces: `GET /api/history/coverage` → `{"totals": {...}, "chats": [asdict(ChatCoverage), ...], "thin_threshold": 30}`.

- [ ] **Step 1: Write failing tests** using `make_store` fixtures:
  - `test_counts_and_oldest`: a fixture chat → `messages`, `own_texts` (own, no media, non-empty, not poll/contact, not deleted: the same filter as `prompt._own_replies`), `oldest` = earliest timestamp ISO.
  - `test_forms_merged`: a chat stored under both a phone JID and its LID → one row, counts summed, `backfill_jid` = the form with more rows.
  - `test_thin_flag`: 29 own texts → thin; 30 → not thin.
  - `test_skips_broadcast`: `status@broadcast` and a `@newsletter` chat are absent.
  - `test_empty_chat`: chat row with no messages → `messages == 0`, `backfill_jid is None`, thin.
  - `test_route(logged_in)`: 200, keys present, `thin_threshold == 30`; logged-out client → 401.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `history.coverage` on top of `store.list_chats` (for names, kind, last activity) and one aggregate query per chat over `store._forms_of` forms; share the own-text filter with `prompt._own_replies` by moving it into a `store` helper `own_text_filter() -> str` used by both.
- [ ] **Step 4: Run** → PASS, plus full server checks.
- [ ] **Step 5: Commit** `feat(wa-app): history coverage per chat`

### Task 3: Backfill queue

**Files:**
- Modify: `wa-app/server/wa_app/history.py` (queue)
- Modify: `wa-app/server/wa_app/mcp_lib.py` (pass-throughs + `__all__`)
- Modify: `wa-app/server/wa_app/api.py` (routes), `wa-app/server/wa_app/main.py` (start/stop with the app lifespan, like the draft worker)
- Test: `wa-app/server/tests/test_history_queue.py`, `wa-app/server/tests/test_mcp_lib.py`

**Interfaces:**
- Consumes: `ChatCoverage.backfill_jid`, `coverage()`, `THIN_OWN`, `FILL_CHATS` (Task 2).
- Produces (`mcp_lib`): `start_history_backfill(chat_jid: str, until: str | None, max_batches: int) -> dict`, `history_backfill_status() -> dict` (thin wrappers over `whatsapp.*`).
- Produces: `class BackfillQueue` with `__init__(self, start: Callable[[str, str | None, int], dict], status: Callable[[], dict], *, poll_s: float = 10.0, watchdog_s: float = 1800.0, today: Callable[[], date] = date.today)`; `enqueue(self, items: list[tuple[str, str]], depth: Literal["year", "3y", "all"]) -> int` (items are `(jid, backfill_jid)`; dedupes against queued and running; returns how many were added); `cancel(self) -> None` (drops the queue; a running bridge job finishes on its own); `snapshot(self) -> dict`; `async run(self) -> None` (loop; started by the app lifespan); `stop(self)`.
- Produces: `snapshot()` shape `{"running": {"jid", "batches", "oldest"} | None, "queued": [jid, ...], "done": [{"jid", "result": "done" | "more" | "failed" | "skipped", "detail": str, "oldest": str | None}]}` (`done` keeps the last 100). Result mapping from bridge `stopped_by`: "phone has nothing older" or "reached until" → `done`; "max batches" → `more`; "error" → `failed` with the bridge error; no `backfill_jid` → `skipped` "nothing to anchor on".
- Produces routes: `POST /api/history/fill` `{"scope": "thin" | "all", "depth": "year" | "3y" | "all"}` → `{"added": n, "status": snapshot}`; `POST /api/chats/{jid}/history` `{"depth"}` → same; `GET /api/history/status` → snapshot; `POST /api/history/cancel` → snapshot. Bad scope/depth → 422; unknown chat → 404.

- [ ] **Step 1: Write failing tests** with fake `start`/`status` callables and `poll_s=0` (drive `run` with `asyncio` and a fake clock or a bounded number of iterations):
  - `test_one_at_a_time`: enqueue 3 → `start` called for the second only after the fake status reports the first `running: False`.
  - `test_depth_mapping`: fixed `today=date(2026, 11, 27)`; "year" → `until="2025-11-27"`, 200; "3y" → `"2023-11-28"`, 200; "all" → `None`, 200.
  - `test_result_mapping`: each `stopped_by` above → the right `result`.
  - `test_start_failure_moves_on`: `start` returns `{"success": False, "error": "Request error: ..."}` → `failed`, next chat starts.
  - `test_conflict_waits`: `start` returns success False with a `job` that is running (bridge 409) → waits for that job, then records its result.
  - `test_watchdog`: status keeps `running: True` past `watchdog_s` → `failed` "timed out", next starts.
  - `test_dedupe_and_cancel`: enqueuing a queued jid adds 0; `cancel()` empties `queued`, keeps `running`.
  - `test_no_anchor_skipped`: item with `backfill_jid=None` → `skipped`, `start` never called.
  - Routes (`logged_in`, queue swapped for one with fakes): fill with scope "thin" enqueues only thin chats from coverage; bad depth → 422; unknown chat → 404; POST without Origin → rejected by the existing middleware (same assertion style as other state-changing route tests).
  - `test_mcp_lib`: the two pass-throughs exist in `__all__` and call `whatsapp.start_history_backfill` / `history_backfill_status` (monkeypatch).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Blocking bridge calls go through `asyncio.to_thread`. Log one line per chat result without message content (`logsafe` conventions).
- [ ] **Step 4: Run** → PASS, plus full server checks.
- [ ] **Step 5: Commit** `feat(wa-app): one-at-a-time history backfill queue`

### Task 4: History screen and per-chat button

**Files:**
- Create: `wa-app/web/src/components/HistorySection.tsx`, `wa-app/web/src/components/HistorySection.test.tsx`
- Modify: `wa-app/web/src/api.ts`, `wa-app/web/src/api-types.ts`, `wa-app/web/src/screens/Settings.tsx`, `wa-app/web/src/components/ChatAiSheet.tsx` (+ its test)
- Modify docs: `wa-app/README.md` (new "History and draft context" section), `SETUP.md` (first-link history: `HISTORY_SYNC_DAYS_LIMIT` default 365, raise before linking for more; the phone decides how much it sends)

**Interfaces:**
- Consumes: the Task 2 and Task 3 routes and shapes.
- Produces (web): `fetchCoverage()`, `fetchHistoryStatus()`, `fillHistory(scope, depth)`, `loadChatHistory(jid, depth)`, `cancelHistory()`.

- [ ] **Step 1: Write failing tests** (`HistorySection.test.tsx`, mocked `apiFetch`):
  - Renders totals ("12,345 messages in 87 chats, oldest Mar 2019") and the thin count ("9 chats have too little of your writing for good drafts").
  - The chat list shows name, message count, own count and oldest month; thin rows carry a "Thin" tag.
  - Fill: a depth segmented control ("Last year" / "3 years" / "Everything", default "Last year") and a button "Fill thin chats" (and a text button "Fill all 50"); pressing it first shows the confirmation "Your phone will show a 'Finished syncing' notice for each batch. Keep it online and charging." with Continue / Cancel; Continue calls `fillHistory("thin", "year")`.
  - While running: polls status every 5 s, shows "Filling {name}: {batches} batches, back to {oldest month}" and "{n} waiting", a Stop button calling `cancelHistory`; after the run, results per chat ("Done", "More available, run again", "Failed: {detail}", "Skipped: nothing to anchor on") and coverage refreshes.
  - `ChatAiSheet`: a "Load older messages" button calls `loadChatHistory(jid, "all")` and shows "Queued" / the running line for that chat.
- [ ] **Step 2: Run** `pnpm test` → FAIL.
- [ ] **Step 3: Implement**; mount `<HistorySection />` in `Settings.tsx` directly above the AI section, label "History". Polling stops when the section unmounts and while `document.hidden`.
- [ ] **Step 4: Run** web checks and server checks → PASS.
- [ ] **Step 5: Docs** as listed above; the README section also says how to check history by hand (open an old chat and scroll up) and what "thin" means.
- [ ] **Step 6: Commit** `feat(wa-app): history screen, one-tap fill and per-chat load`

### Task 5: Live check and ship

- [ ] **Step 1:** Final whole-branch review (most capable model), fix loop.
- [ ] **Step 2:** Deploy to the live app from the branch the same way as M3a Task 1 (`uv sync`, `pnpm build`, `systemctl --user restart wa-app`); health 200.
- [ ] **Step 3:** On Simon's app: Settings → History shows plausible totals; run "Load older messages" on one chat; confirm new older messages appear and the result line says Done or More available.
- [ ] **Step 4:** Record in `docs/progress.md`; PR (base `feat/m3a-notify-send` until M3a merges, then retarget to main).

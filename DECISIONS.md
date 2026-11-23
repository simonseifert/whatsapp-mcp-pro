# Decisions

## D1 — 2026-10-21 — Review fixes deployed (Claude, approved by Simon)
A three-part review (bridge, MCP server, add-ons and ops) produced a prioritized list.
These were fixed, tested and deployed the same day:
- the message upsert;
- recall's UTC date filters;
- loading the recall model from the local cache;
- `deploy-live.sh`: test before swapping the binary, roll back on failure;
- untracking the macOS binaries.

The rest of the list is open in BRIEF.md.

## D2 — 2026-10-21 — wa-client retired, not moved (Simon)
It had shown data frozen at 2026-08-25 since the code/data split, with no login on
the tailnet, and nobody noticed. Simon: "if we need it then move to debian". The
evidence said it isn't needed, so it was retired:
- its LaunchAgent was renamed `.disabled-20261021`;
- bridge webhook 2 was disabled, not deleted.

To revive it: rename the plist back, re-enable the webhook, and first fix its store path and add auth.

## D3 — 2026-10-21 — MEDIA_RETRY_ENABLED=true (Simon)
Expired media is re-requested from the phone. Each retry shows "Finished syncing" on the
phone, which is acceptable because auto-transcription only looks back 3 days, so nothing
retries in bulk. Tested: the request round-trips, but it only recovers media the phone
still has.

## D4 — 2026-10-21 — Ops docs live outside this repo (Claude)
The ops runbook (machines, units, consumers, failover) went into Simon's private infra
folder, not `docs/`, because this repo is public and the runbook names hosts, IPs and the
ntfy topic.

## D5 — 2026-10-22 — Fix-first batch from the full review deployed (Simon: "Deploy all")
Source: `docs/reviews/2026-10-21-full-review.md`, kept local and untracked because it
names hosts and paths.

Code fixes, 14 commits:
- **Media guard:** secret and database files are refused in both layers (including
  `.db-*` sidecars and the top level of any `store/`, matched case-insensitively).
  `send_audio_message` validates its source file before converting.
- **Reconnects:** use `ResetConnection()`, so auto-reconnect and the watchdog stay armed.
- **Media collisions:** saved files are checked against their hash, and a colliding
  file gets the message ID added to its name.
- **StreamReplaced** exits with code 3.
- **Merged LID chats:** every chat-JID entry follows `merged_into`.
- **Timestamp filters** compare UTC instants.
- **`check_inbox`:** cursor writes are atomic and under a lock.

Live config:
- `MEDIA_ALLOWED_DIRS` dropped `~/Code` and gained `~/wa-outbox`. Downloads, Pictures,
  Documents and attachments stay, because the denylist now covers secrets inside them.
  The strict option (attachments + outbox only) was offered and declined.
- Both bridge units got `RestartPreventExitStatus=3` via drop-ins. Friday's unit also
  got the DNS wait, `ExecStartPre=wa-bridge-fix.sh`.
- `friday-warm` stopped and taken out of the failover and recover scripts.

## D6 — 2026-10-22 — Multiple numbers: one process per number, no template rename (Simon: "if you think that's the best way")
Approved as "template units per number". The template rename was dropped because
`whatsapp-bridge.service` is referenced by:
- the health timer, deploy, failover, the M1 recovery script and friday-core;
- the MCP unit's dependencies.

Renaming all of that is risk without payoff at two numbers. The goal itself was
delivered by making everything around the units treat every number alike:
- one health check (`wa-bridge-health.sh`, with ntfy alerts and backoff), which
  replaced the separate Friday timer;
- `deploy-live.sh` restarts and verifies every bridge and rolls them back together;
- `standby-sync` snapshots every number;
- an "add a number" runbook in Simon's private ops doc.

Also done:
- **Fence:** :8082 is limited to loopback and the tailnet (a Docker container
  could reach it before).
- **Key rotation:** the bridge API key was rotated after it turned up in a vault
  note, and the note was redacted.

## D7 — 2026-10-22 — Public-repo privacy scrub, forward only (Simon: no history rewrite)
Real phone numbers, LIDs, a tailnet IP and personal paths were replaced in tracked
code, tests and comments with obvious fakes (`4915112345678`, `123456789012345@lid`,
`100.64.0.10`, `/home/alice`). Git history is not rewritten. The handoff note and
`.coverage` were untracked; `docs/reviews/` and `docs/handoff-*.md` are gitignored.
Checked again 2026-10-23: no tracked file holds a number from the live contact
store; the deploy.env example below still named a real home path and now uses
`/home/alice`. History kept 8 real numbers (Simon's own among them) in old
commits; D12 rewrote it.

**Action for Simon before the next deploy:** `deploy-live.sh` no longer hard-codes the
Friday bridge. It reads other bridges from `$OTHER_BRIDGES` or, if unset, from
`~/.local/share/whatsapp-mcp-pro/deploy.env`. Create that file with:

```
OTHER_BRIDGES=friday|whatsapp-bridge-friday|8083|/home/alice/.local/share/whatsapp-friday/.env
```

One `OTHER_BRIDGES=` line per extra bridge. The file is parsed, not sourced, so the
line needs no quoting. Without it the script still deploys the main bridge, but it
won't restart or verify Friday's.

## D8 — 2026-10-22 — Group C features shipped as v0.5.0-pro (Simon: "keep going until finished")
Phone-offline detection, persistent media-retry queue, idempotent sends with
distinct HTTP codes, reachout-timelock (463) handling, allowlist parity for
reactions/polls/edits/deletes, contact naming with provenance, and opt-in OAuth.
Built by five parallel agents; the automated security review found a real issue in
almost every batch, and each was fixed before merge. A fresh-context review against
a copy of the live store found no blockers. Two of its should-fixes were changed
before deploy:
- **Duplicate-send keys:** a content-derived key would have swallowed deliberate
  repeats (a second "ok"). Keys are now random per call, and the MCP retries a
  timed-out request once with the same key.
- **Media sends:** they get a 150s budget instead of 25s.

Also decided:
- **463 on a LID recipient** doesn't arm the timelock (it usually means no phone
  mapping).
- **The bridge ignores agent-inferred nicknames.**
- **OAuth stays disabled live.** Turning it on requires exposing the MCP on a public
  HTTPS URL, which is Simon's call.

Deployed and verified live:
- the new `/api/connection` fields;
- `/api/media-requests`;
- all four new tables;
- a real duplicate send to the self-chat, which was deduplicated;
- `list_unnamed_contacts` on live data.

## D9 — 2026-10-22 — OAuth tried for real, exposed via Tailscale Funnel (Simon: "we can try just for fun")
The opt-in OAuth is enabled on the live MCP and published on Tailscale Funnel at
port 8443 only, so claude.ai or ChatGPT can add it as a remote connector. The
operator approves each client on the consent page and approves read-only by default.
Static tokens are unchanged. The README demo GIF was recorded on fictional data, and
the M1 standby was brought to v0.5.0-pro after its Xcode licence was accepted. How
to connect, revoke and turn it off is in Simon's private ops doc.

## D10 — 2026-10-23 — Send caps, JID normalisation, v0.6.0-pro (Simon: "lets do that")
Shipped from the review's "Then" list, built by two parallel agents, then a
fresh-context review (no blockers) whose should-fixes were applied before deploy:
- **Send caps on by default**, separate from pacing: 15/chat/min, 60/chat/h,
  120/h, 10 new contacts per 24h; looser budget for reactions/edits/deletes.
  Capped sends are refused before any presence/upload/typing. The per-chat
  default went 8 → 15/min so "send these 12 photos" works. Friday's bridge got
  30/min, 300/h in its own `.env`: it may only message Simon, so the per-chat
  cap there only guards against drowning one chat.
- **Senders stored as full JIDs**; migration 008 runs on every start (23,113 rows
  on main, 22 on Friday; 26 unclassifiable rows stay bare). LIDs stay `@lid`
  rather than being converted, so no information is lost.
- **Webhook `chat_jid` = the store's JID**, plus `chat_jid_alt`. The only live
  `chat_jid` triggers (FIDIT → n8n) are groups, which have no LID form.
- Also: corrupt state fails safe; clock steps don't; group cache never holds its
  lock across a fetch (it used to stall whatsmeow's event loop); panicking
  handlers are recovered; ORDER BY instant (DST night 25 Oct); deploy keeps a
  binary + every bridge's store per backup.
- **Not done, on purpose:** media retry stays enabled. The docs refresh found
  WhatsApp broke re-uploaded media keys for linked devices around 2026-09-29, so
  retries likely can't succeed; the queue has never run here (table empty), so
  leaving it costs nothing until it does.

Ops (outside this repo): ig-saves config files made 600; `~/Code/personal/infra`
pushed to a private GitHub repo.

## D11 — 2026-10-23 — Edits, calls, chat import, recall matrix; v0.7.0/v0.7.1-pro (Simon: "continue on the nect batch pls")
Three parallel agents, merged with the upstream media fix (#99), then a
fresh-context review whose findings were fixed before deploy:
- **Edit history, revokes, call log** (migration 009; 170 old call messages
  became `calls` rows). The store treats an edit or revoke as a claim, not a
  fact: edits only from the stored author, revokes from the author or a group
  admin, call-log messages only from own devices, LID↔phone only through
  `identities`/the LID map, never equal digits. Our own `/api/edit` and
  `/api/delete` refuse other people's messages up front (WhatsApp relays such
  an edit and recipients silently ignore it, so it was only rewriting our store).
- **Chat-export import** (`lib/chat_import.py`), dry run by default, no schema
  change. Imported rows carry `import-<hash>` IDs that never go to WhatsApp, and
  incoming messages with that prefix are dropped (IDs are sender-chosen).
- **recall** keeps vectors in one in-memory matrix: identical top-10 on 20 live
  queries, ~5x faster, ~50 MB resident at 32k messages.
- **Call stanzas:** only `From` is the server's; creator, alt JID and group are
  believed only when From backs them, otherwise an event may only update a call
  already recorded. Cost: an outgoing call placed on the phone that this device
  only sees through the callee's events is not logged live (the phone's
  call-log message still records it).
- **DST:** item 5 of the 2026-10-21 review was already live in v0.6.0-pro; the
  webhook's previous-message lookup was the last text compare and is fixed.
  Storing UTC stays a later migration.

## D12 — 2026-10-23 — History rewritten to finish the D7 scrub (Simon: "rewrite the history")
`git filter-repo` replaced the 8 real numbers and group IDs, a tailnet IP, home
paths and the host name with fakes across our commits and messages, and dropped
files that were only ever ours: the handoff note, two committed macOS bridge
binaries and a `.bak` file. Only commits not on upstream were rewritten
(`--refs ... ^upstream/main`): filter-repo drops commit signatures, so touching
upstream's signed commits would have changed their IDs and cut the fork off
from upstream (a first attempt did exactly that). The final tree is unchanged.
`main` and the six `-pro` tags were force-pushed (annotated tags re-signed);
releases follow their tags. Old commits can still be fetched by SHA from GitHub
(merged PR #4's ref and the fork network keep them) until GitHub Support purges
them. Any other clone of this repo must be re-cloned, not pulled.

## D13 — 2026-11-02 — /api/download obeys the media retry budget (Claude)
D3 assumed nothing retries in bulk. A nightly archive job did: 345 receipts in 15 minutes
through the synchronous path, which skipped the hourly cap, with 344 ending in "the phone
doesn't have it", and it would have re-asked weekly. Now that path shares the budget, sends
nothing for media the phone already said is gone (`NOT_FOUND`, "no longer available") and
nothing while a receipt is unanswered. A request that failed only by running out of
attempts may still be asked again by an explicit download, under budget, because the phone
may just have been offline. Trade-off: a download can fail immediately when an interactive
caller used to wait. Refusals are 502, not 429, so callers don't sleep on them.
This also corrects D10's note that retries likely can't succeed: 46 of 402 did.

## D14 — 2026-11-02 — Media retry is for people, not jobs (Simon: "yes pls")
Only 46 of 402 asks ever brought a file back, and every ask notifies the phone. So the
default cap drops from 20 to 5 an hour, the queue keeps quiet from 23:00 to 08:00 and waits
6 hours between asks about a file, and bulk callers opt out with `no_phone_retry`. The cap
now counts receipts, not files: 12 files once sent 24 receipts in 8 minutes unnoticed.
Quiet hours cover the background queue only, because a download someone is waiting for
should still work at night. Cost: a bulk job no longer recovers purged media by itself; a
person can still ask for a single file.

## D15 — 2026-11-02 — Save media while it exists (Simon: "yes")
About 3,900 of 5,800 media messages had no copy on the live box. Auto-download only ever
covered live incoming messages, so everything the bridge sent or got through history sync
was a row with keys, and it became unrecoverable when the servers purged it. A daily sweep
now fetches recent media with no copy, without asking the phone. Files older than the
window are not chased, because almost none of them were still on the servers.

## D16 — 2026-11-22 — wa-app design directions, round 1 (waiting on Simon's pick)
Three directions on fictional sample data, published at
https://wa-app-directions.vercel.app (throwaway Vercel project; its GitHub link,
added by the Vercel CLI, was disconnected straight away). A: familiar, quiet and
native (amber). B: dark statement, one loud accent (lime only on drafts and
unread, Geist Mono labels). C: reading, not chatting (serif transcript, ink red).
Recommended B: it is Simon's recorded taste and its accent makes a draft
impossible to mistake for a sent message (RESEARCH.md F2, F5). No pick yet.

## D17 — 2026-11-22 — Direction A picked (Simon: "A pls")
Simon picked A, "Familiar, quiet and native", against my recommendation of B.
Its contract is `wa-app/DESIGN.md` (linted, 0 errors, every text pair passes AA in
both themes). B and C are in its "Already tried" list. The two-strangers "does it
look AI-made" check from the skill was not done: there was nobody to ask in-session.
Key components (list rows, chat bottom with draft card and composer, tab bar) and
three symbol-mark options are at https://wa-app-directions.vercel.app/key-components/
and /marks/sheet.html, waiting on Simon's ok and mark pick before the design-lock tag.

## D18 — 2026-11-22 — App icon: option 2 (Simon: "icon 2")
The amber bubble with a solid dot inside (wa-app/docs/brand/src/options/option-2.svg),
against my recommendation of option 1. Masters and the icon set are made from it at
the lock. Key components not yet okayed in Simon's words.

## D19 — 2026-11-22 — Key components okayed, design locked (Simon: "pieces are ok")
Simon okayed the key components page (chat rows, chat bottom with the draft card and
composer, tab bar) as shown. `wa-app/DESIGN.md` is the locked contract (tag
`design-lock`), with the brand masters and icon set in `wa-app/docs/brand/`. The
favicon uses the dark tile, because the bare amber mark is too faint on light tabs.
The public name is still open: Simon turned down Understudy; I advised against
WhatsUp (Meta enforces the "Whats-" prefix, and the PRD rules out a WhatsApp-like
name) and ClaudeChat (the app also runs on local models and other APIs, and
Anthropic's brand rules don't allow "Claude" in third-party product names).

## D20 — 2026-11-22 — wa-app chats list: how the store reader decides things
Task 2 of the M1 plan. Choices the plan left open, all in `wa-app/server/wa_app/store.py`:
- Favourites are the pinned chats (no favourites store exists yet).
- `whatsapp-mcp-server` is flat modules, not a package, so `wa_app/mcp_lib.py` puts its
  directory on `sys.path` (repo sibling, or `WA_MCP_SERVER_DIR`) and re-exports
  `chat_forms`, `resolve_display_names`, `canonical_chat_jid`. The helpers used so far
  (`lib.naming`, `lib.changes`, `lib.utils`) need only the standard library, so no new
  dependencies yet. `lib.utils` insists on a messages.db at import time; `WA_SKIP_DB_CHECK`
  is set so nothing resolves to a store, and every call passes the app's own connection.
- A person filed under both `@lid` and `@s.whatsapp.net` is one conversation (JID forms
  joined through `chat_forms`, the phone form is the chat's `jid`); states merge (pinned,
  archived, marked-unread, muted: any; last read: newest; history count: sum).
- Kinds: `@newsletter` is `newsletter`; a group that is announce-only or a community's
  default subgroup is `announcement`; any other community member is `community_group`.
- Voice-note length is not stored. It is read from the downloaded Ogg/Opus file when
  there is one, else the preview is "Voice message" with no length. Receipts are not
  stored either, so `ticks` is always null for now.

## D21 — 2026-11-22 — M1 build rulings
Choices made while building wa-app M1 (the number D20 above covers only the chats list):
- Favourites are the pinned chats in M1.
- Announcement kind precedence: announce-only or a community's default subgroup counts as
  announcement before the community-member rule applies.
- No TanStack Query. The front end uses plain fetch hooks.
- `contact_cards` is a list, since one message can carry several cards.
- Polls are read from the store, and poll-vote rows are hidden from the chat.
- The members line is built from who has written in the chat, because no participant list is stored.
- Ticks are not shown, because there is no receipt data.
- The chat pane does not follow the finger yet (the DESIGN.md Motion row). Deferred.
- Receipts are capped at the newest 200 per read.

## D22 — 2026-11-23 — M2 build rulings
Choices made while building wa-app M2 (AI drafts), where the plan was silent or unclear:
- R1: Use the installed `httpx2` package (added to runtime dependencies next to
  `cryptography`) for async HTTP and `MockTransport`, because the plan named httpx but only
  `httpx2` was installed. Cost if wrong: a dependency swap.
- R2: A "local" backend is an `openai` kind whose base URL host passes the same loopback or
  private check as the local-only gate; everything else is cloud. This keeps one definition
  of local. Cost if wrong: the wrong worker semaphore (1 for local, 3 for cloud) is chosen.
- R3: The send route and `sending.send_text` accept an optional `draft_id`, kept as a hint
  for outcome matching. `ai.drafts` and `ai.learning` never import `sending`, so the
  "no send path" test stays valid. Cost if wrong: outcomes link by time window only.
- R4: Task 7 also adds a server test route for a backend (`POST /api/ai/backends/{id}/test`)
  with a test, because the Settings screen needs it. Cost if wrong: an extra route to remove.
- R5: The `claude` CLI runs from `<WA_APP_DATA>/claude-cwd`, an empty directory (mode 700),
  so it picks up no project files or settings. Cost if wrong: a different working
  directory, and the CLI might load project context.
- R6: Only eligible triggers restart the debounce. Ineligible incoming messages (stickers,
  polls, contact cards, media without a caption, system rows) do not. Text, voice notes and
  media with a caption are eligible; the manual Draft button accepts any renderable
  incoming message. The spec's "a new message" is read as "a new triggering message".
  Cost if wrong: a reply is drafted for an earlier message while a sticker followed.
- R7: Any own message (text or media, from any device) supersedes the chat's live draft
  (pending, ready or failed). An outcome is recorded only when the message can be linked to
  a draft. This follows "phone replies first removes the card". Cost if wrong: a draft
  disappears that the user still wanted; they can tap Draft again.
- R8: "Last distilled time" is dropped from the chat AI sheet. The API has no distill
  timestamp, and adding one needs an `app.db` column migration. Cost if wrong: the user
  cannot see when the rules were last updated automatically.
- Changing a backend's kind or base URL without sending a new API key drops the saved key,
  so a saved key can never be redirected to a different server. The form says so first.
  Cost if wrong: the user retypes the key after moving a server.
- Prompt hardening stopped after the fourth automated security finding (homoglyph brackets
  in message bodies). Rounds one to three closed the real holes: fenced examples, the
  trigger not repeated outside the fence, no marker or label spoofing, bracket look-alikes
  and invisible characters stripped. The rest is bounded: body text always follows the real
  numbered sender label on its own line, there is no path from a draft to a send, and a
  draft cannot reach another chat. The worst case is a degraded draft the user can ignore.
  Cost if wrong: a crafted message steers one draft, never a send.
- Deferred minors worth knowing:
  - The 03:30 distillation uses naive local time, so it fires an hour off once at a DST change.
  - The send hint can be taken by an unrelated phone message within 120 s, and then falls
    back to the latest ready draft.
  - The chat name sits outside the prompt fence (flattened, 80 characters). The note
    and rules are now fenced as data like the examples.
  - The worker reads whether the app is open once at job start, and the inbox is unbounded
    with one read-only open per candidate under backfill bursts.
  - Save is disabled without a message when a chat is local only, the default is a cloud
    backend and a local one exists.
  - Missing tests: a trigger that renders to nothing, a deleted trigger, and sheet focus
    restore with Escape.
  - AI routes now take `db_lock` and `ai.key` is created with `O_EXCL`; what remains is
    that sqlite errors in AI routes map to generic 503/500.
  - The browser check of the settings screen is left for deploy.
  - The optional thumbs UI is deferred: the feedback route exists (server only) and the
    card has no thumb buttons.
- D22: Local-only calls connect to the address that was checked. For an `http://` base URL with
  a hostname, the name is resolved once, every address must be loopback or private, and the
  request goes to one of those addresses with the original Host header, so a name that changes
  its answer cannot send chat content to a public server. An `https://` hostname is refused on a
  local-only chat, because connecting to a bare address breaks the certificate name check; a
  private IP address over `https://` stays allowed. The server tells the web app which backends
  are local (a `local` flag on each backend), so the sheet and the server always agree. Cost if
  wrong: a local server behind an https name has to be reached by its IP address or over http.

## D23 — 2026-11-23 — Agent drafts: Claude pushes a draft into wa-app
- A bearer-token API for non-browser callers: `POST /api/agent/drafts` and `GET`/`PUT /api/agent/chats/{jid}/ai`.
  The token is `WA_APP_AGENT_TOKEN`, compared with `hmac.compare_digest`; those routes skip the session cookie and the
  Origin check (browsers never attach a bearer token themselves, so CSRF does not apply). Everything else under
  `/api/*` is unchanged.
- Off without a token: the routes answer 404, as if they did not exist.
- No send path. The agent module never imports `sending` (a test pins it), a pushed draft is a `ready` row with
  `source='agent'` and makes no AI call, and the user's tap and Send stay the only way out. The MCP tools live in
  the opt-in `drafts` toolset and write only to wa-app.
- The draft worker stays the only writer of `drafts` (`DraftWorker.push`). A push replaces the chat's live draft and
  cancels a running automatic job. Learning needed no change: it links outcomes to any ready draft.
- Cost if wrong: a leaked token lets its holder put text on draft cards and flip the AI switch, never send.

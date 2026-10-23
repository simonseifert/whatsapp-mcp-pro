# Changelog

All notable changes to this fork are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions carry a
`-pro` suffix and name the upstream release they are based on.

## [Unreleased]

### Added

- **Chat export import** (`uv run python -m lib.chat_import EXPORT.zip --chat JID`):
  brings WhatsApp's own "Export chat" files (iOS and Android, any date order,
  12h/24h, English/German/Croatian placeholders) into `messages.db`, so history
  older than linked-device sync reaches `list_messages`, `recall`,
  `download_media` and transcription. Dry run by default; `--apply` backs up
  the store and writes in one short transaction, safe while the bridge runs.
  Rows already stored are matched by chat, sender, time window and text and
  never duplicated; imported rows get stable `import-<hash>` IDs, so re-runs
  are idempotent. Export names map to JIDs only on evidence; the rest are
  stored as `import:<name>`. Attachments are copied into `store/media/`. See
  `docs/IMPORT.md`. No schema change.
- **Edit history.** Edits (live, history sync and our own `/api/edit`) replace
  `messages.content` and are kept, with the text they replaced, in a new
  `message_edits` table. Replays are safe: edits apply only in edited-time
  order, and a late copy of the original never overwrites an edit. The recall
  index re-embeds an edited message.
- **Deleted for everyone is recorded, not erased.** A revoke sets
  `messages.deleted_at` / `deleted_by` and keeps the text.
- **Call log.** A new `calls` table records every call the bridge sees
  (direction, video, group, outcome, duration when known), filled from live
  call events, the phone's call-log messages and history "missed call" stubs,
  and seeded from the existing `call-…` pseudo-messages. The bridge never
  answers or rejects a call.
- MCP tools `get_message_edits` and `list_calls` (both read-only; 39 tools,
  21 read-only). Message output marks changed messages "(edited)" and
  "(deleted)".
- Webhook `event` trigger: opt in to `message_edited`, `message_deleted` and
  `call` events (see `docs/WEBHOOK.md`). `all` does not receive them.
- Migration `009_edits_calls.sql` (idempotent; the bridge also applies it on
  start).

### Changed

- **`recall` keeps its embeddings in memory instead of reading them all per
  call.** Every call used to SELECT every embedding blob that passed the
  filters and stack them, ~130 MB transient and linear in history. The vectors
  now live in one float32 matrix per model, loaded on the first call and
  extended with the rows the backfill adds; SQL still decides which messages
  match, so filters, merged LID chats, sender forms and edits behave exactly as
  before, and results are identical. At 32k messages an unfiltered search went
  from ~280 ms to ~60 ms with ~1 MB transient instead of ~66 MB; at a synthetic
  160k, ~1 s to ~210 ms and ~360 MB to ~25 MB. The cost is resident memory:
  1.5 KB per embedded message (~50 MB at 32k), held for the life of the
  process. Searches with a date or `is_from_me` filter are bound by the SQL
  filter and gain little. A covering index (`idx_msgemb_model_key`) is created
  on `message_embeddings` the first time recall runs. `scripts/bench_recall.py`
  reproduces the numbers against a copy of a store.
- SETUP.md: `MALLOC_ARENA_MAX=2` in the example MCP unit, and what the shared
  server's memory consists of.
- Bridge: `/api/download` serves an imported row's media from its saved,
  hash-checked copy (such rows have no media key) and never goes to the network
  for it. Reactions, quotes, edits and deletes targeting an `import-` ID are
  refused with 422, and on-demand history requests anchor on the oldest real
  message, not an imported one. `/api/read` drops `import-` IDs from the batch
  (reported as `skipped`), an explicit `import-` history anchor is refused, and
  the reconnect backfill no longer anchors on an imported row.
- Bridge: a newsletter post that arrives only as its edited version is stored
  as a post again instead of waiting as a pending edit.
- Bridge: merging a LID chat into its phone-JID twin also moves its edit
  history and calls, and keeps a revoke or edit that only the dropped
  duplicate had seen. An edit waiting under one chat form is applied when the
  original arrives under the other.

### Fixed

- Rows standing in for a revoked or edited message the store never saw, and
  imported text rows, no longer hold NULL sender/content/media columns, which
  made `/api/messages` fail for the whole chat (and the reconnect backfill skip
  it) and crashed `list_messages`. Readers on both sides now tolerate NULLs
  left by older versions.

### Security

- An edit is applied only when it comes from the message's author (phone JID
  and LID compared through `identities`; own messages only from own devices;
  newsletter posts only from the newsletter). Anything else is recorded as
  `rejected` and changes nothing; an edit for a message not stored yet waits as
  `pending` until the original arrives. Sender-claimed edit times are clamped
  to the server's receive time.
- A revoke is accepted only from the author or, in a group, an admin.
- Call-log messages are accepted only from the user's own devices, and a call
  event cannot attach to a call in another chat.
- `/api/edit` (`edit_message`) refuses a message not sent from this account
  (403 `not_own_message`), and `/api/delete` (`delete_message`) one that is
  neither ours nor in a group we administer (403 `not_group_admin`). Both
  refuse a message that is not stored (422 `message_not_stored`). WhatsApp
  relays such changes and recipients ignore them, so they used to report
  success while rewriting only the local store. Our own changes are now
  recorded through the same author check as everyone else's.

## [v0.6.0-pro] - 2026-10-23

Still based on upstream v0.4.0. Send caps that outlast warm-up, one JID form
per message and webhook, and a bridge that survives a panicking handler.

### Added

- **Send caps, on by default** (`ANTIBAN_CAPS_ENABLED`, independent of
  `ANTIBAN_ENABLED`): per chat 15/min and 60/h, 120/h overall, and 10 new
  contacts per rolling 24h for text, media and polls; reactions, edits and
  deletes get a looser separate budget (20/min per chat, 300/h). A cap hit is
  HTTP 429 `send_cap` with `Retry-After`, on every message-creating endpoint;
  the MCP marks it retryable with a "wait, don't loop" hint. A capped send is
  refused before any presence, upload or typing reaches WhatsApp, and leaves
  its idempotency key retryable. The phone JID and the LID of one person share
  a budget. The new-contact window persists in `store/antiban_caps.json`.
- Group metadata cache (`GROUP_METADATA_CACHE_TTL`, default 5m) in front of
  `GetJoinedGroups` / `GetGroupInfo`, invalidated by WhatsApp's group events.

### Changed

- **Webhook `chat_jid` is now the JID the store uses.** `message_received`
  payloads used to carry the raw chat JID off the wire, which for many 1:1
  chats is `<id>@lid`, while the store, the MCP tools and `list_chats` file the
  same chat under the phone JID. `chat_jid` is now that normalised JID, and a
  new `chat_jid_alt` carries the other identity of a 1:1 chat when the bridge
  knows it (omitted for groups and unmapped chats). **Compatibility:** a
  consumer that matched on the old `@lid` value of `chat_jid` now sees the
  phone JID there; match on `chat_jid` or `chat_jid_alt`, or switch to the
  phone JID. `chat_jid` webhook triggers match either identity, so existing
  trigger rules written against the LID keep firing.
- **`messages.sender` stores full JIDs.** Incoming messages used to store only
  the user part (`4915112345678`, or a LID's digits), which could not be told
  apart later. New rows hold `4915112345678@s.whatsapp.net` or
  `123456789012345@lid` (device suffix stripped). Migration
  `008_normalise_senders.sql` rewrites old bare rows the identity directory can
  classify; the bridge applies it on every start, so rows whose mapping arrives
  later are picked up too. Rows it cannot classify stay bare. Readers comparing
  `sender` to a bare number should match both forms; the MCP tools already do,
  and `recall(sender=...)` now matches every form of the person.
  `sender_name` still falls back to the bare number.
- `/api/send` refuses (400) a bare-number recipient that is a known LID's
  digits instead of sending it to `<digits>@s.whatsapp.net`, a stranger's
  phone number. Pass full JIDs; the MCP send tools now say so.

### Fixed

- Reactions and quoted replies to group messages whose stored sender was a
  bare number went out with a participant JID that had no server, reported
  success, and attached to nothing. The participant is now resolved (bare
  digits are a phone number unless they match a known LID), and a sender that
  cannot be resolved fails the request instead of sending a broken one.
- A corrupt warm-up or caps state file disabled protection. It is now moved
  aside as `.corrupt-<time>` and the strictest limit applies; an antiban that
  fails to start stops the bridge instead of running unprotected.
- Messages are ordered by instant (`datetime(timestamp)`), not timestamp text,
  which misordered the repeated hour on DST-change night.
- A panic in any whatsmeow event handler is logged with a stack instead of
  killing the bridge (whatsmeow runs handlers without a recover).
- `deploy-live.sh` keeps the running binary and every bridge's store in each
  of the last five backups (mode 700), and rolls back when a restart fails.

## [v0.5.0-pro] - 2026-10-22

Still based on upstream v0.4.0. This release is about not losing the account
or a message: the bridge now notices an offline phone before WhatsApp unlinks
it, sends are safe to retry, and first-contact restrictions are respected
instead of retried into a ban.

### Added

- Phone-offline detection. The bridge records when it last saw the primary
  phone (receipts and messages from device 0, media retry answers, pairing) in
  a new `phone_status` table. `GET /api/connection` gains `phone_last_seen` and
  `phone_offline_days`. The log gets a WARN once a day past 7 days and an ERROR
  past 12; WhatsApp unlinks companions after about 14 days without the phone,
  and a restart does not help. Alert on `phone_offline_days` in your health
  check (example in [SETUP.md](SETUP.md#linux-systemd)).
- Phone ping: once the phone has been silent for `PHONE_OFFLINE_PING_AFTER`
  (default `7d`), the bridge sends one app-state key request to your own phone
  at most once a day. Nothing goes to contacts. An idle phone answers and the
  clock resets. `PHONE_PING_ENABLED=false` turns it off; tracking and the API
  fields stay on.
- Persistent media retry queue (`media_requests` table). With
  `MEDIA_RETRY_ENABLED=true`, expired media is queued and a background loop
  asks the phone again later, so a restart or an offline phone no longer loses
  the request. Paced by `MEDIA_RETRY_MAX_PER_HOUR` (default 20, counting the
  receipts `/api/download` sends itself) and `MEDIA_RETRY_MAX_ATTEMPTS`
  (default 3); a NOT_FOUND answer from the phone fails the request at once.
  Recovered files are saved to `store/media/<jid>/<msgid><ext>`, and
  `/api/download` serves that copy (`via: "saved"`). `GET /api/media-requests`
  (optional `state`: pending, sent, succeeded, failed) shows the queue.
- Idempotent sends. `/api/send`, `/api/reaction` and `/api/poll/create` accept
  `idempotency_key` in the body or an `Idempotency-Key` header. A repeat after
  success returns the stored result with `Idempotent-Replay: true` and sends
  nothing; a repeat while the first is still running gets 409; the same key
  with a different payload gets 422; a repeat after a failure tries again with
  the same WhatsApp message ID, which recipients' devices de-duplicate if the
  first attempt did get through. Keys are scoped to endpoint and recipient and
  kept for 24 hours (`idempotency_keys` table).
- MCP send tools (`send_message`, `send_file`, `send_audio_message`,
  `create_poll`) use a fresh random key per call, retry a timed-out or dropped
  request once with that key, and return `idempotency_key` and `deduplicated`.
  Pass the key back to retry a whole call without risking a double send.
  `send_reaction` takes an optional key.
- Reachout timelock handling. WhatsApp answers error 463 when an account is
  temporarily barred from starting new chats. The bridge records the lock end
  (from WhatsApp's `NotifyAccountReachoutTimelock` notification, or
  `REACHOUT_TIMELOCK_DEFAULT`, default 24h), persists it, and refuses sends to
  contacts with no stored history until then, with 429 and `Retry-After`.
  Replies in existing chats and group messages still go out.
  `GET /api/connection` gains `reachout_timelock_until`.
- `list_unnamed_contacts` and `set_contact_names`: an agent-driven loop for
  naming people known only by number (37 tools now, 19 read-only). Names are
  stored in `contact_nicknames` (the `manage_nickname` store). Directory reads,
  `list_chats` and `list_messages` (new `sender_name` field) overlay live
  nicknames, so a name shows before the bridge's next directory rebuild. Names
  carry provenance (`contact_nicknames.source`, added by the MCP):
  `set_contact_names` requires `confirmed_by_user`; unconfirmed names are
  stored as `agent_inferred`, shown as "(unverified)", and can't replace a user
  nickname or an address-book name. Directory records expose `name_source`,
  messages `sender_name_source`.
- Opt-in OAuth 2.1 for the shared HTTP MCP server (`MCP_OAUTH_ENABLED`, off by
  default), so claude.ai and ChatGPT connectors can reach it without a static
  header. PKCE, dynamic client registration, refresh-token rotation, a consent
  page with a passphrase and a read-only or full choice, and a CLI path
  (`python -m lib.oauth approve|list|revoke`). Needs a public HTTPS URL. See
  [SETUP.md](SETUP.md#remote-connector-via-oauth-optional).
- `SEND_TIMEOUT_SECONDS` (25) and `SEND_MEDIA_TIMEOUT_SECONDS` (150) bound a
  send, including upload and pacing, so the bridge answers 504 before the
  client's own timeout and records the outcome against the idempotency key.

### Changed

- Send endpoints return distinct status codes instead of a blanket 500, which
  callers read as "retry" and so delivered slow sends twice: 400 bad input,
  403 refused (allowlist, media path), 409 duplicate in flight, 422 key reused
  with a different payload, 429 rate limit / antiban pause / reachout
  timelock, 502 WhatsApp error, 503 not connected, 504 timeout. Edit and
  delete use the same mapping.
- Sends run on the request context, so a client that disconnects cancels the
  send.
- Media retries now go through the paced queue; `/api/download` still tries
  once inline.
- The media file layout moved to `internal/mediastore`, shared by
  `/api/download` and the retry queue.

### Fixed

- Contact naming's nickname lookup failed with "no such column: raw.jid" on SQLite builds that reject an outer column in a subquery's ORDER BY (e.g. Ubuntu 24.04's 3.45).
- The bridge's directory rebuild and webhook sender names ignore
  `agent_inferred` nicknames, so a stranger's self-claimed name never shows up
  as if you had set it.
- An agent write can no longer delete a user nickname stored under the other
  JID form (LID vs phone JID) of the same person.
- A 463 on a send addressed to a bare LID no longer arms the reachout timelock.
  WhatsApp also answers 463 when it can't route a LID that has no phone
  mapping yet, and treating that as a lock blocked every new contact for a day.

### Security

- `SEND_ALLOWED_JIDS` and `WHATSAPP_ALLOWLIST_JIDS` now gate reactions, polls,
  edits and deletes as well as sends. Before, a sender confined to one chat
  could still react, poll, edit or revoke in any other.
- OAuth hardening: redirect URIs must point at a host in
  `MCP_OAUTH_ALLOWED_REDIRECT_HOSTS` (exact match, checked at registration and
  every sign-in); wrong passphrases cost a growing per-address wait (per IPv4
  address or IPv6 /64) and three of them end the consent request; a global
  failure budget (`MCP_OAUTH_FAILURE_BUDGET` per `MCP_OAUTH_FAILURE_WINDOW`)
  stops guessing from many addresses; consent is bound to the browser that
  started the sign-in, so a forwarded consent link can't be approved;
  `X-Forwarded-For` is honoured only from `MCP_TRUSTED_PROXY`; clients and
  hashed tokens live in `oauth.db` with mode 600.

## [v0.4.0-pro] - 2026-10-22

Based on upstream [v0.4.0](https://github.com/FelixIsaac/whatsapp-mcp-extended/tree/v0.4.0)
(FelixIsaac/whatsapp-mcp-extended). Everything upstream ships is included; this
entry lists what the pro layer adds on top, and the fixes from the October 2026
review.

### Added

- `recall`: multilingual semantic search over the full history, vectors stored
  in the bridge's SQLite, kept current by a background indexer. Model-scoped,
  CPU by default.
- `transcribe_audio` with pluggable backends: whisper.cpp server
  (`WHISPER_SERVER_URL`), mlx-whisper, faster-whisper and Groq.
  `WHISPER_BACKEND=auto` tries them in that order. `AUTO_TRANSCRIBE_VOICE`
  transcribes incoming voice notes in the background into the store.
- `check_inbox`: "what arrived since I last asked", with a cursor that starts
  at now.
- Identity directory: an `identities` table joining phone JIDs and LIDs into one
  row per person, group, newsletter or broadcast list, exposed as
  `lookup_identity`, `search_directory` and the `whatsapp://directory` resource.
- LID chat merging (`MERGE_LID_CHATS`, on by default): chats split across a LID
  and a phone JID are folded into one; merged rows are kept and stamped with
  `chats.merged_into`.
- Shared streamable-HTTP MCP server (`serve_http.py`) with scoped bearer
  tokens: full, and read-only limited to tools annotated `readOnlyHint=true`.
- Media retry for expired media (`MEDIA_RETRY_ENABLED`, opt-in): asks the
  sender's phone to re-upload when the CDN copy is gone.
- Headless pairing: the current QR payload is written to `store/pair-qr.txt`
  while the bridge waits for a scan.
- Poll votes decrypted and stored, `get_poll_results` tool.
- `request_history` finds its anchor message automatically, and history can be
  backfilled in the background.
- `WEBHOOK_ALLOWED_ADDRS`: allow specific private webhook targets without
  turning off the SSRF check.
- `scripts/deploy-live.sh`: build to a side file, run Go and Python tests, back
  up the store online, swap the binary, restart, and roll back if any bridge on
  the binary fails to report `connected` within two minutes. Covers every bridge
  that runs the binary, not only the primary.
- Quote and forward context stored for every message.

### Changed

- MCP server ported to mcp 2.x (`MCPServer`).
- whatsmeow updated to 2026-10-17; bridge built with Go 1.26, toolchain 1.27.3.
- The bridge exits with code 3 on "Stream replaced", so a supervisor can stop
  restarting into a session another process has taken
  (`RestartPreventExitStatus=3`).
- Media retry is opt-in, since every retry shows "Finished syncing" on the
  user's phone.
- Authenticated loopback callers are exempt from the bridge's rate limit.
- CI builds golangci-lint v2 with the toolchain from `go.mod`; the errcheck and
  govet findings it surfaced are fixed.
- Stale macOS binaries and `.bak` files are no longer tracked.

### Fixed

- Message writes are upserts that keep existing transcripts and refreshed media
  fields when WhatsApp re-delivers a message.
- `recall` date filters compare UTC instants, and the embedding model loads from
  the local cache instead of contacting the hub at startup.
- Message timestamp filters in the MCP server compare instants, not text.
- Forced reconnects go through `ResetConnection()`, so auto-reconnect and the
  watchdog stay armed afterwards.
- The first connect after boot retries with backoff and hands a failure to the
  watchdog, instead of leaving a live process that ingests nothing when DNS was
  not ready yet.
- Two media files saved in the same second no longer serve the wrong message:
  saved files are checked against their hash, and a colliding name gets the
  message ID added.
- Auto-download paths stay inside the chat's directory and never overwrite the
  alternate file.
- Every MCP entry point that takes a chat JID follows `chats.merged_into`, so a
  merged LID still finds its messages.
- `check_inbox` cursor writes are atomic and locked, so concurrent calls can't
  lose or corrupt the cursor.
- `deploy-live.sh` tests the bridge before replacing the live binary.

### Security

- Media guard in both the bridge and the MCP server: local paths for
  `send_file` and `send_audio_message` must be in `/tmp` or `MEDIA_ALLOWED_DIRS`,
  and even there dotfiles, dot-directories, `.env`, `.pem`, `.key`, SSH keys,
  credential files, any SQLite database and its WAL/SHM/journal sidecars, and
  files at the top level of a `store/` directory are refused. Symlinks are
  resolved before checking; `store/` segments match case-insensitively.
- `send_audio_message` validates its source file before converting it.
- Two exact-match send allowlists (`SEND_ALLOWED_JIDS`,
  `WHATSAPP_ALLOWLIST_JIDS`); no substring or prefix matching.

[Unreleased]: https://github.com/simonseifert/whatsapp-mcp-pro/compare/v0.5.0-pro...main
[v0.5.0-pro]: https://github.com/simonseifert/whatsapp-mcp-pro/compare/v0.4.0-pro...v0.5.0-pro
[v0.4.0-pro]: https://github.com/simonseifert/whatsapp-mcp-pro/compare/v0.4.0...v0.4.0-pro

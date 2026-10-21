# Roadmap: the ultimate WhatsApp MCP

Goal: the most capable, safest, self-hosted WhatsApp layer for AI agents.
Grounded in a July 2026 survey of the ecosystem (lharries, FelixIsaac upstream,
GOWA, WAHA, wweb-mcp, Baileys forks, Matrix bridges): our unique ground is
**semantic recall + local transcription + the zero-slot web client**; the gaps
worth closing are listed below in value order.

## Shipped (July 2026)

- [x] Rebase pro layer onto live upstream; CI green (tests fixed upstream-side too)
- [x] Shared streamable-HTTP server for N concurrent Claude sessions
- [x] Scoped bearer tokens (full / read-only by tool annotation)
- [x] wa-client: chat web UI riding the bridge session (zero device slots) — **retired 2026-10-21** (DECISIONS D2): it ignores `WA_STORE_PATH`, had no login, and showed stale data for weeks unnoticed
- [x] Real-time push: bridge webhook → SSE → browser
- [x] Scheduled sends (queue + delivery loop + composer UI)
- [x] Send JID allowlist in the bridge (upstream issue #47)
- [x] Automated SQLite backups (online .backup, gzip, retention)
- [x] Anti-ban pacing available (delays + typing sim, warm-up neutralized for aged accounts) — **opt-in**, `ANTIBAN_ENABLED=true`; it is off by default

## Next up (October 2026)

- [x] **Phone-offline ping** (v0.5.0-pro) — `phone_last_seen` / `phone_offline_days` in `/api/connection`, WARN past 7 days, ERROR past 12, one silent app-state key request a day to the user's own phone after `PHONE_OFFLINE_PING_AFTER`
- [x] **Send idempotency keys** (v0.5.0-pro) — `idempotency_key` / `Idempotency-Key` on send, reaction and poll; replays return the stored result; MCP send tools use a fresh key per call and retry once on timeout
- [x] **OAuth 2.1 for the HTTP MCP** (v0.5.0-pro, opt-in `MCP_OAUTH_ENABLED`) — so claude.ai and ChatGPT connectors can reach the shared server without a static bearer header
- [x] **Agent contact-naming loop** (v0.5.0-pro) — `list_unnamed_contacts` + `set_contact_names`; unconfirmed names are `agent_inferred`, shown "(unverified)" and ignored by the bridge's directory and webhooks
- [x] **Persistent media-retry queue** (v0.5.0-pro) — `media_requests` table, paced loop (`MEDIA_RETRY_MAX_PER_HOUR`), recovered files saved under `store/media/`, `GET /api/media-requests`
- [x] **463 reachout-timelock handling** (v0.5.0-pro) — lock recorded and persisted, first-contact sends refused with 429 + `Retry-After`, `reachout_timelock_until` in `/api/connection`
- [ ] **WhatsApp chat-export import** — load a phone's "Export chat" archive to fill history the linked device never received
- [ ] **Edit history + call log** — keep prior versions of edited messages; expose calls as queryable records
- [ ] **Prometheus `/metrics`** — messages/hr, webhook deliveries, send latency, recall index lag (see Observability below)

## Earlier "next" list (high value, low risk)

- [x] **Media-retry path for expired media** (shipped, opt-in) — `internal/whatsapp/download.go` does `SendMediaRetryReceipt` → `events.MediaRetry` → `DecryptMediaRetryNotification` and refreshes `direct_path`. Opt-in via `MEDIA_RETRY_ENABLED=true`, because each retry shows "Finished syncing" on the phone. Enabled on the maintainer's install since 2026-10-21 (DECISIONS D3); it only recovers media the phone still has. The persistent retry queue shipped in v0.5.0-pro.
- [~] **LID/PN JID normalization** — chat merge done (`MERGE_LID_CHATS`, default on; every MCP entry point follows `chats.merged_into` since 2026-10-22); `sender` column still open. Chat side: `NormalizeChatJID` + `MergeLIDChats` leave 0 split `@lid` chats (checked 2026-10-21). Still open: (1) the `sender` column mixes bare user parts (live path, `handlers.go`), full `@lid` and full `@s.whatsapp.net` JIDs (history path), and bare LID numbers can't be told from phone numbers; store `Sender.ToNonAD()`, prefer a phone `SenderAlt`, backfill via `identities.lid`. (2) webhooks carry the raw `msg.Info.Chat`, not the normalized `chat_jid` the row is stored under, which is why wa-assistant watches both. (3) history sync doesn't call `NormalizeChatJID` on `conversation.ID`.
- [x] **Presence policy decision** (2026-07-19; superseded 2026-08-04; current default `WA_PRESENCE_MODE=human` + `PRESENCE_PING_ENABLED=false`) — upstream's presence manager now replaces our static gate: offline by default, online only transiently around real outgoing sends, `PresenceModeAlwaysOnline` to opt back in. `PRESENCE_PING_ENABLED` still governs the periodic ping. Original decision was **off by default**. Both the on-connect `SetPresence("available")` and the periodic ping are now gated behind `PRESENCE_PING_ENABLED` (default `false`), so the paired phone keeps its push notifications and the bridge stops painting an always-online fingerprint. Opt back in with `PRESENCE_PING_ENABLED=true` if you want to appear online to contacts. Future option (not done): mirror real activity mautrix-style.
- [x] **Reply/quote in send_message** (upstream #57, merged in 2026-08-04) — `send_message` now takes `quoted_message_id` and `mentioned_jids`. Remaining: wa-client swipe-to-reply UI.
- [ ] ~~**Reactions in wa-client**~~ — dropped with wa-client's retirement; the bridge endpoint and `send_reaction` tool exist
- [x] **Voice-note auto-transcription pipeline** (shipped 2026-07-06) — background loop transcribes incoming voice notes into `content`; recall indexes them; backend-pluggable (mlx / faster-whisper / Groq)
- [ ] **Scheduled sends as MCP tools** (`schedule_message`, `list_scheduled`, `cancel_scheduled`) so agents can schedule, not just the web UI
- [ ] **Upstream PRs**: tests fix (done in-fork), JID allowlist, scheduled sends — keep the fork mergeable and the upstream relationship warm
- [~] **Releases + tags** — CHANGELOG.md has v0.4.0-pro and v0.5.0-pro; the tags and GitHub releases are not cut yet

## Mid-term

- [x] **go-security green** (shipped 2026-07-06) — CI pinned an exact patch (1.25.10) that missed a stdlib security fix; floated to `1.25` so setup-go always grabs the latest patch. Same bug exists upstream (FelixIsaac) — worth a PR.
- [ ] **Observability** (upstream #10/#11): Prometheus `/metrics` (messages/hr, webhook deliveries, send latency, recall index lag) + structured JSON logs
- [~] **MCP resources for live state** — `whatsapp://status`, `whatsapp://sync-status` and `whatsapp://directory` exist; recall index status is still a tool (`recall_index_status`)
- [ ] ~~**wa-client PWA**~~ — dropped with wa-client's retirement
- [ ] **Per-agent token registry** — N named tokens with per-toolset scopes (WAHA 2026.4-style), not just full/readonly
- [x] **Contact enrichment** — shipped as the identity directory: the `identities` table joins phone contacts and LID mappings, exposed via `lookup_identity`, `search_directory` and `whatsapp://directory`

## Long-term / exploratory

- [ ] **Multi-account** — GOWA-style multiple device sessions behind one server (needs per-session stores + routing; big lift). GOWA's concrete shape (verified July 2026): one process, N sessions via a `/devices` CRUD resource, per-request scoping through an `X-Device-Id` header/query param, WS scoping via `?device_id=`, per-device webhook override, and a separate `DB_KEYS_URI` so session keys don't live in the same DB as messages (a re-link doesn't force a full data wipe). GOWA's README claims MCP and multi-device REST can't run together (a "limitation from whatsmeow") — unverified against whatsmeow's actual API, worth testing against our own bridge before assuming it applies to us too.
- [ ] **Digest engine** — daily/weekly per-chat summaries via recall + an LLM, delivered as a WhatsApp note-to-self or ntfy push
- [ ] **Call events** (whatsmeow exposes them; no MCP surfaces them today) — missed-call triggers for agents
- [x] **Agent inbox pattern** (shipped as `wa-dispatch/`, plus the `check_inbox` MCP tool for pull-style clients) — a queue MCP where incoming messages matching triggers wait for an agent to claim/handle them (webhook → queue → tool). Prior art worth studying, not copying (different stack): LucasQuiles/WhatSoup runs three explicit modes per WhatsApp instance (passive / chat / **agent**), and its agent mode spawns a CLI subprocess (`claude-cli` default, with `codex-cli`/`gemini-cli`/`opencode-cli` fallback chain) per multi-turn session. The mode-split and fallback-chain shape transfers even though the runtime doesn't.

## Ecosystem watch

Not roadmap items, just names worth remembering next time we survey the space (July 2026 pass):

- **rmyndharis/OpenWA** — unrelated to open-wa/wa-automate-nodejs despite the name; a NestJS gateway wrapping Baileys + whatsapp-web.js as pluggable engines, 10.8k stars in ~5 months, verified as real engineering (not a star-inflation case) via its commit history, not just its README.
- **LucasQuiles/WhatSoup** — near-zero stars, genuinely sophisticated (agent-mode CLI subprocess fleet, lid_conflict reconciliation). The clearest lesson yet that stars track virality, not substance, in this space.
- **delltrak/wamcp** — the inverse lesson: an impressive README (63 tools, ban-aware rate limiting) on a repo that's been dead for 4 months. Always check `pushed_at`, not just the README.
- **mario-andreschak/mcp-whatsapp-web** — a live whatsapp-web.js-based MCP server, filling the gap left by the confirmed-dead wweb-mcp. Not yet deeply vetted.

## Non-goals

- **Status/stories posting** — documented ban trigger from server IPs; not worth the account
- **Cold outreach / bulk messaging** — ban magnet, wrong product
- **WhatsApp Business Cloud API mode** — Meta banned general-purpose AI assistants from it (Jan 2026) and it can't see personal history; the linked-device route is the only viable one for a personal agent

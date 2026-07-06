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
- [x] wa-client: chat web UI riding the bridge session (zero device slots)
- [x] Real-time push: bridge webhook → SSE → browser
- [x] Scheduled sends (queue + delivery loop + composer UI)
- [x] Send JID allowlist in the bridge (upstream issue #47)
- [x] Automated SQLite backups (online .backup, gzip, retention)
- [x] Anti-ban enabled-by-default posture for personal use (delays + typing sim, warm-up neutralized for aged accounts)

## Next (high value, low risk)

- [ ] **Media-retry path for expired media** — the bridge returns CDN 404/410 verbatim; whatsmeow supports the retry dance (`SendMediaRetryReceipt` → `events.MediaRetry` → fresh DirectPath, phone must be online). Without it, media older than ~2 weeks is permanently lost to transcription/recall/wa-client. Confirmed (code search, July 2026) that GOWA, the other actively-maintained whatsmeow bridge, hasn't implemented this either, only graceful-degradation placeholders. Shipping it would be a genuine first, not a catch-up.
- [ ] **LID/PN JID normalization audit** (raised priority) — WhatsApp is migrating chats to `@lid` JIDs; the same contact can split into two chat rows (and two recall identities). Map via whatsmeow's `SenderAlt`/`StoreLIDPNMapping` before deriving `chat_jid`. Independently confirmed as a live, current problem: WhatSoup built dedicated `lid_conflict` detection plus a reconciliation UI for exactly this, two unrelated teams hitting the same wall.
- [ ] **Presence policy decision** — the bridge pins presence "available" on every (re)connect (main.go), which suppresses WhatsApp push notifications on the paired phone and paints an always-online fingerprint. Options: mirror real activity (mautrix-style), make it configurable, or keep and document.
- [ ] **Reply/quote in send_message** (upstream #57) — schema already stores quoted_* columns; expose reply_to in bridge send + MCP tool + wa-client swipe-to-reply
- [ ] **Reactions in wa-client** (bridge endpoint exists; UI long-press → emoji row)
- [x] **Voice-note auto-transcription pipeline** (shipped 2026-07-06) — background loop transcribes incoming voice notes into `content`; recall indexes them; backend-pluggable (mlx / faster-whisper / Groq)
- [ ] **Scheduled sends as MCP tools** (`schedule_message`, `list_scheduled`, `cancel_scheduled`) so agents can schedule, not just the web UI
- [ ] **Upstream PRs**: tests fix (done in-fork), JID allowlist, scheduled sends — keep the fork mergeable and the upstream relationship warm
- [ ] **Releases + tags** — cut v0.4.0-pro; stop shipping from `main` only

## Mid-term

- [x] **go-security green** (shipped 2026-07-06) — CI pinned an exact patch (1.25.10) that missed a stdlib security fix; floated to `1.25` so setup-go always grabs the latest patch. Same bug exists upstream (FelixIsaac) — worth a PR.
- [ ] **Observability** (upstream #10/#11): Prometheus `/metrics` (messages/hr, webhook deliveries, send latency, recall index lag) + structured JSON logs
- [ ] **MCP resources for live state** — sync status, index status, scheduled queue as MCP resources instead of tool calls
- [ ] **wa-client PWA** — manifest + service worker so it installs as a phone "app"; push notifications via the existing ntfy channel
- [ ] **Per-agent token registry** — N named tokens with per-toolset scopes (WAHA 2026.4-style), not just full/readonly
- [ ] **Contact enrichment** — merge phone contacts + LID mapping into a `contacts` view so senders always resolve to names

## Long-term / exploratory

- [ ] **Multi-account** — GOWA-style multiple device sessions behind one server (needs per-session stores + routing; big lift). GOWA's concrete shape (verified July 2026): one process, N sessions via a `/devices` CRUD resource, per-request scoping through an `X-Device-Id` header/query param, WS scoping via `?device_id=`, per-device webhook override, and a separate `DB_KEYS_URI` so session keys don't live in the same DB as messages (a re-link doesn't force a full data wipe). GOWA's README claims MCP and multi-device REST can't run together (a "limitation from whatsmeow") — unverified against whatsmeow's actual API, worth testing against our own bridge before assuming it applies to us too.
- [ ] **Digest engine** — daily/weekly per-chat summaries via recall + an LLM, delivered as a WhatsApp note-to-self or ntfy push
- [ ] **Call events** (whatsmeow exposes them; no MCP surfaces them today) — missed-call triggers for agents
- [ ] **Agent inbox pattern** — a queue MCP where incoming messages matching triggers wait for an agent to claim/handle them (webhook → queue → tool). Prior art worth studying, not copying (different stack): LucasQuiles/WhatSoup runs three explicit modes per WhatsApp instance (passive / chat / **agent**), and its agent mode spawns a CLI subprocess (`claude-cli` default, with `codex-cli`/`gemini-cli`/`opencode-cli` fallback chain) per multi-turn session. The mode-split and fallback-chain shape transfers even though the runtime doesn't.

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

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

- [ ] **Media-retry path for expired media** — the bridge returns CDN 404/410 verbatim; whatsmeow supports the retry dance (`SendMediaRetryReceipt` → `events.MediaRetry` → fresh DirectPath, phone must be online). Without it, media older than ~2 weeks is permanently lost to transcription/recall/wa-client. Highest-value fix from the July 2026 whatsmeow study (docs-reference/whatsmeow).
- [ ] **LID/PN JID normalization audit** — WhatsApp is migrating chats to `@lid` JIDs; the same contact can split into two chat rows (and two recall identities). Map via whatsmeow's `SenderAlt`/`StoreLIDPNMapping` before deriving `chat_jid`.
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

- [ ] **Multi-account** — GOWA-style multiple device sessions behind one server (needs per-session stores + routing; big lift)
- [ ] **Digest engine** — daily/weekly per-chat summaries via recall + an LLM, delivered as a WhatsApp note-to-self or ntfy push
- [ ] **Call events** (whatsmeow exposes them; no MCP surfaces them today) — missed-call triggers for agents
- [ ] **Agent inbox pattern** — a queue MCP where incoming messages matching triggers wait for an agent to claim/handle them (webhook → queue → tool)

## Non-goals

- **Status/stories posting** — documented ban trigger from server IPs; not worth the account
- **Cold outreach / bulk messaging** — ban magnet, wrong product
- **WhatsApp Business Cloud API mode** — Meta banned general-purpose AI assistants from it (Jan 2026) and it can't see personal history; the linked-device route is the only viable one for a personal agent

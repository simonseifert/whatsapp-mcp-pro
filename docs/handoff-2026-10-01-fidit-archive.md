# Handoff: WhatsApp bridge issues found while archiving FIDIT chats (2026-10-01)

TL;DR: media older than a few weeks could not be downloaded at all. One fix is already
committed and deployed on Debian (not pushed). Nine more findings below, most important first.

## Resolution (2026-10-01)

All ten are addressed on `main`:

1. Kept, and the fallback now also runs when the stored URL is missing or not a Meta CDN host.
2. Media retry: a 403/404/410 from whatsmeow sends a media retry receipt and waits up to 25 s
   for the sender's phone to re-upload, then downloads from the new direct path and stores it.
   Now opt-in (`MEDIA_RETRY_ENABLED=true`): the FIDIT media download sent 172 retries in ten
   minutes and each one put a "Finished syncing" notification on the phone.
3. `/api/download` always returns an absolute path.
4. The CDN and whatsmeow paths share one writer (`store/media/<jid>/<msgid><ext>`). The
   auto-download layout is unchanged because wa-dispatch and wa-assistant read it directly, but
   `/api/download` now serves that saved copy first, which covers media WhatsApp already purged.
5. Requests from loopback that carry the valid API key skip the rate limit.
6. `POST /api/history/backfill` runs the backfill loop inside the bridge (`GET` for status),
   exposed as `request_history(action="backfill", until=...)`.
7. `/api/history/request` finds the oldest stored message itself when no anchor is given,
   sender included. Group requests from the MCP tool used to be sent without a sender.
8. Webhook `sender_name` resolves through the identity directory, then push name.
9. New `WEBHOOK_ALLOWED_ADDRS` allowlist; the live `.env` switches to it from
   `DISABLE_SSRF_CHECK=true`.
10. Not ghosts: `…082286073` is the FIDIT community parent and `…421210903549` its
    announcement group. Group info now returns `is_community`, `community_jid`,
    `is_announcement_group` and `is_announce_only`.

## Already changed (please review, then push)

**1. `/api/download` reused expired CDN URLs.** Stored media URLs carry signed, expiring
query params, so every download older than a few weeks returned `CDN returned HTTP 403`
(601 of 615 in the FIDIT group). Fix: on CDN failure, fall back to the existing but unused
`whatsapp.Client.DownloadMessageMedia`, which re-resolves `direct_path` via whatsmeow.
- Commit `45d2711` on the local branch, `whatsapp-bridge/internal/api/download.go`. Not pushed.
- Rebuilt and deployed on debian-server; bridge restarted 2026-10-01 00:17 and reconnected.
- Previous binary kept at `whatsapp-bridge/whatsapp-bridge.pre-dlfallback-20261001`.
- `go test ./internal/api/ ./internal/whatsapp/` passes.

## Open findings

**2. No media retry.** Even the whatsmeow path 403s once WhatsApp has purged the file.
The real fix is media retry: `SendMediaRetryReceipt` plus handling `events.MediaRetry` →
`DecryptMediaRetryNotification` → download with the new direct path. That asks the
sender's phone to re-upload. Workaround today: the user exports the chat with media on the phone.

**3. `/api/download` returns a path relative to the bridge's WorkingDirectory**
(`store/media/...`). Callers in another cwd can't find the file. Return an absolute path,
as `DownloadMessageMedia` already does.

**4. Three download code paths, three layouts.** `api/download.go` writes
`store/media/<jid>/<msgid><ext>`, `whatsapp/download.go` writes `<mediaDir>/<jid>/<filename>`,
and auto-download writes `store/<jid_with_underscore>/<filename>`. Sanitisation also differs.
Worth unifying.

**5. Rate limit is shared by every local client.** 100 requests/minute per IP, fixed window.
The MCP server, n8n and scripts all come from 127.0.0.1, so one bulk job (media download)
locks the MCP out for the rest of the minute. Suggest per-API-key buckets, or a higher limit
for authenticated loopback.

**6. Initial history sync only went back to Feb 2026.** `/api/history/request` works, 50
messages per call, and recovered the FIDIT group back to Oct 2025 (1,638 → 3,543 messages).
A bulk "backfill until the phone has nothing older" endpoint or MCP tool would help. Reference
loop: `~/Code/personal/education/scripts/wa_backfill.py`.

**7. `request_history` MCP tool makes the caller supply the oldest message id and timestamp
by hand.** It could look them up itself from the store.

**8. Webhook payload `sender_name` is the raw phone number/LID.** `webhook/manager.go` has
a TODO ("We'll need to handle contact lookup"). `push_name` is present, but resolving through
the `identities` table like the MCP does would make payloads usable directly.

**9. `DISABLE_SSRF_CHECK=true` is set in the running bridge's environment.** That disables
the SSRF checks for webhook URLs and media URLs. If it was set to allow loopback webhooks,
consider an explicit loopback/tailnet allowlist and turning the check back on.

**10. Ghost chats.** Three chats are named "FIDIT 25/26"; two have zero messages
(`120363402082286073@g.us`, `120363421210903549@g.us`). Possibly community sub-groups,
possibly ghosts from the LID/PN split.

## New consumer of the bridge (FYI, don't break)

Webhook config id 3, "FIDIT groups -> n8n": POSTs messages from four FIDIT chat JIDs to
`http://127.0.0.1:5678/webhook/fidit-wa-<secret>` (n8n workflow "FIDIT WhatsApp Watch",
which pings ntfy topic `fidit`). It relies on the payload fields `event_type`,
`message.chat_name`, `message.push_name`, `message.content`, `message.media_type`,
`message.filename` and `message.is_from_me`.

## Before switching the live `.env` to `WEBHOOK_ALLOWED_ADDRS` (note added 2026-10-01 00:58)

The running bridge (PID started 00:17) still has `DISABLE_SSRF_CHECK=true`, and the `.env` has not
been switched yet. The allowlist must cover both live webhooks or they stop silently:
`127.0.0.1:5678` (n8n "FIDIT WhatsApp Watch", config id 3) and `100.78.169.70:8084` (wa-web push,
config id 2). After deploying, send a message in any FIDIT chat and check `webhook_logs` for
config 3 with a 200.

Done 2026-10-01 11:05: `.env` switched (backup `.env.bak-20261001-ssrf`), bridge redeployed on
Go 1.25.13, and both webhooks logged a 200 under the new settings (config 3 "Workflow was started",
config 2 `{"success":true}`) one second after the restart.

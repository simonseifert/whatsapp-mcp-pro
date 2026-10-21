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

**Action for Simon before the next deploy:** `deploy-live.sh` no longer hard-codes the
Friday bridge. It reads other bridges from `$OTHER_BRIDGES` or, if unset, from
`~/.local/share/whatsapp-mcp-pro/deploy.env`. Create that file with:

```
OTHER_BRIDGES=friday|whatsapp-bridge-friday|8083|/home/alice/.local/share/whatsapp-friday/.env
```

One `OTHER_BRIDGES=` line per extra bridge. The file is parsed, not sourced, so the
line needs no quoting. Without it the script still deploys the main bridge, but it
won't restart or verify Friday's.

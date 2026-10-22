# BRIEF: whatsapp-mcp-pro

Project state for the `/project` skill. Adopted 2026-10-21; the tool was already
built and live before that, so the design steps don't apply.

## Track and mode

- **Track:** app. **Mode:** personal tool, published as open source.
  - Only Simon runs it, so there are no accounts, paywall or launch.
  - The repo is public and forked from FelixIsaac/whatsapp-mcp-extended, which shapes what may be committed (see below).
  - Source: README.md, ROADMAP.md, conversation with Simon 2026-10-21.
- **Stage:** live and in daily use. Work here is maintenance and roadmap items, each one a milestone with a fresh-context review.
- **Number:** Simon's own use, meaning his Claude sessions reading and sending through it every day, plus Friday and the FIDIT jobs built on the same bridge. No metric is recorded yet.

## Facts

| Fact | Source |
|-|-|
| Go bridge (whatsmeow) + Python MCP server; 37 MCP tools | CLAUDE.md |
| Runs on Simon's Debian server via systemd, straight from this repo; data is kept outside the repo | CLAUDE.md, `scripts/deploy-live.sh` |
| The M1 is a cold standby; standby and failover scripts are kept outside this repo | Simon's private infra notes (not in this public repo) |
| wa-client retired 2026-10-21: unused, showing data frozen at 2026-08-25 | DECISIONS.md D2 |
| Media retry enabled 2026-10-21 | DECISIONS.md D3 |

## What may be committed

The repo is public. Hostnames, tailnet IPs, ntfy topics, phone numbers and the
ops topology stay in Simon's private infra folder, not here. `wa-assistant.py:195`
and its README still contain a real phone JID (open question).

## Open questions

Recommended answers in brackets.

1. Real phone numbers and private paths: removed from tracked files 2026-10-22 (no history rewrite, by choice). Done.
2. Next from the reviews: the "Then" list in `docs/reviews/2026-10-21-full-review.md`
   (bare-sender participants, bare-number recipients, :8082 reachable from the
   LAN/Docker, token sprawl, send duplicates on timeout, deploy rollback holes,
   anti-ban caps, recall memory), plus the webhook raw-JID issue and the failing
   Security Scan CI job. [done 2026-10-22: fence, key rotation, idempotent sends, 463 handling (v0.5.0-pro). Next: MCP bearer tokens in 644 config.json files, per-recipient anti-ban caps, bare-sender participants, the sender-column normalisation, edit history + call log, chat-export import, /metrics]
3. wa-dispatch / wa-assistant / wa-client: marked dormant/retired in the README status table (2026-10-22). Done.
4. Multi-instance: done, see DECISIONS.md D6.
5. Should Claude sessions get Friday's number through MCP as well (a second MCP
   instance, `whatsapp-friday`)? [no for now; friday-core already owns that number]
6. OAuth remote connector: enabled 2026-10-22 via Tailscale Funnel (D9). Approve read-only.
7. Demo GIF: done 2026-10-22 (docs/assets/demo-recall.gif, fictional data).

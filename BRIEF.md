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
| Go bridge (whatsmeow) + Python MCP server; 35 MCP tools | CLAUDE.md |
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

1. Remove the real phone JID from `wa-assistant/` and its README. [yes]
2. Fix next from the 2026-10-21 review, in this order:
   - the `check_inbox` cursor race;
   - the webhook raw-JID issue;
   - the failing Security Scan CI job;
   - normalizing the `sender` column. [this order]
3. wa-dispatch and wa-assistant don't run anywhere: mark them dormant in the README, or repoint them at the data dir? [mark dormant]
4. **Multi-instance (waiting on Simon, 2026-10-21).** Two bridges already run (Simon's
   number and Friday's), set up by hand and inconsistently: different health checks,
   only one is deployed by `deploy-live.sh`, and only one is in the standby. Plan:
   - make each number an instance of one `whatsapp-bridge@` template unit;
   - give all instances one health check and one deploy;
   - include every instance in the standby sync.

   No Docker. [yes]
5. Should Claude sessions get Friday's number through MCP as well (a second MCP
   instance, `whatsapp-friday`)? [no for now; friday-core already owns that number]


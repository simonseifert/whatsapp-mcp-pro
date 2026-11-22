# wa-app M1 progress

Plan: docs/superpowers/plans/2026-11-22-wa-app-m1-chat-app.md. Branch feat/m1-chat-app
(worktree .claude/worktrees/m1). Ledger: .superpowers/sdd/2026-11-22-wa-app-m1-chat-app/progress.md.

| Task | State |
|-|-|
| 1 Server, login, shell | done, reviewed (2 fix rounds: login races, proxy-aware rate limit); deployed as a user unit on :8085, tailnet-only HTTPS via `tailscale serve` on port 8445 |
| 2 Chats list | done, reviewed (1 fix round: list grouping in memory, 15 ms on the live store) |
| 3 Chat view | done, reviewed (2 fix rounds: scroll position on older messages) |
| 4 Media and voice notes | done, reviewed (1 fix round: safe media decoding, avatar host allowlist, concurrency caps; never pings the phone except on an explicit "Try again") |
| 5 Live updates | done, reviewed (3 fix rounds: reconnect gaps, streams end on logout; 5 ms per check on the live store) |
| 6 Read state | next |
| 7-10 | to do |

Running now: `systemctl --user status wa-app` (runs from the worktree until merge; repoint ExecStart to the main checkout after the M1 merge).

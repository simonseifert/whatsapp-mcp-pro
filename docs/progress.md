# wa-app M1 progress

Plan: docs/superpowers/plans/2026-11-22-wa-app-m1-chat-app.md. Branch feat/m1-chat-app
(worktree .claude/worktrees/m1). Ledger: .superpowers/sdd/2026-11-22-wa-app-m1-chat-app/progress.md.

| Task | State |
|-|-|
| 1 Server, login, shell | done, reviewed (2 fix rounds: login races, proxy-aware rate limit); deployed as a user unit on :8085, tailnet-only HTTPS via `tailscale serve` on port 8445 |
| 2 Chats list | next |
| 3-10 | to do |

Running now: `systemctl --user status wa-app` (runs from the worktree until merge; repoint ExecStart to the main checkout after the M1 merge).

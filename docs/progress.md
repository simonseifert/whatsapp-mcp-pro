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
| 6 Read state | done, reviewed (1 fix round: phone "marked unread" dot clears after reading in the app; receipts capped at 200) |
| 7 Sending | done, reviewed by Opus (1 fix round: slow sends never look failed, retries keep their key and recipient) |
| 8 Desktop, settings, PWA | done, reviewed (1 fix round: iPhone status bar in light mode, update notice) |
| 9 Bridge contact cards | done, reviewed (1 fix round: list preview); not deployed yet |
| 10 Retire wa-client, docs | done (wa-client deleted, README App section with screenshots, DECISIONS D21, deferred nits fixed) |

Running now: `systemctl --user status wa-app` (runs from the worktree until merge; repoint ExecStart to the main checkout after the M1 merge).

# wa-app M2 progress

Plan: docs/superpowers/plans/2026-11-23-wa-app-m2-ai-drafts.md. Branch feat/m2-ai-drafts.

2026-11-23: M2 AI drafts built. Backends (`claude` CLI, Anthropic API, OpenAI-compatible
incl. local Ollama) with encrypted keys and a local-only gate; prompt builder with
untrusted-message framing; draft worker (instant when the app is open, 20 s debounce when
closed); draft card; outcomes (used, edited, ignored) and distilled per-chat rules; AI
settings screen and per-chat sheet. Every task reviewed with fix rounds; rulings in
DECISIONS D22. Not deployed yet, and the week of real use is still open.

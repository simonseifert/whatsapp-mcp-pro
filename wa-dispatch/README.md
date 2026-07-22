# wa-dispatch

**Incoming WhatsApp messages start (or wake) a Claude Code session in the right
project — so client messages are already worked on before you sit down.**

A message lands in a chat you've routed. Within ~20s either the Claude session
already open for that project is handed the message, or a new tmux tab opens
running Claude in that project's directory with the message as its first prompt.
It reads the message, researches, drafts a reply, stages a fix on a local
branch — and stops. It never sends and never pushes. You review and approve,
optionally from your phone.

Built on [whatsapp-mcp-pro](https://github.com/simonseifert/whatsapp-mcp-pro)'s
bridge, but it only needs the bridge's SQLite store and `/api/send`, so any
compatible fork works.

## Why

The gap this fills isn't "read my WhatsApp with AI" — MCP already does that.
It's that you have to *ask*. This inverts it: the work starts when the message
arrives, not when you remember to look. By the time you open the F15 tab, the
timezone bug your client reported at 3am is already diagnosed, fixed on a local
branch, and has a drafted reply waiting.

## How it works

```
   incoming message
          │
          ▼
   ┌──────────────┐   routes.json    ┌────────────────────────────────┐
   │ wa-dispatch  │ ───────────────▶ │ project session                │
   │ polls the    │  chat → project  │  · open  → typed into the pane │
   │ bridge store │                  │  · closed→ new tmux tab, -c    │
   └──────────────┘                  └────────────────────────────────┘
                                                   │ drafts a reply
                                                   ▼
                                       ┌────────────────────────┐
                                       │ wa-approve (optional)  │
                                       │ ntfy → phone: Send?    │
                                       └────────────────────────┘
```

| Session state | What happens |
|---|---|
| **open** | appended to `.wa-inbox.jsonl`, then typed into the live pane — so it responds even if idle |
| **closed** | a visible tmux tab opens in your attached session running `claude -c "<message>"`, resuming that project's previous conversation |

Bursts are batched by a run-lock plus a per-route `cooldown_min`, so a chatty
thread produces one considered run rather than twenty.

## Safety

Incoming messages are **untrusted input** — anyone who can message you can put
text in front of your agent. Two independent layers:

1. **Framing** (`prompts/prepare-frame.md`) — message content is presented as
   data, with explicit instruction never to obey commands inside it.
2. **Enforcement** (`hooks/deny.py`, a `PreToolUse` hook) — vetoes outbound and
   destructive tools *before* they run. Blocked: every WhatsApp
   send/edit/delete/react tool, `git push`, `git reset --hard`, `rm -rf`,
   `sudo`, publishes, deploys, and `curl` at the bridge's send endpoint.
   Allowed: reads, writes, local git, builds, research.

**The hook holds even under `--dangerously-skip-permissions`** — `PreToolUse`
hooks are a separate enforcement layer from the permission prompt. This is
tested, not assumed; in practice sessions have also flagged injection attempts
in message text on their own and declined to act on them.

If you'd rather not run with skip-permissions, set `CLAUDE_FLAGS=` empty in
config — but note that an unattended run will then stall at the first prompt.

## Approving from your phone (optional)

`wa-approve.py` runs next to the bridge. A session that has written a
ready-to-go reply POSTs it; you get an ntfy notification with the draft text and
**Send** / **Discard** buttons. Tapping Send relays it through the bridge.

This preserves the invariant that the model cannot send — it can only propose:

- The `/draft` response returns a **draft_id, never the approval token**. The
  token exists only in the notification's action URLs. Without that split a
  session could submit a draft, read the token from the response, and approve
  its own send — the human gate would be decorative.
- `deny.py` additionally blocks `curl` at the approve endpoints.
- Tokens are single-use and expiring; `chat_jid` and `text` are frozen at draft
  time, so approving can neither redirect nor edit the message.
- Bind it to a VPN/tailnet address or `127.0.0.1`. **Never `0.0.0.0`** — this
  endpoint can send WhatsApp messages. The installer fails if you try.

## Install

See **[INSTALL.md](INSTALL.md)**. Short version:

```bash
git clone <this repo> && cd wa-dispatch
./install.sh                  # creates config.env, then tells you to edit it
$EDITOR config.env            # bridge location, claude path, approval settings
$EDITOR routes.json           # chat → project mapping
./install.sh                  # validates, generates plists + hook wiring
launchctl load ~/Library/LaunchAgents/com.wa-dispatch.dispatcher.plist
```

Works in two topologies: **local** (bridge on the same machine — start here) or
**ssh** (bridge on an always-on box, sessions on your laptop).

## Meetings (optional)

`meet-dispatch.py` closes the same loop for calls. It polls Fathom for new
recordings, then runs **one Claude pass that splits the transcript across your
routed projects** — because a recurring catch-up covers five clients in ninety
minutes, and routing the whole thing into one project is useless.

Each project that was genuinely discussed gets a `.meet-inbox.jsonl` entry
(summary, action items with attribution, key quotes) and its session opened or
nudged. Projects that were only mentioned in passing are omitted, so a call
never wakes a session with nothing to say.

Set `FATHOM_KEY_FILE` in config to enable. Same deny hook applies: a meeting can
produce notes, drafts and staged branches, never a sent message.

```bash
./meet-dispatch.py --dry-run              # segment recent meetings, deliver nothing
./meet-dispatch.py --meeting <id>         # re-run one recording
./meet-dispatch.py --reset-state          # mark current meetings as seen
```

## Layout

```
wa-dispatch.py             WhatsApp poller + router
meet-dispatch.py           Fathom meeting splitter (optional)
wa_session.py              shared tmux/session plumbing used by both
wa-approve.py              phone-approval service (runs next to the bridge)
wa_config.py               shared config loader (config.env)
hooks/deny.py              PreToolUse enforcement — the safety floor
hooks/inbox-stop-hook.py   surfaces new messages in an already-open session
prepare-frame.md           the prepare-ahead system prompt
routes.json                your chat → project mapping (gitignored)
config.env                 your settings (gitignored)
```

`routes.json` and `config.env` are gitignored on purpose: they contain client
names, chat identifiers and local paths. Ship `*.example.*`, never your own.

## Operating

```bash
./wa-dispatch.py --dry-run     # show routing decisions, start nothing
./wa-dispatch.py --once        # single cycle
./wa-dispatch.py --reset-state # skip backlog, start from now
tail -f /tmp/wa-dispatch.log
launchctl unload ~/Library/LaunchAgents/com.wa-dispatch.dispatcher.plist   # stop
```

First run records the current max message id — it will not backfill your
history.

## Requirements

macOS (launchd + tmux), Python 3.9+ (stdlib only), `sqlite3`, `tmux`, Claude
Code, and a running WhatsApp bridge you've already paired. ntfy only if you want
phone approvals. Linux works with systemd units instead of the generated plists;
nothing else is macOS-specific.

## Honest limitations

- **macOS-first.** The installer generates launchd plists.
- **tmux required** for the visible-session path. Without it, cold messages fall
  back to a headless run that writes `PREPARED.md` instead.
- **Polling, ~20s latency.** Fine for this; the bridge does support webhooks if
  you want push.
- **Media** is surfaced as `[image]`/`[audio]`; transcription happens on request
  via the MCP, not in the dispatch path.
- **One conversation per project.** Two chats routed to the same directory share
  a session.

## Sharp edges found the hard way

Documented so you don't rediscover them:

- `claude -c` with **no prompt** aborts; with **no prior conversation** it also
  aborts rather than starting fresh. Hence the message-as-launch-prompt plus a
  `||` fallback.
- Sessions started through a wrapper report `bash`/`sh` as
  `pane_current_command`, so live-session detection matches on pane path + the
  on-screen Claude TUI. Matching the command name silently spawned duplicates.
- An **interactive** spawn hangs on the workspace-trust dialog, so routed
  projects are pre-trusted before a tab is opened.
- HTTP headers are latin-1: emoji or an accented client name in a notification
  title throws and costs you the whole push. Titles are ASCII-folded; the body
  is UTF-8 and keeps emoji.
- Whichever path delivers a message claims the shared inbox cursor, so the Stop
  hook can't re-announce what was already handed over.

## Where this is going

A proposed redesign moves the queue into Notion: capture writes a task, an
hourly runner works it, and Simon approves the one irreversible step. It is
mostly a deletion — the warm/cold detection, tmux targeting, cursors and
keystroke nudging that caused nearly every bug in the first week all go away.

Not built. See [DESIGN-notion-queue.md](DESIGN-notion-queue.md), including what
to learn from real usage before committing to it.

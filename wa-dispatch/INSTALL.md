# Installing wa-dispatch

Assumes macOS. ~15 minutes if the bridge is already running.

## 0. Prerequisites

You need a **working, paired WhatsApp bridge** before any of this is useful —
[whatsapp-mcp-pro](https://github.com/simonseifert/whatsapp-mcp-pro) or a
compatible fork. Get to the point where `store/messages.db` is filling up with
your messages. wa-dispatch reads that file; it does not set up WhatsApp for you.

Also needed: `tmux`, `sqlite3`, Python 3.9+, and Claude Code (`claude`).

Decide your topology:

| | Use when |
|---|---|
| **local** | Bridge and your Claude sessions on the same machine. Start here. |
| **ssh** | Bridge on an always-on box, sessions on your laptop. Needs passwordless SSH (`ssh <host> true` must work without a prompt). |

## 1. Clone and bootstrap

```bash
git clone <repo> ~/tools/wa-dispatch
cd ~/tools/wa-dispatch
./install.sh
```

The first run copies `config.example.env` to `config.env` and stops. That's
expected.

## 2. Configure

```bash
$EDITOR config.env
```

The settings that actually matter:

- `BRIDGE_MODE` — `local` or `ssh` (plus `BRIDGE_SSH_HOST` if ssh)
- `BRIDGE_DB` — path to `messages.db` **on the machine running the bridge**
- `BRIDGE_ENV_FILE` — the bridge's `.env` (its `API_KEY` is read from here)
- `CLAUDE_BIN` — output of `which claude`. launchd does **not** inherit your
  shell PATH, so a bare `claude` will not be found.
- `CLAUDE_FLAGS` — `--dangerously-skip-permissions` lets unattended runs work
  without stalling on prompts. The deny hook is the real safety floor. Set it
  empty if you prefer prompts and only ever run attended.

## 3. Route your chats

Find the chat identifiers you care about:

```bash
# local bridge
sqlite3 -readonly ~/path/to/messages.db \
  "SELECT jid, name FROM chats WHERE name IS NOT NULL ORDER BY name;"

# remote bridge
ssh <host> 'sqlite3 -readonly ~/path/to/messages.db "SELECT jid, name FROM chats;"'
```

Then map them to project directories:

```bash
cp routes.example.json routes.json
$EDITOR routes.json
```

```json
{
  "match": { "chat_jid": "1203xxxxxxxxxxxxxxx@g.us" },
  "label": "Acme Corp",
  "project": "~/Code/clients/acme",
  "session": "acme",
  "cooldown_min": 10,
  "enabled": true
}
```

- `match` accepts `chat_jid` (exact), or `chat_name` / `sender_name`
  (case-insensitive substring). First **enabled** match wins.
- `project` must already exist — it's a directory Claude will work in.
- `cooldown_min` batches bursts. Raise it for chatty chats.
- `"default": null` means unrouted chats are ignored entirely. **Keep it that
  way** unless you want every group you're in spawning sessions.

Start with one or two routes. Add more once you trust the output.

## 4. Install for real

```bash
./install.sh
```

It validates prerequisites, checks the bridge is reachable, verifies every
routed project directory exists, generates the launchd plists and the deny-hook
wiring, and installs the Stop hook into `~/.claude/settings.json` (additively —
your existing hooks are preserved, and a backup is written).

Fix anything it reports as `fail` and re-run. It's idempotent.

## 5. Start it

```bash
launchctl load ~/Library/LaunchAgents/com.wa-dispatch.dispatcher.plist
launchctl load ~/Library/LaunchAgents/com.wa-dispatch.approve.plist   # if enabled
tail -f /tmp/wa-dispatch.log
```

You should see `polling bridge (...) every 20s — N/M routes armed`.

## 6. Verify without waiting for a real message

```bash
./wa-dispatch.py --dry-run     # shows what it would do with recent messages

./wa-dispatch.py --inject '{"chat_jid":"<a routed jid>","chat_name":"Test",
  "sender_name":"Someone","content":"test message","media_type":"text"}'
```

The inject should open a tmux tab (or type into an existing session) for that
project. Check with `tmux list-windows`.

## Phone approvals (optional)

Needs an [ntfy](https://ntfy.sh) server — hosted or self-run — and your phone
subscribed to a topic.

1. Put the topic name in the file at `NTFY_TOPIC_FILE`, the auth token in
   `NTFY_TOKEN_FILE` (`chmod 600` both).
2. Set `APPROVE_BIND` to an address **your phone can reach** — a VPN/tailnet IP.
   Never `0.0.0.0`; this endpoint can send WhatsApp messages.
3. Set `APPROVE_PUBLIC_URL` to how the phone addresses it.
4. Re-run `./install.sh`, load the approve plist.

Test:

```bash
curl -s -X POST http://<approve-host>:8086/draft -H 'Content-Type: application/json' \
  -d '{"chat_jid":"<your own number>@s.whatsapp.net","chat_name":"me","text":"test"}'
```

A notification with Send / Discard should appear. Tapping Send delivers it.

## Troubleshooting

**Nothing happens on a new message.** `--dry-run` shows routing decisions. Most
often the chat isn't routed, or its route is `"enabled": false`.

**A tab opens but Claude isn't running in it.** Usually `CLAUDE_BIN` is wrong or
unset — launchd can't find `claude` on its default PATH.

**Cold spawn hangs on "Is this a project you trust?".** The dispatcher
pre-trusts routed projects, but only for routes that were enabled when it last
ran. Re-run `./install.sh` after adding routes.

**Messages surface twice.** Shouldn't happen — whichever path delivers a message
claims the shared cursor. If it does, check for two dispatcher processes:
`launchctl list | grep wa-dispatch`.

**Stop everything:**

```bash
launchctl unload ~/Library/LaunchAgents/com.wa-dispatch.dispatcher.plist
launchctl unload ~/Library/LaunchAgents/com.wa-dispatch.approve.plist
```

## Upgrading

`git pull && ./install.sh` — regenerates the plists, leaves `config.env` and
`routes.json` alone. Reload the services afterwards.

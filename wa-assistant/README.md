# wa-assistant

**Message yourself on WhatsApp. Claude Code answers.**

Type into your own Note-to-Self chat and a persistent Claude Code session
replies — from your phone, from a bus, from bed. Send a voice note, get a voice
note back. Send a screenshot, have it filed. Ask something big and it quietly
hands off to a stronger model and comes back when it's done.

Sibling to [`wa-dispatch`](../wa-dispatch), which routes *other people's*
messages into project sessions. This one is a conversation with yourself.

## How it works

```
   you → your own WhatsApp chat
              │
              ▼
      ┌────────────────┐  short  ┌──────────────────────────────┐
      │  wa-assistant  │────────▶│ warm claude session          │
      │  polls the     │         │ (stays alive, keeps context) │
      │  bridge store  │         └──────────────────────────────┘
      └────────────────┘  long   ┌──────────────────────────────┐
              │       ─────────▶ │ one-shot run, stronger model │
              │                  │ (threaded, doesn't block)    │
              ▼                  └──────────────────────────────┘
      reply back into the same chat
      (text, or a voice note)
```

The session is kept **warm** on purpose. A cold `claude -p` per message costs
seconds of startup and, worse, forgets the thread — so "and what about the
other one?" stops working. Long or research-shaped messages go to a separate
one-shot process instead, so a ten-minute task doesn't block "what time is my
meeting".

## Setup

```bash
cd wa-assistant
cp config.example.env config.env
./wa-assistant.py --check
```

`--check` validates everything without sending a message:

```
PASS messages.db  — /…/whatsapp-bridge/store/messages.db
PASS API_KEY  — from /…/.env
PASS bridge connected  — jid=4915112345678:10@s.whatsapp.net
PASS self jids  — 4915112345678@s.whatsapp.net, 1234567890123@lid
PASS claude binary  — /usr/local/bin/claude
PASS workdir  — /Users/you/wa-assistant-workdir
     speech in: off     speech out: off     images: off
Ready.
```

Then run it. `--once` handles a single message and exits, which is the sane way
to try it the first time:

```bash
./wa-assistant.py --once      # try it
./wa-assistant.py             # run it
```

For always-on, copy `com.example.wa-assistant.plist` into
`~/Library/LaunchAgents/`, fix the paths, and
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.wa-assistant.plist`.

### You have two identities, and that bites

Your self-chat is filed under either your phone-number jid
(`…@s.whatsapp.net`) or your LID (`…@lid`) — **and which one differs per
linked device.** Measured on one account:

| device | self-chat filed under | messages |
|---|---|---|
| laptop (`:10`) | `123456789012345@lid` | 264 |
| server (`:12`) | `4915112345678@s.whatsapp.net` | 367 |

Same conversation, same final timestamp. A daemon watching only the jid the
bridge reports would have found **nothing** on the laptop and looked simply
broken. So both are read straight from whatsmeow's device row — `/api/connection`
exposes the jid but not the lid. You should not need to set `SELF_JIDS` at all;
if `--check` shows only one identity, that's when to.

## Voice

Both directions are off by default and take a shell command, so you can point
them at whatever you already have — local models, a hosted API, or the `say`
binary that ships with macOS. No Python dependencies either way.

**In** — `STT_CMD` gets a 16 kHz mono wav as `"$1"` and prints the transcript.
Fully local on Apple Silicon:

```
STT_ENABLED=true
STT_CMD=uv run --with parakeet-mlx python -c 'import sys;from parakeet_mlx import from_pretrained;print(from_pretrained("mlx-community/parakeet-tdt-0.6b-v2").transcribe(sys.argv[1]).text.strip())'
```

**Out** — `TTS_CMD` gets the text on stdin and an output path as `"$1"`. Free,
no key, macOS:

```
TTS_ENABLED=true
TTS_CMD=sh -c 'say -v Daniel -o "$1" "$(cat)"' sh
```

Voice notes are always answered by voice. To get a typed message answered by
voice, prefix it with 🔊. If TTS fails for any reason the reply still arrives as
text — a broken voice pipeline never costs you the answer.

## Routing to the stronger model

A message goes to the one-shot deeper run when it **starts with** one of
`HEAVY_KEYWORDS` ("research…", "plan…", "fix…"), **contains** 🧠, or is longer
than `HEAVY_LENGTH` characters. Everything else stays in the warm session.

The split is deliberately crude and entirely in config. It costs you nothing to
be wrong: guess low and you wait a few extra seconds, guess high and you get a
faster answer than you needed.

## Security

Read this before enabling it.

The session runs with `--dangerously-skip-permissions`, because there is no
human at a keyboard to approve anything. Two things keep that reasonable:

1. **Only your own messages in your own chat are read** — the query is
   `is_from_me=1 AND chat_jid IN (self jids)`. Someone messaging you cannot
   reach it. They would need your WhatsApp account.
2. **It never messages anyone but you.** Replies go back to the chat the
   message came from, which is always your own.

That still leaves: anything that can write into your self-chat can run commands
on your machine. If you want a harder boundary, point `CLAUDE_FLAGS` at the
deny hook that ships with `wa-dispatch`:

```
CLAUDE_FLAGS=--dangerously-skip-permissions --strict-mcp-config --settings ../wa-dispatch/hooks/settings.json
```

Give the session its own `WORKDIR` rather than pointing it at a real project,
and put a `CLAUDE.md` there with standing instructions ("never push", "never
send messages to anyone").

## Why the zero-width marker

Every outgoing message is prefixed with two zero-width spaces. Without it the
daemon reads its own replies back as new prompts and talks to itself until you
notice. It is invisible in the chat.

## Configuration

Every key, its default, and what it does is documented in
[`config.example.env`](config.example.env). Environment variables override the
file, so a launchd plist can change behaviour without editing config.

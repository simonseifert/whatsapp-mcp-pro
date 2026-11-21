# wa-app: product requirements

Status: approved by Simon, 2026-11-21. Decisions below come from the design
conversation with Simon on 2026-11-21 unless marked "proposal".

## What it is

A self-hosted WhatsApp chat app that runs in the browser and installs to a phone's
home screen. It rides the existing bridge (no extra linked-device slot) and adds what
WhatsApp itself doesn't have: AI reply drafts that learn how you write, voice notes as
text, and search by meaning across your whole history.

It ships in this repo for anyone who self-hosts whatsapp-mcp-pro. Every user brings
their own AI: a local model, their own API key, or their own logged-in `claude` CLI.

## Who it's for

- A self-hoster already running the bridge, mostly on a phone.
- They want WhatsApp's familiar layout, not a dashboard.
- First user: the maintainer. The design must not depend on anything only he has,
  such as his notes vault or his local models.

## Why

- Replying is the daily cost. A good draft in your own voice, one tap from sent, saves
  that time without handing control to a bot.
- Voice notes and old threads are hard to skim. Reading a transcript and searching by
  meaning beat listening and scrolling.
- The MCP already does all of this for AI agents. People should be able to use it with
  their own thumbs too.

## Goals

1. Feels like WhatsApp: same layout and gestures, with its own colours and icons, in
   dark and light themes.
2. A full chat app, not only a drafts inbox. You can read and reply in every chat
   (private chats, groups, Community groups), see media, and play or read voice notes.
3. AI drafts in opted-in chats.
   - Drafts sound like you, per chat.
   - Nothing is ever sent without a tap.
   - Drafts learn from your edits without you rating anything.
4. Surfaces the MCP's features: transcripts, search by meaning, the contact
   directory, edits and deletes, polls, and the call log.
5. Live: with the app open, new messages and drafts appear at once. With it closed,
   a phone notification arrives.
6. Safe by default.
   - Every send goes through the bridge's existing allowlists, caps and first-contact
     lock.
   - A login is required.
   - It is reachable only on your private network unless you choose otherwise.

## Non-goals

- **Calls.** whatsmeow can't place or answer calls. The Calls tab shows the call log
  only.
- **Posting statuses.** This is deliberate, because it's a ban risk. Viewing statuses
  is a later item.
- **Auto-replies or bulk sends.** No mode ever sends without a human tap.
- **A replacement for the phone's WhatsApp account.** The phone stays the primary
  device, and pairing still happens on the phone.
- **Hosting for other people.** One install serves the person whose WhatsApp it is.

## Anti-goals (failure even if it works)

- A draft goes out that you didn't approve.
- Chat content reaches a cloud AI for a chat you marked local-only.
- It looks or is named like an official WhatsApp product.
- It makes the account more likely to be banned than using WhatsApp normally.

## Drafts: decided behaviour

- **Per-chat opt-in.** Nothing is drafted until you switch a chat on. Any chat type
  can be switched on.
  - In announcement groups, it drafts only if you are an admin.
  - Any chat has a one-off "Draft" button.
- **Per-chat settings:** on or off, which AI to use (including "local only"), and an
  optional free-text note such as "formal, she's a client".
- **When drafts are made:**
  - App open: the draft starts the moment a message arrives, and the chat refreshes
    live.
  - App closed: it waits for the burst to end (proposal: 20 seconds of quiet), then
    drafts once.
- **Placement.** A draft card sits above the composer, with 1 to 2 alternatives.
  - Tap to put it in the field, then edit or send.
  - In groups the draft quotes the message it answers.
- **Learning.** Automatic, with no ratings needed.
  - **Day one:** your own past replies in that chat are the examples.
  - **After that:** every sent message is compared with the draft it came from. Edits
    become examples, and every so often they are distilled into a few plain rules per
    chat that you can read and edit.
  - **Optional:** thumbs up or down and a note. Never required.
- **Stale drafts.** If you reply from another device first, the draft disappears and
  your reply still counts as learning material.

## Success

- **Maintainer, 30 days after M2:** he uses it daily, and more than half of the
  replies in opted-in chats start from a draft.
- **Public:** another self-hoster runs it from the README alone.

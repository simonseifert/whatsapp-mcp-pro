# RESEARCH: wa-app (the chat app on top of whatsapp-mcp-pro)

Track: app, personal tool published as open source. The build-vs-adopt question is
answered in `docs/app/TECH_DESIGN.md` (approach A). This note covers only what the
design step needs. Written 2026-11-22.

## Sources

- `docs/app/PRD.md` and `docs/app/MVP.md` (approved by Simon 2026-11-21).
- Simon's taste notes: vault `07-Resources/web-design-source-map.md` (Taste section).
- Known conventions of WhatsApp, Signal, Telegram, iMessage and Beeper as messaging
  clients (from general knowledge; a Mobbin teardown was not possible, the connector
  needs a paid plan).

## What users of this category expect (must match)

1. Two-level structure: a chats list, then one chat. On desktop the two sit side by
   side. Bottom tabs on phones (Chats, Updates/Communities, Calls).
2. Chats list rows: avatar, name, last message preview with sender in groups, time,
   unread badge, pinned on top, muted icon, archived folded into one row at the top.
3. Chat view: own messages right, others left, time and ticks per message, day
   separators, reply quotes, reactions under the bubble, composer at the bottom.
4. Voice notes are a play button plus waveform and duration.
5. Dark and light themes both expected; most heavy users run dark.

## Room to stand out

1. **The draft card** is the one thing no other client has. It needs its own visual
   voice so it is never mistaken for a sent or received message (anti-goal: a draft
   goes out unapproved).
2. **Voice notes as text.** A transcript shown in place makes a chat readable at a
   glance; no mainstream client does this inline.
3. **Not official.** The PRD forbids looking like an official WhatsApp product: no
   WhatsApp green, no doodle wallpaper, no green ticks, no logo-like marks.
4. **Density.** Power users read a lot of history; a denser list than WhatsApp's 72 px
   rows is a real gain on desktop.

## What this means for the design

- F1. Keep the layout and gestures people already know (list, chat, bubbles, bottom
  tabs, two-pane desktop). Novelty goes into colour, type and the AI parts.
- F2. The draft card must read as "not sent yet" at a glance, in both themes.
- F3. Transcripts are first-class text, not a hidden extra.
- F4. Own identity: no WhatsApp green, wallpaper or tick colours.
- F5. Simon's taste: monochrome plus one accent, Geist, hairline borders, dark by
  default, no purple gradients, no glassmorphism, purposeful motion.
- F6. Messy real data: Croatian diacritics, very long names, media-only messages,
  deleted and edited messages, Community groups, polls.

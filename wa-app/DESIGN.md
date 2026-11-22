---
version: alpha
name: wa-app Familiar
description: Direction A, "Familiar, quiet and native". Picked by Simon 2026-11-22 ("A pls").
colors:
  primary: "#f0a94a"
  on-primary: "#1a1100"
  background: "#0b0b0c"
  surface: "#151517"
  surface-2: "#1d1d20"
  foreground: "#f3f3f4"
  muted-foreground: "#a0a0a7"
  border: "#ffffff1a"
  bubble-them: "#1e1e21"
  bubble-me: "#3a2b14"
  on-bubble-me: "#fbefdc"
  meta-on-bubble-me: "#d2bd9b"
  draft-surface: "#1d1810"
  danger: "#ff6b5e"
  primary-light: "#a35e05"
  primary-text-light: "#935400"
  on-primary-light: "#ffffff"
  background-light: "#f7f5f1"
  surface-light: "#efece6"
  surface-2-light: "#e7e3dc"
  foreground-light: "#151413"
  muted-foreground-light: "#67655f"
  border-light: "#0000001c"
  bubble-them-light: "#ebe8e1"
  bubble-me-light: "#f3dfb8"
  on-bubble-me-light: "#2b1d06"
  meta-on-bubble-me-light: "#6a5426"
  draft-surface-light: "#faf1e2"
  danger-light: "#b3261e"
typography:
  large-title:
    fontFamily: Geist
    fontSize: 34px
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: -0.02em
  headline:
    fontFamily: Geist
    fontSize: 17px
    fontWeight: 600
    lineHeight: 1.3
  title:
    fontFamily: Geist
    fontSize: 16px
    fontWeight: 600
    lineHeight: 1.3
  body:
    fontFamily: Geist
    fontSize: 16px
    fontWeight: 400
    lineHeight: 1.35
  body-sm:
    fontFamily: Geist
    fontSize: 15px
    fontWeight: 400
    lineHeight: 1.35
  meta:
    fontFamily: Geist
    fontSize: 13px
    fontWeight: 500
    lineHeight: 1.3
    fontFeature: "'tnum'"
  caption:
    fontFamily: Geist
    fontSize: 12px
    fontWeight: 500
    lineHeight: 1.3
spacing:
  xxs: 2px
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
  xxl: 32px
  touch: 44px
  row: 72px
  avatar: 48px
rounded:
  xs: 6px
  sm: 10px
  md: 14px
  bubble: 19px
  lg: 20px
  full: 9999px
components:
  body-text:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    typography: "{typography.body}"
  body-text-light:
    backgroundColor: "{colors.background-light}"
    textColor: "{colors.foreground-light}"
    typography: "{typography.body}"
  secondary-text:
    backgroundColor: "{colors.background}"
    textColor: "{colors.muted-foreground}"
    typography: "{typography.body-sm}"
  secondary-text-light:
    backgroundColor: "{colors.background-light}"
    textColor: "{colors.muted-foreground-light}"
    typography: "{typography.body-sm}"
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.title}"
    rounded: "{rounded.full}"
    height: 44px
    padding: 16px
  button-primary-light:
    backgroundColor: "{colors.primary-light}"
    textColor: "{colors.on-primary-light}"
    typography: "{typography.title}"
    rounded: "{rounded.full}"
    height: 44px
    padding: 16px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.md}"
    padding: 16px
  card-light:
    backgroundColor: "{colors.surface-light}"
    textColor: "{colors.foreground-light}"
    rounded: "{rounded.md}"
    padding: 16px
  list-row:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    typography: "{typography.title}"
    height: 72px
    padding: 16px
  list-row-light:
    backgroundColor: "{colors.background-light}"
    textColor: "{colors.foreground-light}"
    typography: "{typography.title}"
    height: 72px
    padding: 16px
  unread-badge:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.caption}"
    rounded: "{rounded.full}"
    size: 20px
  unread-badge-light:
    backgroundColor: "{colors.primary-light}"
    textColor: "{colors.on-primary-light}"
    typography: "{typography.caption}"
    rounded: "{rounded.full}"
    size: 20px
  bubble-them:
    backgroundColor: "{colors.bubble-them}"
    textColor: "{colors.foreground}"
    typography: "{typography.body}"
    rounded: "{rounded.bubble}"
    padding: 10px
  bubble-them-light:
    backgroundColor: "{colors.bubble-them-light}"
    textColor: "{colors.foreground-light}"
    typography: "{typography.body}"
    rounded: "{rounded.bubble}"
    padding: 10px
  bubble-me:
    backgroundColor: "{colors.bubble-me}"
    textColor: "{colors.on-bubble-me}"
    typography: "{typography.body}"
    rounded: "{rounded.bubble}"
    padding: 10px
  bubble-me-meta:
    backgroundColor: "{colors.bubble-me}"
    textColor: "{colors.meta-on-bubble-me}"
    typography: "{typography.caption}"
  bubble-me-light:
    backgroundColor: "{colors.bubble-me-light}"
    textColor: "{colors.on-bubble-me-light}"
    typography: "{typography.body}"
    rounded: "{rounded.bubble}"
    padding: 10px
  bubble-me-meta-light:
    backgroundColor: "{colors.bubble-me-light}"
    textColor: "{colors.meta-on-bubble-me-light}"
    typography: "{typography.caption}"
  draft-card:
    backgroundColor: "{colors.draft-surface}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.md}"
    padding: 12px
  draft-card-label:
    backgroundColor: "{colors.draft-surface}"
    textColor: "{colors.primary}"
    typography: "{typography.meta}"
  draft-card-light:
    backgroundColor: "{colors.draft-surface-light}"
    textColor: "{colors.foreground-light}"
    rounded: "{rounded.md}"
    padding: 12px
  draft-card-label-light:
    backgroundColor: "{colors.draft-surface-light}"
    textColor: "{colors.primary-text-light}"
    typography: "{typography.meta}"
  input-composer:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.foreground}"
    typography: "{typography.body}"
    rounded: "{rounded.full}"
    height: 44px
    padding: 16px
  input-composer-light:
    backgroundColor: "{colors.surface-light}"
    textColor: "{colors.foreground-light}"
    typography: "{typography.body}"
    rounded: "{rounded.full}"
    height: 44px
    padding: 16px
  tab-bar:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.muted-foreground}"
    typography: "{typography.caption}"
    height: 56px
  tab-bar-active:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    typography: "{typography.caption}"
  tab-bar-light:
    backgroundColor: "{colors.surface-light}"
    textColor: "{colors.muted-foreground-light}"
    typography: "{typography.caption}"
    height: 56px
  tab-bar-active-light:
    backgroundColor: "{colors.surface-light}"
    textColor: "{colors.primary-text-light}"
    typography: "{typography.caption}"
---

# wa-app design

The visual contract for the chat app in `wa-app/`. Read this and `PRODUCT.md` before
any UI work. Picked direction and its fragments: `designs/wa-app-directions/a-familiar/`
(screenshots in its `shots/`). Product truth: `docs/app/PRD.md`.

## Overview

**Direction A, "Familiar, quiet and native"**, picked by Simon on 2026-11-22 ("A pls").
Axis: the layout everyone already knows from their phone's messenger, done with
Apple-like restraint, so the only loud object on screen is the AI draft.

Why it won: the app replaces a habit people use dozens of times a day, so the layout
and gestures stay exactly where thumbs expect them (RESEARCH.md F1). Novelty goes only
into colour, type and the AI parts. Amber is the colour of "pending, not final", which
is what a draft is (F2). It carries none of WhatsApp's identity: no green, no doodle
wallpaper, no green ticks (F4). Simon's taste rules hold: Geist, hairline borders, one
accent, dark by default, no glass, no purple (F5).

Must-match conventions it keeps: chats list then chat, bottom tabs on phones, two
panes on desktop, own messages right, day separators, reply quotes, reactions under the
bubble, voice notes as play plus waveform.

## Colors

Two themes from one set of roles. Dark is the default; light follows
`prefers-color-scheme` or the user's setting. Tokens without a suffix are dark; the
`-light` tokens are the same role in the light theme.

- **background / surface / surface-2**: three neutral steps. Background is the page and
  list; surface is the composer, tab bar and cards; surface-2 is pressed rows and the
  search field.
- **foreground / muted-foreground**: names and message text / previews, times, labels.
- **border**: hairlines at 10% (dark) and 11% (light) alpha. Inset separators start at
  the text edge, not the avatar.
- **primary (amber)**: unread badges, the active tab, links, the draft card's outline,
  label and "Use" actions. Nothing else. In light the fill is `primary-light` and text
  uses the darker `primary-text-light` so it passes AA on the off-white.
- **bubble-them / bubble-me**: received bubbles are neutral; own bubbles carry a low
  amber tint with their own text and meta colours.
- **draft-surface**: a faint amber wash behind the draft card, plus a dashed
  `primary` outline.
- **danger**: only for destructive confirmations and failed sends.

Hierarchy never relies on colour alone: unread is also bold name plus a numbered
badge, the draft is also dashed, labelled and full width.

## Typography

One family: **Geist** (OFL, served self-hosted in the build), system emoji. Weights
400, 500, 600, 700 only.

- large-title 34/700: the "Chats" header only.
- headline 17/600: chat header name, sheet titles.
- title 16/600: names in rows and bubbles' sender labels in groups (at 13/600).
- body 16/400: message text. Never smaller, the composer too (stops iOS zoom).
- body-sm 15/400: list previews (two lines max, ellipsis).
- meta 13/500 with tabular figures: times, counts, "Edited", "Forwarded".
- caption 12/500: tab labels, badge digits.

Sentence case everywhere ("Show text", "Draft · replying to Nikola"). No all-caps
labels. Message text is never restyled: what people typed is shown as typed.

## Layout

- 8 px base with 2 and 4 px for fine work: `xs` 4 inside chips, `sm` 8 between bubble
  runs, `md` 12 inside bubbles and the draft card, `lg` 16 page gutter and row padding,
  `xl` 24 between sections, `xxl` 32 above the large title.
- Rows are 72 px with a 48 px avatar. Touch targets are at least 44 px (`touch`).
- Bubbles max 80% of the chat width on phones, 560 px on desktop.
- Desktop (from 900 px): two panes, list 380 px fixed, chat fills the rest with the
  conversation column capped at 760 px. Phones: one pane, the chat slides over.
- Safe areas respected (`viewport-fit=cover`), composer and tab bar sit above the
  home indicator.

## Elevation & Depth

Flat. Depth comes from the three tonal steps and hairlines, not shadows. The only
shadow is on sheets and menus that float over content (a soft 0 8 24 at 30% black
in dark, 12% in light). No blur, no glass.

## Shapes

- full: pills (composer, chips, buttons, badges), avatars are circles.
- bubble 19 px, with a tail only on the last bubble of a run from one sender.
- md 14 px: the draft card, cards, sheets' inner blocks. Deliberately squarer than
  bubbles so a draft never reads as a message.
- sm 10 px: images inside bubbles (bubble 19 minus 9 px padding, concentric), the
  reply quote block.
- xs 6 px: reaction chips' inner emoji tiles, small tags.

## Components

Defined in the front matter with both themes: body text, secondary text, primary
button, card, list row, unread badge, received and own bubbles (with meta), the draft
card and its label, the composer input, and the tab bar (idle and active).

- **List row**: avatar, name (title), time (meta, primary when unread), preview
  (body-sm, sender prefix in groups), right column for pin, mute and the badge. A small
  sparkle next to the name marks chats with drafts on.
- **Draft card**: sits above the composer, full width, dashed 1.5 px `primary` outline
  on `draft-surface`, sparkle plus "Draft · replying to <name>", the quoted message in
  muted text, 1 to 2 alternatives as full-width rows ending in "Use", and a 44 px
  dismiss. Tapping a row puts the text in the composer; it never sends.
- **Voice note**: play button, waveform, duration; "Show text" under it reveals the
  transcript in body text inside the same bubble.
- **Deleted**: outline-only bubble, italic muted "Poruka je obrisana" / "Message deleted".

## Do's and Don'ts

1. 2026-11-22, PRD anti-goal: never use WhatsApp green (#25D366, #128C7E, #075E54,
   #00A884), its doodle wallpaper, its logo or a green tick. The product must not look
   official.
2. 2026-11-22, PRD anti-goal: a draft never looks like a message. Dashed outline,
   squarer radius, full width, label. No "Send" on the draft card itself, only "Use".
3. 2026-11-22, F5: one accent. Amber marks unread, the active tab and drafts only. No
   second accent colour, no gradients, no glassmorphism.
4. 2026-11-22, F1: keep the conventional layout and gestures. Swipe to reply, long
   press for reactions, back swipe on phones. Don't move things to be different.
5. 2026-11-22, F6: design every screen with the messy cases (very long names,
   č ć š ž đ, media-only messages, deleted and edited messages, empty states).
6. 2026-11-22: message text stays 16 px everywhere, including the composer.

## Motion

Almost still: a chat app is used many times a day, so motion is feedback only.

**Tokens**: `instant` 120 ms (press feedback), `quick` 200 ms (state changes),
`move` 280 ms (pane slide on phones), `enter` 160 ms (a new message or the draft
card rising in). Exits are 2/3 of the entrance. Eases: default
`cubic-bezier(.2,0,0,1)`, decelerate `cubic-bezier(0,0,0,1)` for entrances,
accelerate `cubic-bezier(.3,0,1,1)` for exits. Rise distance 12 px. No stagger.

| Element | Property | From → to | Token, ease | Trigger | Interrupted | Reduced motion |
|-|-|-|-|-|-|-|
| New incoming/outgoing message | opacity, translateY | 0, 12px → 1, 0 | enter, decelerate | arrival while at bottom | snaps to end | fade only |
| Draft card | opacity, translateY | 0, 12px → 1, 0 | enter, decelerate | draft ready | snaps to end | fade only |
| Draft card dismiss/use | opacity, height | 1 → 0 | quick·⅔, accelerate | tap | snaps | instant |
| Chat pane (phone) | translateX | 100% → 0 | move, default | open chat / back | follows the finger | cross-fade |
| Row / button press | background | → surface-2 | instant | press | - | same |

**Still**: the chats list, tab bar, header, composer, scroll position. Nothing loops,
nothing pulses, typing indicators are three static-then-fading dots at most.

**Reduced motion**: slides become cross-fades, rises become fades; colour and focus
changes stay.

## Imagery

No marketing imagery inside the app. Avatars are the contacts' own profile photos
(cached), with neutral initials on surface-2 as the fallback. Media shows as sent,
cropped to the bubble with the sm radius. Empty states are text plus one line icon
(Lucide, 1.5 px stroke), no illustrations. Screenshots for the README use the demo
mode's fictional data only.

## Already tried

- B, "Dark statement, one loud accent" (lime on black, Geist Mono labels, squarer
  dense rows): not picked, 2026-11-22.
- C, "Reading, not chatting" (serif transcript, no bubbles, ink red): not picked,
  2026-11-22.

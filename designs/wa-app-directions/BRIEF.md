# Direction brief: wa-app chat screens

Shared by every direction. Read `../../RESEARCH.md` (findings F1 to F6),
`../../docs/app/PRD.md` and `sample-data.json` first.

## What to make

Three static HTML pages per direction, in `designs/wa-app-directions/<letter>-<slug>/`:

1. `list.html`: the Chats list at phone width (375 px). Header with search, filter
   chips (All, Unread, Favourites, Groups), the archived row, then every chat in
   `sample-data.json`, bottom tabs (Chats, Communities, Calls, Settings).
2. `chat.html`: the open group chat "Ekipa za vikend" at 375 px, with every message in
   `open_chat.messages` and the draft card (2 alternatives, quoting Nikola's message)
   above the composer. The voice note shows its transcript expanded ("Show text").
   The image can be a plain neutral placeholder block labelled "photo".
3. `desktop.html`: 1440 px two-pane, list on the left, the same chat on the right.

Each page supports both themes: dark is the default, light via `?theme=light`
(and `prefers-color-scheme`). Self-contained: inline CSS, no build step, fonts from a
CDN are fine (Geist via Google Fonts or jsDelivr). No JavaScript frameworks; a few
lines of JS for the theme switch is fine.

## Rules

- Use exactly the content in `sample-data.json`. Invent nothing visible beyond UI
  labels (button names, tab names). It is fictional sample data; keep it that way.
- Not official (F4): no WhatsApp green (#25D366, #128C7E, #075E54, #00A884), no doodle
  wallpaper, no WhatsApp logo or anything shaped like it. No product name in the UI;
  where a name would go, write "wa-app".
- The draft card must never look like a message (F2): different shape, label, and an
  obvious "tap to use" affordance plus a dismiss.
- The UI language is English; the messages stay Croatian/German as given.
- Hairline borders, no glassmorphism, no purple or blue-purple gradients (F5).
- 44 px minimum touch targets. Text contrast at least WCAG AA in both themes.
- Before reporting, run
  `~/.claude/skills/impeccable/plugin/skills/impeccable/scripts/impeccable detect --json <file>.html`
  on each page and fix "slop" hits unless your axis chose them on purpose.
- Screenshot your pages yourself, serving the folder on YOUR port only:
  `python3 -m http.server <port> --directory designs/wa-app-directions` then
  `python3 ~/.claude/skills/project/scripts/shots.py --url http://127.0.0.1:<port>/<dir> --pages /list.html /chat.html --widths 375 --out designs/wa-app-directions/<dir>/shots`
  and the same for `/desktop.html` at `--widths 1440`, each again with `--dark` removed
  and `?theme=light` (check shots.py --help for the exact flags). LOOK at every
  screenshot before you report, and fix what looks wrong. Stop the server afterwards.
- Write `NOTES.md` in your folder: the axis (one line), the finding it answers, the
  motion idea (one line), the motif only this product could have, the reference it
  borrows from and what exactly it borrows, and "Unsourced copy" (any visible text not
  from sample-data.json or a plain UI label; should be empty).

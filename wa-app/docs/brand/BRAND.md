# Brand: wa-app (working name)

**TL;DR:** one mark, an amber speech bubble with a solid "pending" dot (Simon's pick,
2026-11-22, option 2). Amber `#f0a94a` on near-black `#0b0b0c`. Use `mark-square.svg`
for anything square, `mark.svg` on the app's own dark surfaces. The public name is
still open (DECISIONS.md D18); the name never contains "WhatsApp".

## Masters (built by `src/build-masters.py`)

| File | Use |
|-|-|
| `mark.svg` | Transparent, amber. In-app header and splash on dark backgrounds. |
| `mark-square.svg` | Full-bleed dark tile. Favicon, PWA and home-screen icons, GitHub social preview. |
| `mark-round.svg` | Inside a circle, for round avatars (GitHub org, socials). |
| `mark-night.svg` | Same as square; kept for the brand-kit naming. |
| `mark-ink.svg` | One colour, near-black, for light backgrounds and print. |
| `mark-paper.svg` | One colour, off-white, on dark photos or print. |

The icon set in `icons/` comes from `mark-square.svg` via the project skill's
`icons.py`; the build copies it into the web app's `public/`.

## Rules

- **Clear space:** the dot's diameter on every side. **Minimum size:** 16 px, as the
  dark tile (the bare amber mark on a light background is too faint).
- **Colours:** `primary` #f0a94a and `background` #0b0b0c from `wa-app/DESIGN.md`;
  one-colour versions use `background` or `background-light` #f7f5f1.
- **Typeface next to the mark:** Geist 600 (OFL).
- **Don't:** recolour it green or blue (it must never read as WhatsApp, Signal or
  iMessage); stretch it or rotate it; add a phone handset, a tick or a plane; put the
  bare amber mark on light backgrounds; add a gradient or glow.

## OG image

Not made: the app is private behind a login and its pages are never shared as links.
The only public face is the GitHub repo, whose social preview gets a 1280x640 image
from `mark-square.svg` and the final name once that is decided.

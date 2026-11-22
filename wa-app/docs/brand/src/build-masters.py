#!/usr/bin/env python3
"""Build the six brand masters from the picked mark (option 2, Simon 2026-11-22).

Run from wa-app/docs/brand: python3 src/build-masters.py
Colours come from DESIGN.md tokens: primary #f0a94a, background #0b0b0c,
primary-light #a35e05, background-light #f7f5f1.
"""
AMBER, NIGHT, AMBER_INK, PAPER = "#f0a94a", "#0b0b0c", "#a35e05", "#f7f5f1"

def glyph(c, scale=1.0, dx=0.0, dy=0.0):
    t = f' transform="translate({dx} {dy}) scale({scale})"' if scale != 1 or dx or dy else ""
    return (f'<g{t}><rect x="176" y="194" width="672" height="476" rx="180" fill="none" '
            f'stroke="{c}" stroke-width="84"/>'
            f'<polygon points="572,660 696,660 734,836" fill="{c}" stroke="{c}" '
            f'stroke-width="44" stroke-linejoin="round"/>'
            f'<circle cx="512" cy="432" r="96" fill="{c}"/></g>')

def svg(body, bg=None, clip_round=False):
    out = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">'
    if bg and clip_round:
        out += f'<circle cx="512" cy="512" r="512" fill="{bg}"/>'
    elif bg:
        out += f'<rect width="1024" height="1024" fill="{bg}"/>'
    return out + body + "</svg>\n"

# The glyph's bounding box is about 134..890 x 152..878; centre it at 512,512.
centre = glyph  # already roughly centred in the 1024 tile
masters = {
    "mark.svg": svg(glyph(AMBER)),                              # transparent, for the header
    "mark-square.svg": svg(glyph(AMBER), NIGHT),                # full bleed, platform rounds it
    "mark-round.svg": svg(glyph(AMBER, 0.78, 113, 113), NIGHT, clip_round=True),
    "mark-night.svg": svg(glyph(AMBER), NIGHT),                 # on dark backgrounds
    "mark-ink.svg": svg(glyph(NIGHT)),                          # one colour, dark on light
    "mark-paper.svg": svg(glyph(PAPER)),                        # one colour, light on dark
}
for name, data in masters.items():
    open(name, "w").write(data)
    print("wrote", name)

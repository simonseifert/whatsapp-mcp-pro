# A, "Familiar, quiet and native"

**Axis:** the layout everyone already knows, done with Apple-like restraint. The "fits expectations" slot.

**Finding answered:** F1 (keep the known layout; novelty only in colour, type and the AI parts). Also F2 (the draft card is the one loud object) and F4 (no green, no wallpaper).

**Motion idea:** almost still; the newest message and the draft card rise in over 150 ms, nothing else moves (off under reduced motion).

**Accent: warm amber.** Dark `#f0a94a`, light `#a35e05` (darkened so text and icons pass AA on the off-white). Not green, not purple/blue, and not the terracotta/coral that reads as a default. Amber is also the colour of "pending, not final", which is exactly what a draft is, so the colour carries meaning. Own bubbles use a subdued amber tint (`#3a2b14` dark, `#f3dfb8` light). Everything else is neutral: near-black `#0b0b0c` / off-white `#f7f5f1`, hairlines at 10% alpha.

**Motif only this product has:** the draft card. A dashed amber outline, 14 px radius (bubbles are 19 px, solid, and sit at 80% width), sparkle mark, label "Draft · replying to Nikola", the quoted message, two full-width rows each ending in "Use", and a 44 px dismiss. Dashed + full width + amber means it can never pass for a message. A small sparkle next to a chat name in the list marks chats with drafts on.

**Reference borrowed:** iOS Messages and Signal on iPhone. Borrowed: large calm title, inset hairline separators that start at the text edge, rounded bubbles with a tail only on the last message of a run (computed per sender run), centred day labels, pill composer, solid tab bar with a hairline.

**Notes**
- Geist (Google Fonts) for UI, system emoji. Neutral initials avatars.
- Voice transcript is shown expanded; the toggle reads "Hide text" (its collapsed state is "Show text").
- Photo is a neutral block labelled "photo".
- Pages are static: the tab bar, draft dock and composer sit at the end of the page flow rather than fixed, so full-page screenshots show everything. Desktop likewise.
- `impeccable detect` leftovers are intended: Geist and the soft off-white are required by the brief.
- Preview port 8091 was already taken by another process (nemo-speech), so screenshots were taken from `file://` URLs, no server was started.

## Unsourced copy
UI labels only: Chats, Search, All, Unread, Favourites, Groups, Archived, Communities, Calls, Settings, New chat, Back to chats, Message, Edited, Forwarded, Hide text, photo, "4 votes", "Draft · replying to Nikola", Use, Dismiss draft. Initials in avatars are derived from names.

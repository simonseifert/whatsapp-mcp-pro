# meet-dispatch duplicate-delivery loop — root cause + fix (2026-07-29)

STAGED, not applied. Live infra — review, then apply + restart the poller yourself.

## Symptom
`.meet-inbox.jsonl` in the DMC repo filled with 11+ reworded copies of one Fathom
call (762863797), each re-firing the "a meeting just finished" session prompt every
~10-13 min. Deduping the file didn't help — the feeder re-appended.

## Root cause (a path-case mismatch, not a Fathom bug)
`meet-dispatch.cycle()` only marks a recording seen (`mark_seen`) if `deliver()`
returns True. `deliver()` returns True cleanly only when `find_claude_pane()` finds
the live session. That function compares the tmux pane's dir to the route's project
dir as **case-sensitive strings** (`wa_session.py`, `find_claude_pane` line ~101 and
`find_idle_pane` line ~252):

    if os.path.realpath(path) != target:

The DMC route is `~/Code/clients/rj-media/dmc-recruitment-dashboard` (lowercase), but
the live DMC session's pane runs in `.../RJ-media/dmc-recruitment-dashboard` (capital).
macOS's case-insensitive FS opens the same folder, but `realpath` preserves case, so
the strings differ, the pane is never matched, `deliver()` never returns clean-True,
the recording is never marked seen, and every poll re-segments (fresh summary) +
re-appends + re-nudges. Only DMC loops because only its session was opened capital-R.

## Fix 1 — case-insensitive path match (the actual fix). wa_session.py
In BOTH `find_claude_pane()` and `find_idle_pane()`, compare case-insensitively
(correct on a case-insensitive filesystem):

    - if os.path.realpath(path) != target:
    + if os.path.realpath(path).lower() != target.lower():

That alone lets the live session be found → clean nudge → `mark_seen` → loop ends.

## Fix 2 — idempotent append (defence in depth). meet-dispatch.py `deliver()`
Even with Fix 1, a delivery hiccup shouldn't stack duplicate inbox lines. Before the
unconditional append (line ~208), skip if this recording is already in the inbox:

    inbox_path = os.path.join(proj, INBOX)
    murl = meeting.get("url")
    already = False
    if murl and os.path.exists(inbox_path):
        try:
            with open(inbox_path, encoding="utf-8") as f:
                already = any(json.loads(l).get("meeting_url") == murl
                              for l in f if l.strip())
        except Exception:
            already = False
    os.makedirs(proj, exist_ok=True)
    if not already:
        with open(inbox_path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

## Immediate stop (before applying/restarting)
CAUTION: the number in the Fathom URL (762863797) is the share-URL id, NOT the API
`recording_id` that `.meet-state` tracks — those are ~9-digit ~165M values
(e.g. 166441355). Do NOT append 762863797; it won't match, won't stop the loop.

Also note the running poller loads routes.json ONCE into memory (line ~280), so
editing routes.json / disabling the DMC route does nothing until a restart. But it
re-reads `.meet-state` every cycle, so that file IS a live lever — if you have the
real recording_id, appending it stops only this meeting.

Reliable stops:
- Apply Fix 1, run `_dedupe-meet-inbox.mjs`, then restart the poller. It delivers
  ONE more time, find_claude_pane now matches (case-insensitive) → marks the
  recording seen → stops for good.
- Or `meet-dispatch.py --reset-state` (fetches recent recording_ids and marks them
  seen — reliable, but also marks other clients' recent meetings seen; only if
  nothing else is pending).

## Activate
1. Apply Fix 1 (+ Fix 2), 2. run `_dedupe-meet-inbox.mjs` in the DMC repo to clear the
backlog, 3. restart the meet-dispatch poller so it loads the new code.

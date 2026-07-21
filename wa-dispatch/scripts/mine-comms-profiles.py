#!/usr/bin/env python3
"""Derive a `## Comms profile` for each linked contact from Simon's own messages.

The drafting loop is gated on this section. `prepare-frame.md` tells the agent
to match a person's profile and to refuse rather than invent a voice — correct,
but 4 of 198 notes have one, so for almost everyone the agent is instructed to
produce nothing. Meanwhile ~4,900 of Simon's own sent messages sit in the store
as ground truth about how he actually writes to each person.

This reads that ground truth and writes the section.

Deliberate constraints:
  * only notes that already carry `wa_jid` — no guessing who a chat belongs to
  * never overwrites an existing profile; a hand-written one always wins
  * skips contacts with too few messages rather than inventing a register from
    three lines, which is how you get a confident, wrong voice
  * every profile records how many messages it was derived from, so a thin one
    is visible as thin

Must run somewhere with access to the vault. Under tmux on macOS that means
granting tmux Full Disk Access; from a terminal that already has the Documents
permission it just works.

  ./mine-comms-profiles.py --dry-run          # show what would be written
  ./mine-comms-profiles.py --limit 3          # do three, check them, then more
  ./mine-comms-profiles.py
"""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wa_config as cfg  # noqa: E402

VAULT = os.path.expanduser(os.environ.get(
    "WA_VAULT_PEOPLE", "~/Documents/Obsidian/Simon/01-People"))
MIN_MESSAGES = int(os.environ.get("MIN_MESSAGES", "15"))
SECTION = "## Comms profile"


def notes_with_jid():
    """[(path, jid, has_profile)] for every people note carrying wa_jid."""
    out = []
    for fn in sorted(os.listdir(VAULT)):
        if not fn.endswith(".md"):
            continue
        path = os.path.join(VAULT, fn)
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        m = re.search(r"^wa_jid:\s*(.+)$", text, re.M)
        if m:
            out.append((path, m.group(1).strip(), SECTION in text))
    return out


def my_messages(jid, limit=200):
    """Simon's own messages in one thread, oldest first."""
    q = ("SELECT content FROM messages WHERE chat_jid = '%s' AND is_from_me = 1 "
         "AND content IS NOT NULL AND content != '' "
         "ORDER BY rowid DESC LIMIT %d" % (jid.replace("'", "''"), limit))
    try:
        out = subprocess.run(cfg.db_command(q, json_out=False),
                             capture_output=True, text=True, timeout=60)
    except Exception:
        return []
    return [ln for ln in reversed(out.stdout.splitlines()) if ln.strip()]


PROMPT = """Below are real messages Simon sent to ONE person over time. Derive how
he writes TO THIS PERSON specifically.

Return GitHub-flavoured markdown, no preamble, no code fence, exactly:

**Register:** <one line: formality, warmth, who holds power in the relationship>
**Language:** <which language(s), and when he switches>
**Habits:** <3-5 bullets: length, punctuation, capitalisation, emoji, greetings,
how he opens and closes, typos he actually makes>
**Examples:**
> <verbatim message, unedited>
> <verbatim message, unedited>
> <verbatim message, unedited>

Rules:
- Describe what IS there, not what good writing would look like. If he is terse
  and lowercase with no punctuation, say that.
- Examples must be copied verbatim from the input, including typos. Never
  polish them — the typos are the signal.
- If the sample is too thin or too inconsistent to characterise, reply with
  exactly: INSUFFICIENT
- Never invent. This is used to draft messages that go to a real person.

MESSAGES (oldest first):
%s
"""


def derive(messages):
    sample = "\n".join(messages)[:24000]
    cmd = [cfg.path("CLAUDE_BIN") or "claude", "-p"]
    cmd += [f for f in cfg.get("CLAUDE_FLAGS").split() if f]
    cmd += [PROMPT % sample]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return None
    text = (r.stdout or "").strip()
    if not text or "INSUFFICIENT" in text[:60]:
        return None
    return text


def insert_section(text, profile, n):
    body = "%s\n\n%s\n\n*Derived from %d messages Simon sent to this person. " \
           "Edit freely — a hand-written profile is never overwritten.*\n" % (
               SECTION, profile.strip(), n)
    return text.rstrip() + "\n\n" + body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-messages", type=int, default=MIN_MESSAGES)
    args = ap.parse_args()

    if not os.path.isdir(VAULT):
        sys.exit("cannot read %s — see the TCC note in the module docstring" % VAULT)

    try:
        candidates = notes_with_jid()
    except PermissionError:
        sys.exit("permission denied reading %s.\nRun from a terminal with "
                 "Documents access, or grant tmux Full Disk Access." % VAULT)

    todo = []
    for path, jid, has in candidates:
        if has:
            continue
        msgs = my_messages(jid)
        if len(msgs) < args.min_messages:
            continue
        todo.append((path, jid, msgs))
    todo.sort(key=lambda t: -len(t[2]))
    if args.limit:
        todo = todo[:args.limit]

    print("%d linked notes, %d already have a profile, %d to mine\n"
          % (len(candidates), sum(1 for _, _, h in candidates if h), len(todo)))

    for path, jid, msgs in todo:
        name = os.path.basename(path)[:-3]
        print("  %-30s %4d messages" % (name[:30], len(msgs)), flush=True)
        if args.dry_run:
            continue
        profile = derive(msgs)
        if not profile:
            print("      -> insufficient / failed, skipped")
            continue
        text = open(path, encoding="utf-8").read()
        if SECTION in text:  # raced with a hand edit
            print("      -> profile appeared meanwhile, left alone")
            continue
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(insert_section(text, profile, len(msgs)))
        os.replace(tmp, path)
        print("      -> written")

    if args.dry_run:
        print("\nDry run. Re-run without --dry-run to write. "
              "Start with --limit 3 and read them before doing the rest.")


if __name__ == "__main__":
    main()

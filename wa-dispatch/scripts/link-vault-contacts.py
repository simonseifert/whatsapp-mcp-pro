#!/usr/bin/env python3
"""Link WhatsApp chats to Obsidian people notes via `wa_jid` frontmatter.

Sessions currently find a person's note by guessing from the chat name, which
breaks on nicknames, surnames and WhatsApp's own display names (a chat titled
"Jane Doe Ltd" against a note called "Jane"). Writing the JID into the note's
frontmatter makes the
lookup exact and reversible: given a chat, find the person; given a person,
find the chat.

Matching is deliberately conservative — only an exact, case-insensitive match
between the chat's display name and the note's title counts. Fuzzy matching a
first name would happily link the wrong human, and a wrong `## Comms profile`
is worse than none: it means drafting in a stranger's voice.

  ./link-vault-contacts.py             # dry run
  ./link-vault-contacts.py --apply
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


def chats():
    """Direct-message chats only — groups are not people."""
    q = ("SELECT jid||char(9)||COALESCE(name,'') FROM chats "
         "WHERE jid NOT LIKE '%@g.us' AND jid NOT LIKE '%@newsletter' "
         "AND name IS NOT NULL AND name != ''")
    out = subprocess.run(cfg.db_command(q, json_out=False),
                         capture_output=True, text=True, timeout=60)
    rows = []
    for line in out.stdout.splitlines():
        if "\t" in line:
            jid, name = line.split("\t", 1)
            rows.append((jid.strip(), name.strip()))
    return rows


def norm(s):
    """Normalise a display name for comparison.

    Only punctuation and case are folded — vault notes use hyphens where a
    chat name uses spaces ("Jane-Doe.md" vs "Jane Doe"). Word order and
    content are left alone, so this still cannot match two different people.
    """
    return re.sub(r"[\s\-_]+", " ", (s or "").strip().lower())


def notes():
    out = {}
    if not os.path.isdir(VAULT):
        return out
    for fn in os.listdir(VAULT):
        if fn.endswith(".md"):
            out[norm(fn[:-3])] = os.path.join(VAULT, fn)
    return out


def existing_jid(path):
    try:
        head = open(path, encoding="utf-8").read(2000)
    except OSError:
        return None
    m = re.search(r"^wa_jid:\s*(.+)$", head, re.M)
    return m.group(1).strip() if m else None


def add_frontmatter(path, jid):
    """Insert `wa_jid:` into existing frontmatter, or create a block."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            return text[:end] + "\nwa_jid: %s" % jid + text[end:]
    return "---\nwa_jid: %s\n---\n\n" % jid + text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    people = notes()
    if not people:
        sys.exit("no people notes found at %s" % VAULT)

    matched, skipped, already = [], [], []
    for jid, name in chats():
        path = people.get(norm(name))
        if not path:
            skipped.append((name, jid))
            continue
        cur = existing_jid(path)
        if cur:
            (already if cur == jid else skipped).append((name, jid))
            continue
        matched.append((name, jid, path))

    for name, jid, path in matched:
        print("  link  %-24s -> %s" % (name[:24], os.path.basename(path)))
    print("\n%d note(s) to link, %d already linked, %d chat(s) with no exact "
          "note match." % (len(matched), len(already), len(skipped)))

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the frontmatter.")
        return

    for name, jid, path in matched:
        text = add_frontmatter(path, jid)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    print("\nwrote wa_jid into %d note(s)." % len(matched))


if __name__ == "__main__":
    main()

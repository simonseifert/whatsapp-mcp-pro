#!/usr/bin/env python3
"""Merge chat rows split across @lid and phone-number JIDs.

WhatsApp delivers the same one-to-one conversation under either a hidden "@lid"
JID or the phone-number JID. Before normalization landed in the bridge, whichever
arrived got stored, so a single contact ends up as two chat rows with the
messages divided between them.

This merges the historical rows: messages move onto the phone-number JID, the
LID chat row is dropped, and the surviving row keeps the newest last-message
time. Groups (@g.us) are never touched.

Pairing is only done where the bridge can prove the two JIDs are the same
person — via whatsmeow's LID/PN mapping in whatsapp.db. Name similarity is NOT
used: "Nirmal" and "Nirmal Karl" happen to be the same person here, but merging
strangers who share a first name would be unrecoverable.

  ./merge-lid-chats.py                 # dry run, prints what would change
  ./merge-lid-chats.py --apply         # do it (backs up first)
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time

STORE = os.path.expanduser(
    os.environ.get("WA_STORE", "~/whatsapp-mcp-extended-pro/whatsapp-bridge/store"))
MESSAGES_DB = os.path.join(STORE, "messages.db")
WHATSAPP_DB = os.path.join(STORE, "whatsapp.db")


def lid_pn_pairs():
    """(lid_jid, pn_jid) pairs the device store can prove are the same person."""
    if not os.path.exists(WHATSAPP_DB):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % WHATSAPP_DB, uri=True)
    pairs = []
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            cols = [r[1].lower() for r in con.execute("PRAGMA table_info(%s)" % t)]
            has_lid = any("lid" in c for c in cols)
            has_pn = any(c in ("pn", "phone", "pn_jid", "phone_number") for c in cols)
            if not (has_lid and has_pn):
                continue
            lid_col = next(c for c in cols if "lid" in c)
            pn_col = next(c for c in cols if c in ("pn", "phone", "pn_jid", "phone_number"))
            for lid, pn in con.execute("SELECT %s, %s FROM %s" % (lid_col, pn_col, t)):
                if lid and pn:
                    pairs.append((str(lid), str(pn)))
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return pairs


def jidify(v, server):
    v = str(v)
    return v if "@" in v else "%s@%s" % (v.split(":")[0], server)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(MESSAGES_DB):
        sys.exit("no message store at %s" % MESSAGES_DB)

    pairs = lid_pn_pairs()
    if not pairs:
        print("No LID/PN mappings found in whatsapp.db.")
        print("Nothing can be merged safely — pairing must be proven, not guessed.")
        return

    con = sqlite3.connect(MESSAGES_DB)
    con.row_factory = sqlite3.Row
    counts = {r["jid"]: r["n"] for r in con.execute(
        "SELECT c.jid AS jid, (SELECT COUNT(*) FROM messages WHERE chat_jid=c.jid) AS n "
        "FROM chats c")}

    plan = []
    for lid, pn in pairs:
        lj = jidify(lid, "lid")
        pj = jidify(pn, "s.whatsapp.net")
        if lj in counts and pj in counts:
            plan.append((lj, pj, counts[lj], counts[pj]))

    if not plan:
        print("LID/PN mappings exist, but no chat is currently split across both.")
        return

    emb = 0
    try:
        emb = con.execute(
            "SELECT COUNT(*) FROM message_embeddings WHERE chat_jid LIKE '%@lid'"
        ).fetchone()[0]
    except sqlite3.Error:
        pass
    print("%-34s %-30s %s" % ("LID chat (messages)", "-> phone chat (messages)", ""))
    moved = 0
    for lj, pj, nl, npn in plan:
        print("  %-32s %6d  ->  %-28s %6d" % (lj, nl, pj, npn))
        moved += nl
    print("\n%d chat pair(s), %d messages would move." % (len(plan), moved))
    if emb:
        print("%d recall embeddings would move with them (index stays intact)." % emb)

    if not args.apply:
        print("\nDry run. Re-run with --apply to perform the merge.")
        return

    backup = MESSAGES_DB + ".pre-lid-merge-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(MESSAGES_DB, backup)
    print("\nbackup: %s" % backup)

    cur = con.cursor()
    for lj, pj, _, _ in plan:
        # (id, chat_jid) is the primary key, so a message already present under
        # the phone JID would collide. Drop those duplicates rather than abort.
        cur.execute(
            "DELETE FROM messages WHERE chat_jid=? AND id IN "
            "(SELECT id FROM messages WHERE chat_jid=?)", (lj, pj))
        cur.execute("UPDATE messages SET chat_jid=? WHERE chat_jid=?", (pj, lj))
        # The recall index is keyed by (chat_jid, message_id). Moving messages
        # without moving their embeddings would orphan 44% of the index here —
        # search would quietly stop resolving those results to a chat.
        for tbl, col in (("message_embeddings", "chat_jid"),
                         ("contact_nicknames", "jid")):
            try:
                cur.execute("DELETE FROM %s WHERE %s=? AND rowid IN "
                            "(SELECT rowid FROM %s WHERE %s=?)"
                            % (tbl, col, tbl, col), (lj, pj))
                cur.execute("UPDATE %s SET %s=? WHERE %s=?" % (tbl, col, col),
                            (pj, lj))
            except sqlite3.Error:
                pass  # table may not exist in older stores
        cur.execute(
            "UPDATE chats SET last_message_time=(SELECT MAX(last_message_time) "
            "FROM chats WHERE jid IN (?,?)) WHERE jid=?", (lj, pj, pj))
        cur.execute("DELETE FROM chats WHERE jid=?", (lj,))
    con.commit()
    print("merged %d pair(s). Restart the bridge and reload any client." % len(plan))


if __name__ == "__main__":
    main()

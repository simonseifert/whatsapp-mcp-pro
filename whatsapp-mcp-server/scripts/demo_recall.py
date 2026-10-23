"""Demo for the README GIF: recall finds messages by meaning, across languages.

Builds a throwaway store of FICTIONAL chats (Croatian, German, English), embeds
it with the real indexer, then runs the real lib.recall.recall() and prints
ranked hits. Nothing here touches a real store: the store path is required.

    python scripts/demo_recall.py --store DIR --build          # create + index
    python scripts/demo_recall.py --store DIR "when is the proposal due?"
    python scripts/demo_recall.py --store DIR --repl           # used by the .tape

Re-record the GIF with docs/assets/demo-recall.tape.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

ME = "4915500000001@s.whatsapp.net"
ANA = "4915500000102@s.whatsapp.net"
MARKO = "4915500000103@s.whatsapp.net"
LENA = "4915500000104@s.whatsapp.net"
JONAS = "4915500000105@s.whatsapp.net"
TEAM = "120363000000000001@g.us"

CHATS = {ANA: "Ana (designer)", MARKO: "Marko", LENA: "Lena", TEAM: "Team Alpha"}
NAMES = {ME: "You", ANA: "Ana", MARKO: "Marko", LENA: "Lena", JONAS: "Jonas"}

# (chat, sender, timestamp, content). All people, numbers and messages are made up.
MESSAGES = [
    (ANA, ANA, "2026-11-03 09:12:00+01:00", "Bok! Poslala sam ti prve skice za logo"),
    (ANA, ME, "2026-11-03 09:20:00+01:00", "Super, pogledat ću danas popodne"),
    (ANA, ANA, "2026-11-03 09:21:00+01:00", "Javi mi koja ti se boja najviše sviđa"),
    (ANA, ME, "2026-11-03 13:40:00+01:00", "Druga varijanta je odlična, samo font malo deblji"),
    (ANA, ANA, "2026-11-04 10:05:00+01:00", "Rok za prijedlog je petak 14. studenog, nemoj zaboraviti"),
    (ANA, ME, "2026-11-04 10:07:00+01:00", "Hvala, zapisao sam"),
    (ANA, ANA, "2026-11-05 18:30:00+01:00", "Idemo na kavu sutra?"),
    (ANA, ME, "2026-11-05 18:41:00+01:00", "Može, u 10 kod tržnice"),
    (MARKO, MARKO, "2026-11-02 20:15:00+01:00", "Jesi gledao utakmicu sinoć?"),
    (MARKO, ME, "2026-11-02 20:20:00+01:00", "Nisam, kako je završilo?"),
    (MARKO, MARKO, "2026-11-02 20:22:00+01:00", "2:1, zadnja minuta, ludilo"),
    (MARKO, MARKO, "2026-11-06 08:45:00+01:00", "Auto je na servisu do srijede, možeš me pokupiti?"),
    (MARKO, ME, "2026-11-06 08:50:00+01:00", "Naravno, javi kad završiš"),
    (MARKO, MARKO, "2026-11-08 12:00:00+01:00", "Vikend na moru? Prognoza je sunčana"),
    (LENA, LENA, "2026-11-01 11:00:00+01:00", "Hey, wie war dein Wochenende?"),
    (LENA, ME, "2026-11-01 11:30:00+01:00", "Ruhig, viel gelesen. Und deins?"),
    (LENA, LENA, "2026-11-01 11:32:00+01:00", "Wir waren wandern, total schön"),
    (LENA, LENA, "2026-11-04 15:10:00+01:00", "Wir haben uns auf 2.400 € für das Logo geeinigt"),
    (LENA, ME, "2026-11-04 15:12:00+01:00", "Perfekt, dann schicke ich die Rechnung morgen"),
    (LENA, LENA, "2026-11-04 15:13:00+01:00", "Die Hälfte vorab, den Rest nach Abnahme"),
    (LENA, LENA, "2026-11-07 09:00:00+01:00", "Kannst du mir das Rezept für den Kuchen schicken?"),
    (LENA, ME, "2026-11-07 09:30:00+01:00", "Klar, schicke ich dir heute Abend"),
    (TEAM, JONAS, "2026-11-02 09:00:00+01:00", "Morning all, standup in 10"),
    (TEAM, LENA, "2026-11-02 09:01:00+01:00", "Running 5 min late, start without me"),
    (TEAM, ME, "2026-11-02 09:30:00+01:00", "Notes from the standup are in the shared doc"),
    (TEAM, JONAS, "2026-11-03 14:00:00+01:00", "Staging is green again, thanks for the fix"),
    (TEAM, ANA, "2026-11-03 16:20:00+01:00", "New icons are uploaded to the drive"),
    (
        TEAM,
        JONAS,
        "2026-11-05 11:15:00+01:00",
        "Heads up: we're moving the launch to March, the payment provider needs more time",
    ),
    (TEAM, LENA, "2026-11-05 11:17:00+01:00", "Makes sense, better than rushing it"),
    (TEAM, ME, "2026-11-05 11:20:00+01:00", "I'll update the roadmap and tell the client"),
    (TEAM, JONAS, "2026-11-06 17:45:00+01:00", "Who's in for pizza on Friday?"),
    (TEAM, ANA, "2026-11-06 17:50:00+01:00", "Me! Margherita please"),
    (TEAM, LENA, "2026-11-07 10:00:00+01:00", "Reminder: office is closed on Monday"),
    (TEAM, JONAS, "2026-11-08 13:30:00+01:00", "Can someone review the onboarding copy?"),
    (TEAM, ME, "2026-11-08 13:45:00+01:00", "On it, will leave comments today"),
    (TEAM, ANA, "2026-11-09 09:10:00+01:00", "Do we have a budget for stock photos?"),
    (TEAM, JONAS, "2026-11-09 09:25:00+01:00", "Small one, check with Lena"),
    (MARKO, MARKO, "2026-11-09 19:00:00+01:00", "Hoćeš doći na roštilj u subotu?"),
    (LENA, LENA, "2026-11-09 20:10:00+01:00", "Gute Nacht, bis morgen!"),
    (ANA, ANA, "2026-11-09 21:00:00+01:00", "Laku noć!"),
]

SCHEMA = """
CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP, merged_into TEXT);
CREATE TABLE messages (
    id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP, is_from_me BOOLEAN,
    media_type TEXT, filename TEXT, file_length INTEGER, sender_name TEXT,
    is_system_message BOOLEAN DEFAULT 0,
    PRIMARY KEY (id, chat_jid)
);
"""

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
CYAN, GREEN, YELLOW, MAGENTA = "\033[36m", "\033[32m", "\033[33m", "\033[35m"


def build_store(store: Path) -> None:
    store.mkdir(parents=True, exist_ok=True)
    db = store / "messages.db"
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    for jid, name in CHATS.items():
        last = max(m[2] for m in MESSAGES if m[0] == jid)
        conn.execute("INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)", (jid, name, last))
    for i, (chat, sender, ts, text) in enumerate(MESSAGES, 1):
        conn.execute(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, sender_name)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"DEMO{i:04d}", chat, sender, text, ts, int(sender == ME), NAMES[sender]),
        )
    conn.commit()
    conn.close()


def show(recall_mod, query: str, limit: int) -> None:
    hits = recall_mod.recall(query, limit=limit).get("results", [])
    print()
    for rank, h in enumerate(hits, 1):
        when = h["timestamp"][:16]
        who = NAMES.get(h["sender"], h["sender"])
        print(
            f" {BOLD}{rank}{RESET}  {GREEN}{h['similarity']:.2f}{RESET}  "
            f"{CYAN}{h['chat_name']}{RESET} {DIM}·{RESET} {who} {DIM}· {when}{RESET}"
        )
        print(f"       {YELLOW}{h['content']}{RESET}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", required=True, help="directory for the demo messages.db (created by --build)")
    ap.add_argument("--build", action="store_true", help="(re)create the demo store and embed it")
    ap.add_argument("--repl", action="store_true", help="load the model, clear the screen, then read queries")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("queries", nargs="*")
    args = ap.parse_args()

    store = Path(args.store).expanduser().resolve()
    if args.build:
        build_store(store)
    if not (store / "messages.db").exists():
        print(f"no demo store at {store}; run with --build first", file=sys.stderr)
        return 1

    # lib.utils reads WA_STORE_PATH at import time.
    os.environ["WA_STORE_PATH"] = str(store)
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lib import recall as recall_mod

    if not args.build:
        logging.disable(logging.INFO)  # keep the recorded screen to the results

    if args.build:
        recall_mod._index_pending()  # synchronous: the store is tiny
        print(f"indexed {recall_mod.index_status()['embedded']} messages into {store / 'messages.db'}")

    for q in args.queries:
        print(f"{MAGENTA}recall›{RESET} {q}")
        show(recall_mod, q, args.limit)

    if args.repl:
        recall_mod._embed(["warm up"])
        print("\033[2J\033[H", end="")
        print(f"{DIM}# {len(MESSAGES)} fictional messages in Croatian, German and English. Ask in English:{RESET}\n")
        while True:
            # Prompt on stdout ourselves: input() sends it to stderr under readline.
            print(f"{MAGENTA}recall›{RESET} ", end="", flush=True)
            try:
                q = input().strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if q:
                show(recall_mod, q, args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())

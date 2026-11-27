"""Custom chat lists: names and order in app.db, members keyed on the chat's canonical JID.

The canonical JID is the one the chat list shows (`store._Conversation.jid`, the phone form when known). A chat can
change form later (phone JID to LID, a merge), so reads match a member against every form of the conversation, and a
write drops every stored form of it. A list filter shows archived chats too.
"""

import sqlite3
from typing import Any

from . import store, unread

MAX_LISTS = 20
MAX_NAME = 40
BUILTIN_CHIPS = [
    ("all", "All"),
    ("unread", "Unread"),
    ("favourites", "Favourites"),
    ("groups", "Groups"),
    ("mentions", "Mentions and replies"),
]


class ListError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _clean_name(raw: object) -> str:
    name = " ".join(raw.split()) if isinstance(raw, str) else ""
    if not name:
        raise ListError(422, "A list needs a name")
    if len(name) > MAX_NAME:
        raise ListError(422, f"A list name can be at most {MAX_NAME} characters")
    return name


def _taken(conn: sqlite3.Connection, name: str, except_id: int | None = None) -> bool:
    row = conn.execute(
        "SELECT 1 FROM lists WHERE lower(name) = lower(?) AND id <> ?", (name, except_id if except_id else -1)
    ).fetchone()
    return row is not None


def _renumber(conn: sqlite3.Connection, ordered_ids: list[int]) -> None:
    for position, list_id in enumerate(ordered_ids):
        conn.execute("UPDATE lists SET position = ? WHERE id = ?", (position, list_id))


def _ids(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT id FROM lists ORDER BY position, id")]


def list_all(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    counts = dict(conn.execute("SELECT list_id, COUNT(*) FROM list_chats GROUP BY list_id"))
    return [
        {"id": i, "name": n, "position": p, "count": counts.get(i, 0)}
        for i, n, p in conn.execute("SELECT id, name, position FROM lists ORDER BY position, id")
    ]


def create(conn: sqlite3.Connection, raw_name: object) -> dict[str, Any]:
    name = _clean_name(raw_name)
    if conn.execute("SELECT COUNT(*) FROM lists").fetchone()[0] >= MAX_LISTS:
        raise ListError(409, f"You can have at most {MAX_LISTS} lists")
    if _taken(conn, name):
        raise ListError(409, "You already have a list with that name")
    position = len(_ids(conn))
    cur = conn.execute("INSERT INTO lists (name, position) VALUES (?, ?)", (name, position))
    return {"id": cur.lastrowid, "name": name, "position": position, "count": 0}


def update(conn: sqlite3.Connection, list_id: int, *, name: object = None, position: object = None) -> None:
    ids = _ids(conn)
    if list_id not in ids:
        raise ListError(404, "List not found")
    if name is not None:
        clean = _clean_name(name)
        if _taken(conn, clean, list_id):
            raise ListError(409, "You already have a list with that name")
        conn.execute("UPDATE lists SET name = ? WHERE id = ?", (clean, list_id))
    if position is not None:
        if not isinstance(position, int) or isinstance(position, bool) or position < 0:
            raise ListError(422, "'position' must be a whole number from 0")
        ids.remove(list_id)
        ids.insert(min(position, len(ids)), list_id)
        _renumber(conn, ids)


def delete(conn: sqlite3.Connection, list_id: int) -> None:
    if list_id not in _ids(conn):
        raise ListError(404, "List not found")
    conn.execute("DELETE FROM list_chats WHERE list_id = ?", (list_id,))
    conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
    _renumber(conn, _ids(conn))


def exists(conn: sqlite3.Connection, list_id: int) -> bool:
    return conn.execute("SELECT 1 FROM lists WHERE id = ?", (list_id,)).fetchone() is not None


def _forms(ro: sqlite3.Connection, jid: str) -> tuple[str, list[str]]:
    """(canonical jid, every stored form of the chat); an unknown JID is its own only form."""
    found = unread.conversation(ro, jid)
    if found is None:
        return jid, [jid]
    conv = found[0]
    return conv.jid, list(dict.fromkeys([conv.jid, *conv.forms, jid]))


def change_chats(
    conn: sqlite3.Connection, ro: sqlite3.Connection, list_id: int, add: list[str], remove: list[str]
) -> None:
    if not exists(conn, list_id):
        raise ListError(404, "List not found")
    for jid in remove:
        _, forms = _forms(ro, jid)
        conn.executemany("DELETE FROM list_chats WHERE list_id = ? AND chat_jid = ?", [(list_id, f) for f in forms])
    for jid in add:
        canonical, forms = _forms(ro, jid)
        conn.executemany("DELETE FROM list_chats WHERE list_id = ? AND chat_jid = ?", [(list_id, f) for f in forms])
        conn.execute("INSERT INTO list_chats (list_id, chat_jid) VALUES (?, ?)", (list_id, canonical))


def members(conn: sqlite3.Connection, list_id: int) -> set[str]:
    return {r[0] for r in conn.execute("SELECT chat_jid FROM list_chats WHERE list_id = ?", (list_id,))}


def lists_of(conn: sqlite3.Connection, ro: sqlite3.Connection, jid: str) -> set[int]:
    _, forms = _forms(ro, jid)
    marks = ",".join("?" * len(forms))
    return {r[0] for r in conn.execute(f"SELECT DISTINCT list_id FROM list_chats WHERE chat_jid IN ({marks})", forms)}


def chips(conn: sqlite3.Connection) -> list[dict[str, str]]:
    out = [{"id": i, "label": label} for i, label in BUILTIN_CHIPS]
    out += [{"id": f"list:{row['id']}", "label": str(row["name"])} for row in list_all(conn)]
    return out


def valid_filter(conn: sqlite3.Connection, value: str) -> bool:
    if value in store.FILTERS:
        return True
    prefix, _, rest = value.partition(":")
    return prefix == "list" and rest.isdigit() and exists(conn, int(rest))

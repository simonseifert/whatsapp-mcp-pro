"""Local names for people WhatsApp only knows by number.

Many chats and group senders resolve to a bare phone number or a hidden @lid:
no address-book entry, no nickname, at most a self-chosen push name. This
module backs the "name the unknown numbers" loop:

1. `list_unnamed_contacts` ranks those identities by recent activity and hands
   back short context (last messages, shared groups) to infer who they are.
2. `set_contact_names` stores the names the agent (or the user) settled on.

Names go into `contact_nicknames` in messages.db — the same table
`manage_nickname` writes and the bridge folds into the `identities` directory
on its next rebuild. Nothing here talks to WhatsApp; these are local labels.

Because the bridge only rebuilds the directory every IDENTITY_SYNC_INTERVAL,
`identities.nickname` / `display_name` lag a fresh nickname by up to that long.
`identities_source` overlays the live table onto directory reads, and
`resolve_display_names` is the one place the MCP picks a person's display name
(nickname first), so a name set here shows up immediately.

The MCP never writes `identities`; the bridge owns it.
"""

import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from lib.utils import MESSAGES_DB_PATH, WHATSAPP_DB_PATH, canonical_chat_jid, logger

NAME_MAX_CHARS = 64
SET_NAMES_MAX_ITEMS = 50
UNNAMED_MAX_LIMIT = 50  # each entry carries snippets; keep one call well under the output budget
SNIPPET_MAX_CHARS = 120
SNIPPETS_PER_CONTACT = 3
SHARED_GROUPS_PER_CONTACT = 5

# Bidi overrides and isolates can make a label render as something else.
_BIDI_CONTROLS = {chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))}
_LETTER = re.compile(r"[^\W\d_]")
_WHITESPACE = re.compile(r"\s+")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _directory_ready(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "identities"):
        return False
    try:
        return conn.execute("SELECT 1 FROM identities LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def _blank(value: str | None) -> bool:
    return value is None or not str(value).strip()


def _has_letter(value: str | None) -> bool:
    return bool(value) and _LETTER.search(str(value)) is not None


def _bare(handle: str) -> str:
    """User part of a JID, without any device suffix (``123:4@s.whatsapp.net`` -> ``123``)."""
    return handle.split("@", 1)[0].split(":", 1)[0]


# --- display-name overlay ----------------------------------------------------

# The freshest nickname for a directory row, under whichever JID form it was
# stored. A nickname keyed on the canonical JID wins over one on the LID.
_LIVE_NICKNAME = """(
    SELECT n.nickname FROM contact_nicknames n
    WHERE n.jid IN (raw.jid, raw.lid, raw.phone_jid) AND TRIM(COALESCE(n.nickname, '')) <> ''
    ORDER BY n.jid = raw.jid DESC, n.updated_at DESC
    LIMIT 1
)"""

# Mirrors ResolveDisplayName in whatsapp-bridge/internal/database/identities.go,
# for a row whose stale nickname has since been removed.
_RECOMPUTED_NAME = """COALESCE(
    NULLIF(TRIM(i.full_name), ''), NULLIF(TRIM(i.first_name), ''), NULLIF(TRIM(i.business_name), ''),
    NULLIF(TRIM(i.chat_name), ''), NULLIF(TRIM(i.push_name), ''), NULLIF(TRIM(i.phone), ''),
    NULLIF(TRIM(i.redacted_phone), ''), i.jid
)"""


def identities_source(conn: sqlite3.Connection, columns: Sequence[str]) -> str:
    """A FROM-clause source equivalent to ``identities`` with live nicknames applied.

    ``nickname`` and ``display_name`` come from `contact_nicknames` when it has
    an entry for any of the row's JID forms, so a name set a moment ago is
    already visible; a nickname removed since the last rebuild stops showing.
    Every other column passes through. Falls back to the bare table on a store
    without `contact_nicknames`.
    """
    if not _table_exists(conn, "contact_nicknames"):
        return "identities"
    select: list[str] = []
    for col in columns:
        if col == "nickname":
            select.append("i.live_nickname AS nickname")
        elif col == "display_name":
            select.append(
                "CASE WHEN i.live_nickname IS NOT NULL THEN i.live_nickname"
                f" WHEN NULLIF(TRIM(i.nickname), '') IS NOT NULL THEN {_RECOMPUTED_NAME}"
                " ELSE i.display_name END AS display_name"
            )
        else:
            select.append(f"i.{col}")
    return (
        f"(SELECT {', '.join(select)} FROM "
        f"(SELECT raw.*, {_LIVE_NICKNAME} AS live_nickname FROM identities AS raw) AS i"
        ") AS identities"
    )


def _handle_forms(handle: str) -> tuple[list[str], str]:
    """Every JID form a sender/chat handle might be stored under, plus its bare user part."""
    bare = _bare(handle)
    forms = {handle, f"{bare}@s.whatsapp.net", f"{bare}@lid"}
    return sorted(forms), bare


def resolve_display_names(
    conn: sqlite3.Connection, handles: Iterable[str], nickname_only: bool = False
) -> dict[str, str]:
    """Best human name for each handle: local nickname first, then the directory's name.

    The one display-name resolver for MCP output. Accepts bare numbers, phone
    JIDs and LIDs; returns only handles that resolve to an actual name (never
    the number echoed back). With ``nickname_only`` only nicknames count —
    for places like chat lists that already carry a name of their own.
    """
    wanted = {h for h in handles if h and not h.endswith(("@g.us", "@newsletter", "@broadcast"))}
    if not wanted:
        return {}
    form_to_handles: dict[str, set[str]] = {}
    for handle in wanted:
        forms, bare = _handle_forms(handle)
        for key in (*forms, bare):
            form_to_handles.setdefault(key, set()).add(handle)

    names: dict[str, str] = {}
    keys = sorted(form_to_handles)
    try:
        if _directory_ready(conn):
            cols = ["jid", "lid", "phone_jid", "phone", "nickname", "display_name"]
            source = identities_source(conn, cols)
            for start in range(0, len(keys), 200):
                chunk = keys[start : start + 200]
                ph = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT {', '.join(cols)} FROM {source} "
                    f"WHERE jid IN ({ph}) OR lid IN ({ph}) OR phone_jid IN ({ph}) OR phone IN ({ph})",
                    tuple(chunk) * 4,
                ).fetchall()
                for jid, lid, phone_jid, phone, nickname, display_name in rows:
                    name = nickname if nickname_only else (nickname or display_name)
                    if _blank(name) or name in (jid, lid, phone_jid, phone):
                        continue
                    for key in (jid, lid, phone_jid, phone):
                        for handle in form_to_handles.get(key or "", ()):
                            names.setdefault(handle, str(name).strip())
        if len(names) < len(wanted) and _table_exists(conn, "contact_nicknames"):
            # No directory row (store predates it, or the bridge has not seen
            # this person yet): a nickname keyed on any JID form still counts.
            missing = [k for k in keys if any(h not in names for h in form_to_handles[k])]
            for start in range(0, len(missing), 500):
                chunk = missing[start : start + 500]
                ph = ",".join("?" * len(chunk))
                for jid, nickname in conn.execute(
                    f"SELECT jid, nickname FROM contact_nicknames WHERE jid IN ({ph}) ORDER BY updated_at",
                    tuple(chunk),
                ).fetchall():
                    if _blank(nickname):
                        continue
                    for handle in form_to_handles.get(jid, ()):
                        names.setdefault(handle, str(nickname).strip())
    except sqlite3.Error as e:
        logger.warning(f"Display-name resolution failed: {e}")
    return names


# --- list_unnamed_contacts ---------------------------------------------------


@dataclass
class _Candidate:
    jid: str
    kind: str
    lid: str | None
    phone_jid: str | None
    phone: str | None
    push_name: str | None
    message_count: int = 0
    last_active: str | None = None
    chat_forms: set[str] = field(default_factory=set)
    sender_forms: set[str] = field(default_factory=set)


def _is_unnamed(row: dict[str, Any]) -> bool:
    """No name the user chose or the address book holds — at most a push name."""
    if any(not _blank(row[k]) for k in ("nickname", "full_name", "first_name", "business_name")):
        return False
    chat_name = row["chat_name"]
    if row["kind"] == "group":
        # A group subject is a real name unless it is empty, the JID, or just digits.
        return not any(
            _has_letter(name) and name.strip() != row["jid"] for name in (chat_name, row["display_name"]) if name
        )
    # A DM chat name is usually the push name or the number; one with letters
    # that is neither came from a name source we should not second-guess.
    if _has_letter(chat_name) and (chat_name or "").strip().casefold() != (row["push_name"] or "").strip().casefold():
        return False
    return True


def _own_bare_ids() -> set[str]:
    """The linked account's own phone and LID user parts, to leave out of the list."""
    try:
        conn = sqlite3.connect(f"file:{WHATSAPP_DB_PATH}?mode=ro", uri=True)
    except sqlite3.Error:
        return set()
    try:
        own: set[str] = set()
        for jid, lid in conn.execute("SELECT jid, lid FROM whatsmeow_device").fetchall():
            own.update(_bare(v) for v in (jid, lid) if v)
        return own
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def _snippet(text: str | None, media_type: str | None) -> str:
    text = _WHITESPACE.sub(" ", text or "").strip()
    if not text:
        return f"[{media_type}]" if media_type else ""
    if media_type:
        text = f"[{media_type}] {text}"
    return text if len(text) <= SNIPPET_MAX_CHARS else text[: SNIPPET_MAX_CHARS - 1].rstrip() + "…"


def _iso_utc(sql_utc: str | None) -> str | None:
    return f"{sql_utc.replace(' ', 'T')}Z" if sql_utc else None


def _context(conn: sqlite3.Connection, cand: _Candidate, skip_system: bool) -> dict[str, Any]:
    chat_forms = sorted(cand.chat_forms)
    sender_forms = sorted(cand.sender_forms)
    cph = ",".join("?" * len(chat_forms))
    sph = ",".join("?" * len(sender_forms)) if sender_forms else "NULL"
    system = " AND COALESCE(m.is_system_message, 0) = 0" if skip_system else ""
    rows = conn.execute(
        f"""
        SELECT datetime(m.timestamp), m.chat_jid, c.name, m.is_from_me, m.content, m.media_type
        FROM messages m LEFT JOIN chats c ON c.jid = m.chat_jid
        WHERE (m.chat_jid IN ({cph})
               OR (m.chat_jid LIKE '%@g.us' AND m.sender IN ({sph}) AND COALESCE(m.is_from_me, 0) = 0))
          AND (TRIM(COALESCE(m.content, '')) <> '' OR m.media_type IS NOT NULL){system}
        ORDER BY datetime(m.timestamp) DESC
        LIMIT ?
        """,
        (*chat_forms, *sender_forms, SNIPPETS_PER_CONTACT),
    ).fetchall()
    recent = [
        {
            "when": _iso_utc(ts),
            "where": (chat_name or chat_jid) if chat_jid.endswith("@g.us") else "direct",
            "from": "me" if from_me else ("them" if cand.kind == "user" else "member"),
            "text": _snippet(content, media_type),
        }
        for ts, chat_jid, chat_name, from_me, content, media_type in rows
    ]
    result: dict[str, Any] = {"recent_messages": recent}
    if cand.kind == "user" and sender_forms:
        groups = conn.execute(
            f"""
            SELECT COALESCE(c.name, m.chat_jid), COUNT(*) FROM messages m LEFT JOIN chats c ON c.jid = m.chat_jid
            WHERE m.chat_jid LIKE '%@g.us' AND m.sender IN ({sph}) AND COALESCE(m.is_from_me, 0) = 0
            GROUP BY m.chat_jid ORDER BY COUNT(*) DESC LIMIT ?
            """,
            (*sender_forms, SHARED_GROUPS_PER_CONTACT),
        ).fetchall()
        result["shared_groups"] = [name for name, _ in groups]
    return result


def list_unnamed_contacts(limit: int = 20, min_messages: int = 1, include_groups: bool = False) -> dict[str, Any]:
    """People (and optionally groups) with activity but no usable name, most recent first."""
    limit = max(1, min(int(limit), UNNAMED_MAX_LIMIT))
    min_messages = max(0, int(min_messages))
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
    except sqlite3.Error as e:
        return {"available": False, "reason": str(e), "count": 0, "contacts": []}
    try:
        if not _directory_ready(conn):
            return {
                "available": False,
                "reason": "identities table missing or not yet synced by the bridge",
                "count": 0,
                "contacts": [],
            }
        cols = [
            "jid",
            "lid",
            "phone_jid",
            "phone",
            "kind",
            "first_name",
            "full_name",
            "push_name",
            "business_name",
            "nickname",
            "chat_name",
            "display_name",
        ]
        kinds = ("user", "group") if include_groups else ("user",)
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM {identities_source(conn, cols)} "
            f"WHERE kind IN ({','.join('?' * len(kinds))})",
            kinds,
        ).fetchall()

        own = _own_bare_ids()
        candidates: dict[str, _Candidate] = {}
        by_handle: dict[str, str] = {}
        for raw in rows:
            row = dict(zip(cols, raw, strict=True))
            if not _is_unnamed(row) or _bare(row["jid"]) in own:
                continue
            cand = _Candidate(
                jid=row["jid"],
                kind=row["kind"],
                lid=row["lid"],
                phone_jid=row["phone_jid"],
                phone=row["phone"],
                push_name=None if _blank(row["push_name"]) else row["push_name"].strip(),
            )
            if cand.kind == "group":
                cand.chat_forms = {cand.jid}
            else:
                jids = {j for j in (cand.jid, cand.lid, cand.phone_jid) if j}
                cand.chat_forms = set(jids)
                cand.sender_forms = jids | {_bare(j) for j in jids} | ({cand.phone} if cand.phone else set())
            for handle in cand.chat_forms | cand.sender_forms:
                by_handle.setdefault(handle, cand.jid)
            candidates[cand.jid] = cand

        def bump(cand_jid: str | None, count: int, last: str | None) -> None:
            if cand_jid is None:
                return
            cand = candidates[cand_jid]
            cand.message_count += count
            if last and (cand.last_active is None or last > cand.last_active):
                cand.last_active = last

        # Whole conversations with them (both directions), or the group itself.
        for chat_jid, count, last in conn.execute(
            "SELECT chat_jid, COUNT(*), MAX(datetime(timestamp)) FROM messages GROUP BY chat_jid"
        ):
            if chat_jid and (include_groups or not chat_jid.endswith("@g.us")):
                cand_jid = by_handle.get(chat_jid)
                if cand_jid and candidates[cand_jid].kind == ("group" if chat_jid.endswith("@g.us") else "user"):
                    bump(cand_jid, count, last)
        # What they said in groups.
        for sender, count, last in conn.execute(
            "SELECT sender, COUNT(*), MAX(datetime(timestamp)) FROM messages "
            "WHERE chat_jid LIKE '%@g.us' AND COALESCE(is_from_me, 0) = 0 AND sender IS NOT NULL GROUP BY sender"
        ):
            cand_jid = by_handle.get(sender)
            if cand_jid and candidates[cand_jid].kind == "user":
                bump(cand_jid, count, last)

        ranked = sorted(
            (c for c in candidates.values() if c.message_count >= min_messages),
            key=lambda c: (c.last_active or "", c.message_count),
            reverse=True,
        )
        skip_system = any(r[1] == "is_system_message" for r in conn.execute("PRAGMA table_info(messages)"))
        contacts = []
        for cand in ranked[:limit]:
            entry: dict[str, Any] = {"jid": cand.jid, "kind": cand.kind}
            if cand.kind == "user":
                entry.update({"phone": cand.phone, "lid": cand.lid, "push_name": cand.push_name})
            entry.update({"message_count": cand.message_count, "last_active": _iso_utc(cand.last_active)})
            entry.update(_context(conn, cand, skip_system))
            contacts.append(entry)
        return {
            "available": True,
            "count": len(contacts),
            "total_unnamed": len(ranked),
            "contacts": contacts,
            "next_step": (
                "Infer a name only from strong evidence in the snippets (a signature, 'this is X', "
                "someone addressing them by name, a clear group role). Ask the user about the rest. "
                "Then call set_contact_names with the confirmed names."
            ),
        }
    except sqlite3.Error as e:
        logger.error(f"Database error in list_unnamed_contacts: {e}")
        return {"available": False, "reason": str(e), "count": 0, "contacts": []}
    finally:
        conn.close()


# --- set_contact_names -------------------------------------------------------


def clean_name(value: Any) -> str | None:
    """A safe one-line label, or None when nothing usable is left.

    Drops control characters, line/paragraph separators and bidi overrides,
    collapses whitespace runs to one space. Emoji joiners stay.
    """
    if not isinstance(value, str):
        return None
    kept = "".join(
        " " if unicodedata.category(ch) in ("Zl", "Zp") or ch in "\t\n\r\v\f" else ch
        for ch in value
        if unicodedata.category(ch) != "Cc" or ch in "\t\n\r\v\f"
        if ch not in _BIDI_CONTROLS
    )
    cleaned = _WHITESPACE.sub(" ", kept).strip()
    return cleaned or None


def _resolve_target(conn: sqlite3.Connection, handle: str) -> tuple[str, list[str]] | None:
    """Canonical JID for a handle, plus every other JID form of the same identity.

    The handle must already be known: a row in the identity directory, or a chat.
    """
    # A bare number may be a phone number or a LID's user part (group senders
    # are often stored that way); a full JID matches only itself.
    forms = [handle] if "@" in handle else [f"{handle}@s.whatsapp.net", f"{handle}@lid"]
    phone = "" if "@" in handle else handle
    if _directory_ready(conn):
        ph = ",".join("?" * len(forms))
        row = conn.execute(
            f"""
            SELECT jid, lid, phone_jid FROM identities
            WHERE jid IN ({ph}) OR lid IN ({ph}) OR phone_jid IN ({ph}) OR (phone = ? AND phone <> '')
            ORDER BY CASE WHEN jid IN ({ph}) THEN 0 ELSE 1 END, message_count DESC
            LIMIT 1
            """,
            (*forms, *forms, *forms, phone, *forms),
        ).fetchone()
        if row:
            return row[0], [j for j in row[1:] if j and j != row[0]]
    for form in forms:
        if conn.execute("SELECT 1 FROM chats WHERE jid = ?", (form,)).fetchone():
            canonical = canonical_chat_jid(conn, form)
            return canonical, [form] if canonical != form else []
    return None


def set_contact_names(names: list[dict[str, Any]]) -> dict[str, Any]:
    """Store local nicknames for up to SET_NAMES_MAX_ITEMS identities in one call."""
    if not isinstance(names, list) or not names:
        return {"success": False, "error": "names must be a non-empty list of {jid, name} objects"}
    if len(names) > SET_NAMES_MAX_ITEMS:
        return {
            "success": False,
            "error": f"at most {SET_NAMES_MAX_ITEMS} names per call (got {len(names)}); split the batch",
        }
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH, timeout=10)
    except sqlite3.Error as e:
        return {"success": False, "error": str(e)}
    results: list[dict[str, Any]] = []
    try:
        if not _table_exists(conn, "contact_nicknames"):
            return {"success": False, "error": "contact_nicknames table missing; start the bridge once to create it"}
        for item in names:
            handle = str(item.get("jid") or "").strip() if isinstance(item, dict) else ""
            if not handle:
                results.append({"jid": None, "success": False, "error": "jid is required"})
                continue
            raw = item.get("name")
            name = clean_name(raw)
            if name is None:
                results.append({"jid": handle, "success": False, "error": "name is empty after cleaning"})
                continue
            if len(name) > NAME_MAX_CHARS:
                results.append(
                    {"jid": handle, "success": False, "error": f"name longer than {NAME_MAX_CHARS} characters"}
                )
                continue
            target = _resolve_target(conn, handle)
            if target is None:
                results.append(
                    {"jid": handle, "success": False, "error": "unknown jid: not in the identity directory or chats"}
                )
                continue
            canonical, other_forms = target
            all_forms = [canonical, *other_forms]
            ph = ",".join("?" * len(all_forms))
            prev = conn.execute(
                f"SELECT nickname FROM contact_nicknames WHERE jid IN ({ph}) "
                "ORDER BY jid = ? DESC, updated_at DESC LIMIT 1",
                (*all_forms, canonical),
            ).fetchone()
            # One nickname per person: a stale one under the LID would otherwise
            # compete with this one when the bridge folds them into the directory.
            if other_forms:
                conn.execute(
                    f"DELETE FROM contact_nicknames WHERE jid IN ({','.join('?' * len(other_forms))})",
                    tuple(other_forms),
                )
            conn.execute(
                """
                INSERT INTO contact_nicknames (jid, nickname, created_at, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(jid) DO UPDATE SET nickname = excluded.nickname, updated_at = CURRENT_TIMESTAMP
                """,
                (canonical, name),
            )
            entry: dict[str, Any] = {"jid": canonical, "success": True, "name": name}
            if canonical != handle:
                entry["input"] = handle
            if prev and prev[0]:
                entry["previous"] = prev[0]
            results.append(entry)
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        logger.error(f"Database error in set_contact_names: {e}")
        return {"success": False, "error": str(e), "results": []}
    finally:
        conn.close()
    saved = sum(1 for r in results if r["success"])
    return {"success": saved > 0, "saved": saved, "failed": len(results) - saved, "results": results}

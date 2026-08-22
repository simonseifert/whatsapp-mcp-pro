"""Read access to the unified identity directory (the `identities` table).

The bridge maintains the table; everything here is read-only. It collapses what
WhatsApp splits: the same person arrives under a phone JID on one route and a
hidden `@lid` on another, and whatsmeow's contact store keeps a separate row for
each — usually the address-book name on one and the self-chosen push name on the
other. A caller holding either identity gets the whole record in one lookup.

Every function degrades to an empty result when the table is missing or empty
(a store that predates the directory, or a bridge that has not synced yet), so
callers can fall back to the older per-source queries.
"""

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from lib.utils import MESSAGES_DB_PATH, logger

IdentityKind = str  # user | group | newsletter | broadcast

_COLUMNS = """
    jid, lid, phone_jid, phone, redacted_phone, kind,
    first_name, full_name, push_name, business_name, nickname, display_name, chat_name,
    is_business, is_contact, has_chat, message_count,
    participant_count, is_announce, owner_jid,
    first_seen, last_seen, sources, updated_at
"""


@dataclass
class Identity:
    """One directory row: a person, group, newsletter or broadcast list."""

    jid: str
    display_name: str
    kind: IdentityKind = "user"
    lid: str | None = None
    phone_jid: str | None = None
    phone: str | None = None
    redacted_phone: str | None = None
    first_name: str | None = None
    full_name: str | None = None
    push_name: str | None = None
    business_name: str | None = None
    nickname: str | None = None
    chat_name: str | None = None
    is_business: bool = False
    is_contact: bool = False
    has_chat: bool = False
    message_count: int = 0
    participant_count: int | None = None
    is_announce: bool = False
    owner_jid: str | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    sources: list[str] = field(default_factory=list)
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary for structured tool output.

        Group-only and person-only fields are omitted rather than emitted as
        nulls — the directory holds both kinds, and a caller reading a group
        should not have to skip past six empty name fields.
        """
        result: dict[str, Any] = {
            "jid": self.jid,
            "display_name": self.display_name,
            "kind": self.kind,
            "has_chat": self.has_chat,
            "message_count": self.message_count,
            "last_seen": self.last_seen,
        }
        if self.kind == "group":
            result.update(
                {
                    "participant_count": self.participant_count,
                    "is_announce": self.is_announce,
                    "owner_jid": self.owner_jid,
                }
            )
        else:
            result.update(
                {
                    "lid": self.lid,
                    "phone_jid": self.phone_jid,
                    "phone": self.phone,
                    "is_contact": self.is_contact,
                    "is_business": self.is_business,
                    "names": {
                        "nickname": self.nickname,
                        "full_name": self.full_name,
                        "first_name": self.first_name,
                        "push_name": self.push_name,
                        "business_name": self.business_name,
                        "chat_name": self.chat_name,
                    },
                }
            )
            if self.redacted_phone and not self.phone:
                result["redacted_phone"] = self.redacted_phone
        result["first_seen"] = self.first_seen
        result["sources"] = self.sources
        result["updated_at"] = self.updated_at
        return result


def _row_to_identity(row: tuple) -> Identity:
    return Identity(
        jid=row[0],
        lid=row[1],
        phone_jid=row[2],
        phone=row[3],
        redacted_phone=row[4],
        kind=row[5] or "user",
        first_name=row[6],
        full_name=row[7],
        push_name=row[8],
        business_name=row[9],
        nickname=row[10],
        display_name=row[11] or row[0],
        chat_name=row[12],
        is_business=bool(row[13]),
        is_contact=bool(row[14]),
        has_chat=bool(row[15]),
        message_count=row[16] or 0,
        participant_count=row[17],
        is_announce=bool(row[18]),
        owner_jid=row[19],
        first_seen=row[20],
        last_seen=row[21],
        sources=[s for s in (row[22] or "").split(",") if s],
        updated_at=row[23],
    )


def directory_available(conn: sqlite3.Connection | None = None) -> bool:
    """Whether the directory exists and has been populated at least once.

    Checked per call rather than cached: the bridge can create and fill the
    table while a long-lived MCP session is running, and a cached False would
    pin that session to the fallback path for its whole lifetime.
    """
    own_conn = conn is None
    try:
        conn = conn or sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'identities'")
        if cursor.fetchone() is None:
            return False
        return conn.execute("SELECT 1 FROM identities LIMIT 1").fetchone() is not None
    except sqlite3.Error as e:
        logger.debug(f"Identity directory unavailable: {e}")
        return False
    finally:
        if own_conn and conn is not None:
            conn.close()


def get_identity(value: str) -> Identity | None:
    """Look up one identity by any handle we know it under.

    Accepts a canonical JID, a LID, a phone JID, a bare phone number, or an
    exact display name. Anything a message, a chat row or a human is likely to
    be holding resolves to the same record.
    """
    if not value or not value.strip():
        return None
    value = value.strip()
    bare = value.split("@")[0]

    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        if not directory_available(conn):
            return None
        cursor = conn.execute(
            f"""
            SELECT {_COLUMNS} FROM identities
            WHERE jid = ? OR lid = ? OR phone_jid = ? OR phone = ?
               OR lid = ? OR phone_jid = ?
               OR LOWER(display_name) = LOWER(?)
            ORDER BY CASE WHEN jid = ? THEN 0 ELSE 1 END, message_count DESC
            LIMIT 1
            """,
            (
                value,
                value,
                value,
                bare,
                f"{bare}@lid",
                f"{bare}@s.whatsapp.net",
                value,
                value,
            ),
        )
        row = cursor.fetchone()
        return _row_to_identity(row) if row else None
    except sqlite3.Error as e:
        logger.error(f"Database error in get_identity: {e}")
        return None
    finally:
        if "conn" in locals():
            conn.close()


def resolve_jid(value: str) -> str | None:
    """Return the canonical JID for any handle, or None if unknown."""
    identity = get_identity(value)
    return identity.jid if identity else None


def list_identities(
    query: str | None = None,
    kind: str | None = None,
    has_chat: bool | None = None,
    is_contact: bool | None = None,
    sort_by: str = "last_active",
    limit: int = 50,
    page: int = 0,
) -> list[Identity]:
    """Search the directory.

    `query` matches any name, any JID form, and the phone number, so a caller
    can pass whatever fragment it has without knowing which field it belongs to.
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        if not directory_available(conn):
            return []

        where: list[str] = []
        params: list[Any] = []

        if query:
            pattern = f"%{query.strip()}%"
            where.append(
                """(
                    LOWER(COALESCE(display_name, '')) LIKE LOWER(?)
                    OR LOWER(COALESCE(full_name, '')) LIKE LOWER(?)
                    OR LOWER(COALESCE(first_name, '')) LIKE LOWER(?)
                    OR LOWER(COALESCE(push_name, '')) LIKE LOWER(?)
                    OR LOWER(COALESCE(business_name, '')) LIKE LOWER(?)
                    OR LOWER(COALESCE(nickname, '')) LIKE LOWER(?)
                    OR LOWER(COALESCE(chat_name, '')) LIKE LOWER(?)
                    OR jid LIKE ? OR COALESCE(lid, '') LIKE ? OR COALESCE(phone, '') LIKE ?
                )"""
            )
            params.extend([pattern] * 10)

        if kind:
            where.append("kind = ?")
            params.append(kind)
        if has_chat is not None:
            where.append("has_chat = ?")
            params.append(1 if has_chat else 0)
        if is_contact is not None:
            where.append("is_contact = ?")
            params.append(1 if is_contact else 0)

        order = {
            "last_active": "last_seen DESC, message_count DESC",
            "name": "display_name COLLATE NOCASE",
            "messages": "message_count DESC",
        }.get(sort_by, "last_seen DESC, message_count DESC")

        sql = f"SELECT {_COLUMNS} FROM identities"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        params.extend([limit, page * limit])

        return [_row_to_identity(row) for row in conn.execute(sql, tuple(params)).fetchall()]
    except sqlite3.Error as e:
        logger.error(f"Database error in list_identities: {e}")
        return []
    finally:
        if "conn" in locals():
            conn.close()


def directory_stats() -> dict[str, Any]:
    """Coverage summary — how much of the directory is actually resolved.

    Useful on its own (how many contacts do I have?) and as the answer to "why
    is this name still a phone number?": a low `linked` count means the bridge
    has not seen the LID mapping for those people yet.
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        if not directory_available(conn):
            return {"available": False, "reason": "identities table missing or not yet synced by the bridge"}

        row = conn.execute(
            """
            SELECT
                COUNT(*),
                SUM(kind = 'user'),
                SUM(kind = 'group'),
                SUM(lid IS NOT NULL AND phone_jid IS NOT NULL),
                SUM(lid IS NOT NULL AND phone_jid IS NULL),
                SUM(is_contact = 1),
                SUM(has_chat = 1),
                MAX(updated_at)
            FROM identities
            """
        ).fetchone()

        # merged_into arrives with the same migration as the directory, but a
        # store can be half-migrated; a missing column is not worth failing on.
        try:
            merged = conn.execute("SELECT COUNT(*) FROM chats WHERE merged_into IS NOT NULL").fetchone()[0]
        except sqlite3.Error:
            merged = None

        return {
            "available": True,
            "total": row[0],
            "users": row[1] or 0,
            "groups": row[2] or 0,
            "lid_and_phone_known": row[3] or 0,
            "lid_only": row[4] or 0,
            "in_address_book": row[5] or 0,
            "with_chat": row[6] or 0,
            "merged_lid_chats": merged,
            "last_synced": row[7],
        }
    except sqlite3.Error as e:
        logger.error(f"Database error in directory_stats: {e}")
        return {"available": False, "reason": str(e)}
    finally:
        if "conn" in locals():
            conn.close()

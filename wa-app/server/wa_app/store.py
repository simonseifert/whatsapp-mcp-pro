"""Read-only reader over the bridge's messages.db: the chat list now; chat view, media and state later."""

import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .config import Settings
from .mcp_lib import chat_forms, resolve_display_names
from .unread import UnreadCounter

ChatFilter = Literal["all", "unread", "favourites", "groups"]
FILTERS: tuple[str, ...] = ("all", "unread", "favourites", "groups")
GROUP_KINDS = ("group", "community_group", "announcement")
PREVIEW_MAX = 300
_PHONE_SUFFIX = "@s.whatsapp.net"


@dataclass
class LastMessage:
    id: str
    sender_name: str | None
    from_me: bool
    preview: str
    media_type: str | None
    deleted: bool
    timestamp_iso: str
    # The store keeps no delivery receipts, so there is nothing to tick yet.
    ticks: str | None = None


@dataclass
class ChatSummary:
    jid: str
    name: str
    kind: Literal["private", "group", "community_group", "announcement", "newsletter"]
    avatar_url: str
    last_message: LastMessage
    unread: int
    marked_unread: bool
    pinned: bool
    muted: bool
    archived: bool
    community_name: str | None


def open_ro(settings: Settings) -> sqlite3.Connection:
    """messages.db, read-only. The app never writes a bridge table."""
    uri = f"{(settings.store_path / 'messages.db').resolve().as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


# --- helpers ------------------------------------------------------------------------------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _marks(items: list[str]) -> str:
    return ",".join("?" * len(items))


def _chunks(items: list[str], size: int = 400) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def parse_ts(value: str | None) -> datetime | None:
    """The bridge stores 'YYYY-MM-DD HH:MM:SS+02:00'; a value with no offset is taken as UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


_FOLD_EXTRA = str.maketrans({"đ": "d", "Đ": "D", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "ð": "d", "ı": "i"})
_FOLD_MULTI = {"ß": "ss", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe", "þ": "th"}


def fold(text: str) -> str:
    """Lower-case and strip diacritics, so 'durdica' finds 'Đurđica' (stroked letters do not decompose)."""
    for src, dst in _FOLD_MULTI.items():
        text = text.replace(src, dst)
    decomposed = unicodedata.normalize("NFKD", text.translate(_FOLD_EXTRA))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _bare(jid: str) -> str:
    return jid.split("@", 1)[0].split(":", 1)[0]


def _is_number_like(text: str) -> bool:
    return re.fullmatch(r"[+\d\s()-]*", text) is not None


def _db_dir(conn: sqlite3.Connection) -> Path | None:
    for _, name, path in conn.execute("PRAGMA database_list"):
        if name == "main" and path:
            return Path(path).parent
    return None


def ogg_opus_seconds(path: Path) -> int | None:
    """Length of an Ogg/Opus file from its pre-skip and final granule position; None when unreadable."""
    try:
        with path.open("rb") as f:
            head = f.read(256)
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read()
        at = head.find(b"OpusHead")
        end = tail.rfind(b"OggS")
        if at < 0 or end < 0:
            return None
        pre_skip = int.from_bytes(head[at + 10 : at + 12], "little")
        granule = int.from_bytes(tail[end + 6 : end + 14], "little")
        if granule in (0, 2**64 - 1) or granule < pre_skip:
            return None
        return round((granule - pre_skip) / 48000)
    except OSError:
        return None


def _voice_seconds(store_dir: Path | None, chat_jids: list[str], chat_jid: str, filename: str | None) -> int | None:
    """Duration of a voice note that was auto-downloaded (the store keeps no duration column)."""
    if store_dir is None or not filename:
        return None
    name = Path(filename).name
    for jid in dict.fromkeys([chat_jid, *chat_jids]):
        if "/" in jid or "\\" in jid or jid.startswith("."):
            continue
        seconds = ogg_opus_seconds(store_dir / jid / name)
        if seconds is not None:
            return seconds
    return None


def _preview(
    content: str | None,
    media_type: str | None,
    filename: str | None,
    deleted: bool,
    voice_seconds: int | None,
) -> str:
    if deleted:
        return "Message deleted"
    text = " ".join((content or "").split())
    if media_type in ("audio", "ptt"):
        if voice_seconds is None:
            return "🎤 Voice message"
        return f"🎤 Voice message ({voice_seconds // 60}:{voice_seconds % 60:02d})"
    if media_type == "image":
        return f"📷 {text or 'Photo'}"[:PREVIEW_MAX]
    if media_type == "video":
        return f"🎥 {text or 'Video'}"[:PREVIEW_MAX]
    if media_type == "sticker":
        return "Sticker"
    if media_type == "document":
        return f"📄 {text or filename or 'Document'}"[:PREVIEW_MAX]
    if text.startswith("[Poll: ") and text.endswith("]"):
        return f"📊 Poll: {text[7:-1]}"[:PREVIEW_MAX]
    if text.startswith("[Contact: ") and text.endswith("]"):
        return f"👤 {text[10:-1]}"[:PREVIEW_MAX]
    return text[:PREVIEW_MAX]


# --- chat list ----------------------------------------------------------------------------------------------------


@dataclass
class _Conversation:
    """One person or group, possibly stored under several chat JIDs (LID and phone form)."""

    jid: str
    forms: list[str]
    rows: dict[str, str | None]  # chat jid -> chats.name, for rows that are listed (not merged away)


def _conversations(conn: sqlite3.Connection) -> list[_Conversation]:
    has_merge = "merged_into" in _columns(conn, "chats")
    where = "WHERE COALESCE(merged_into, '') = ''" if has_merge else ""
    rows = {
        jid: name
        for jid, name in conn.execute(f"SELECT jid, name FROM chats {where}")
        if jid and not jid.endswith("@broadcast")
    }
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for jid in rows:
        for form in chat_forms(conn, jid):
            parent[find(form)] = find(jid)
    groups: dict[str, list[str]] = {}
    for form in list(parent):
        groups.setdefault(find(form), []).append(form)
    out = []
    for forms in groups.values():
        listed = {j: rows[j] for j in sorted(forms) if j in rows}
        if not listed:
            continue
        phone = [j for j in listed if j.endswith(_PHONE_SUFFIX)] or [
            j for j in sorted(forms) if j.endswith(_PHONE_SUFFIX)
        ]
        out.append(_Conversation(phone[0] if phone else next(iter(listed)), sorted(forms), listed))
    return out


@dataclass
class _State:
    pinned: bool = False
    archived: bool = False
    marked_unread: bool = False
    muted_until: int = 0
    last_read_at: str | None = None
    history_unread: int | None = None


def _states(conn: sqlite3.Connection, conversations: list[_Conversation]) -> dict[str, _State]:
    if not _columns(conn, "chat_state"):
        return {}
    raw = {
        r[0]: r
        for r in conn.execute(
            "SELECT chat_jid, pinned, archived, marked_unread, muted_until, last_read_at, history_unread_count"
            " FROM chat_state"
        )
    }
    out: dict[str, _State] = {}
    for conv in conversations:
        state = _State()
        newest_read: datetime | None = None
        for form in conv.forms:
            r = raw.get(form)
            if r is None:
                continue
            state.pinned |= bool(r[1])
            state.archived |= bool(r[2])
            state.marked_unread |= bool(r[3])
            state.muted_until = r[4] if r[4] == -1 or state.muted_until == -1 else max(state.muted_until, r[4] or 0)
            read_at = parse_ts(r[5])
            if read_at and (newest_read is None or read_at > newest_read):
                newest_read, state.last_read_at = read_at, r[5]
            if r[6] is not None:
                state.history_unread = (state.history_unread or 0) + r[6]
        out[conv.jid] = state
    return out


def count_archived(conn: sqlite3.Connection) -> int:
    convs = _conversations(conn)
    states = _states(conn, convs)
    return sum(1 for c in convs if states.get(c.jid, _State()).archived)


def _last_message(conn: sqlite3.Connection, forms: list[str], extra: str) -> tuple | None:
    """Newest message over all forms. The index order is the raw string; the true order (mixed UTC offsets)
    is decided on the few newest candidates."""
    rows = conn.execute(
        f"""
        SELECT id, chat_jid, sender, sender_name, content, timestamp, COALESCE(is_from_me, 0), media_type, filename,
               {extra}
        FROM messages WHERE chat_jid IN ({_marks(forms)}) ORDER BY timestamp DESC LIMIT 20
        """,
        forms,
    ).fetchall()
    stamped = [(parse_ts(r[5]), r) for r in rows]
    stamped = [s for s in stamped if s[0] is not None]
    if not stamped:
        return rows[0] if rows else None
    return max(stamped, key=lambda s: s[0])[1]  # type: ignore[return-value,arg-type]


def _group_info(conn: sqlite3.Connection, jids: list[str]) -> dict[str, dict[str, object]]:
    cols = _columns(conn, "identities")
    if not cols:
        return {}
    wanted = [
        c
        for c in ("kind", "is_announce", "community_jid", "is_default_subgroup", "display_name", "chat_name")
        if c in cols
    ]
    info: dict[str, dict[str, object]] = {}
    for chunk in _chunks(jids):
        for row in conn.execute(
            f"SELECT jid, {', '.join(wanted)} FROM identities WHERE jid IN ({_marks(chunk)})", chunk
        ):
            info[row[0]] = dict(zip(wanted, row[1:], strict=True))
    return info


def _kind(jid: str, info: dict[str, object]) -> str:
    if jid.endswith("@newsletter"):
        return "newsletter"
    if jid.endswith("@g.us"):
        # A community's announcement group is its default subgroup; other groups can be announce-only too.
        if info.get("is_announce") or info.get("is_default_subgroup"):
            return "announcement"
        if info.get("community_jid"):
            return "community_group"
        return "group"
    return "private"


def list_chats(
    conn: sqlite3.Connection,
    appconn: sqlite3.Connection,
    *,
    filter: ChatFilter = "all",  # noqa: A002 - the API's parameter name
    archived: bool = False,
    query: str | None = None,
    limit: int = 200,
) -> list[ChatSummary]:
    convs = _conversations(conn)
    states = _states(conn, convs)
    convs = [c for c in convs if states.get(c.jid, _State()).archived == archived]

    deleted_sql = "deleted_at" if "deleted_at" in _columns(conn, "messages") else "NULL"
    store_dir = _db_dir(conn)
    now = time.time()

    # Last message per conversation; one without any message (a community shell, an empty chat) is not listed.
    found: list[tuple[_Conversation, tuple]] = []
    for conv in convs:
        last = _last_message(conn, conv.forms, deleted_sql)
        if last is not None:
            found.append((conv, last))

    group_infos = _group_info(conn, [c.jid for c, _ in found if c.jid.endswith(("@g.us", "@newsletter"))])
    kinds = {c.jid: _kind(c.jid, group_infos.get(c.jid, {})) for c, _ in found}

    # Names: nickname / address book through the shared resolver for people, the chat's own subject for groups.
    people = [form for c, _ in found if kinds[c.jid] == "private" for form in c.forms]
    resolved = resolve_display_names(conn, people)
    senders = {last[2] for c, last in found if kinds[c.jid] in GROUP_KINDS and last[2] and not last[6]}
    sender_names = resolve_display_names(conn, senders)

    counter = UnreadCounter(conn, appconn)
    community_names: dict[str, str | None] = {}
    summaries: list[ChatSummary] = []
    for conv, last in found:
        (mid, chat_jid, sender, sender_name, content, stamp, from_me, media_type, filename, deleted_at) = last
        kind = kinds[conv.jid]
        info = group_infos.get(conv.jid, {})
        state = states.get(conv.jid, _State())

        name = None
        if kind == "private":
            name = next((resolved[f].name for f in [conv.jid, *conv.forms] if f in resolved), None)
        if not name:
            for jid in [conv.jid, *conv.forms]:
                cand = (conv.rows.get(jid) or "").strip()
                if cand and not (kind == "private" and _is_number_like(cand)):
                    name = cand
                    break
        if not name:
            name = str(info.get("display_name") or info.get("chat_name") or "").strip() or None
        if not name:
            bare = _bare(conv.jid)
            name = f"+{bare}" if conv.jid.endswith(_PHONE_SUFFIX) else bare

        community_name = None
        community = info.get("community_jid")
        if isinstance(community, str) and community:
            if community not in community_names:
                row = conn.execute("SELECT name FROM chats WHERE jid = ?", (community,)).fetchone()
                fallback = _group_info(conn, [community]).get(community, {})
                community_names[community] = (
                    (row[0] if row and row[0] else None) or fallback.get("display_name") or fallback.get("chat_name")
                ) or None  # type: ignore[assignment]
            community_name = community_names[community]

        in_group = kind in GROUP_KINDS
        prefix = None
        if in_group and not from_me:
            prefix = sender_names[sender].name if sender in sender_names else (sender_name or None)
        stamped = parse_ts(stamp)
        voice = _voice_seconds(store_dir, conv.forms, chat_jid, filename) if media_type in ("audio", "ptt") else None
        muted = state.muted_until == -1 or state.muted_until > now
        summaries.append(
            ChatSummary(
                jid=conv.jid,
                name=name,
                kind=kind,  # type: ignore[arg-type]
                avatar_url=f"/api/avatar/{conv.jid}",
                last_message=LastMessage(
                    id=mid,
                    sender_name=prefix,
                    from_me=bool(from_me),
                    preview=_preview(content, media_type, filename, bool(deleted_at), voice),
                    media_type=media_type or None,
                    deleted=bool(deleted_at),
                    timestamp_iso=stamped.isoformat() if stamped else str(stamp),
                ),
                unread=counter.count(conv.forms, state.last_read_at, state.history_unread),
                marked_unread=state.marked_unread,
                pinned=state.pinned,
                muted=muted,
                archived=state.archived,
                community_name=community_name,
            )
        )

    if filter == "unread":
        summaries = [s for s in summaries if s.unread > 0 or s.marked_unread]
    elif filter == "favourites":  # M1: favourites are the pinned chats
        summaries = [s for s in summaries if s.pinned]
    elif filter == "groups":
        summaries = [s for s in summaries if s.kind in GROUP_KINDS]
    if query and query.strip():
        needle = fold(query.strip())
        digits = query.strip().lstrip("+").replace(" ", "")
        summaries = [s for s in summaries if needle in fold(s.name) or (digits.isdigit() and digits in _bare(s.jid))]
    summaries.sort(key=lambda s: (s.pinned, _sort_key(s.last_message.timestamp_iso)), reverse=True)
    return summaries[:limit]


def _sort_key(iso: str) -> float:
    parsed = parse_ts(iso)
    return parsed.timestamp() if parsed else 0.0

"""Read-only reader over the bridge's messages.db: the chat list now; chat view, media and state later."""

import json
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .config import Settings
from .mcp_lib import resolve_display_names
from .unread import NOT_POLL_VOTE, UnreadCounter

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
    cards = parse_contact_cards(content or "")
    if cards:
        extra = f" +{len(cards) - 1}" if len(cards) > 1 else ""
        return f"👤 {cards[0].name}{extra}"[:PREVIEW_MAX]
    return text[:PREVIEW_MAX]


# --- chat list ----------------------------------------------------------------------------------------------------


@dataclass
class _Conversation:
    """One person or group, possibly stored under several chat JIDs (LID and phone form)."""

    jid: str
    forms: list[str]
    rows: dict[str, str | None]  # chat jid -> chats.name, for rows that are listed (not merged away)


_MAX_MERGE_HOPS = 8


def _conversations(conn: sqlite3.Connection) -> list[_Conversation]:
    """Group chat rows into conversations from two table reads, with no per-chat queries.

    Same links as lib.changes.chat_forms: a chat's own JID, the chat it was merged into (merged_into chain, a
    loop falls back to the chat itself) and the identities row (jid, lid, phone_jid) of that target. Never digits.
    """
    chat_cols = _columns(conn, "chats")
    merge_col = "merged_into" if "merged_into" in chat_cols else "NULL"
    all_rows = conn.execute(f"SELECT jid, name, {merge_col} FROM chats").fetchall()
    merged_into = {jid: target for jid, _, target in all_rows if target}
    rows = {jid: name for jid, name, target in all_rows if jid and not target and not jid.endswith("@broadcast")}
    identity_forms: dict[str, tuple[str, ...]] = {}
    if _columns(conn, "identities"):
        for jid, lid, phone_jid in conn.execute("SELECT jid, lid, phone_jid FROM identities"):
            row_forms = tuple(f for f in (jid, lid, phone_jid) if f)
            for form in row_forms:
                identity_forms.setdefault(form, row_forms)  # first row wins, like LIMIT 1

    def canonical(jid: str) -> str:
        current, seen = jid, {jid}
        for _ in range(_MAX_MERGE_HOPS):
            nxt = merged_into.get(current)
            if not nxt:
                return current
            if nxt in seen:
                return jid
            seen.add(nxt)
            current = nxt
        return current

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for jid in rows:
        target = canonical(jid)
        for form in (jid, target, *identity_forms.get(target, ())):
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
    marked_changed_at: str | None = None
    history_unread: int | None = None


def _states(conn: sqlite3.Connection, conversations: list[_Conversation]) -> dict[str, _State]:
    if not _columns(conn, "chat_state"):
        return {}
    changed = "marked_unread_changed_at" if "marked_unread_changed_at" in _columns(conn, "chat_state") else "NULL"
    raw = {
        r[0]: r
        for r in conn.execute(
            f"SELECT chat_jid, pinned, archived, marked_unread, muted_until, last_read_at, history_unread_count,"
            f" {changed} FROM chat_state"
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
            if r[3] and (parse_ts(r[7]) or datetime.min.replace(tzinfo=UTC)) >= (
                parse_ts(state.marked_changed_at) or datetime.min.replace(tzinfo=UTC)
            ):
                state.marked_changed_at = r[7]
        out[conv.jid] = state
    return out


def _last_message(conn: sqlite3.Connection, forms: list[str], extra: str) -> tuple | None:
    """Newest message over all forms. The index order is the raw string; the true order (mixed UTC offsets)
    is decided on the few newest candidates."""
    rows = conn.execute(
        f"""
        SELECT id, chat_jid, sender, sender_name, content, timestamp, COALESCE(is_from_me, 0), media_type, filename,
               {extra}
        FROM messages WHERE chat_jid IN ({_marks(forms)}) AND {NOT_POLL_VOTE} ORDER BY timestamp DESC LIMIT 20
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
    return list_chats_with_count(conn, appconn, filter=filter, archived=archived, query=query, limit=limit)[0]


def list_chats_with_count(
    conn: sqlite3.Connection,
    appconn: sqlite3.Connection,
    *,
    filter: ChatFilter = "all",  # noqa: A002 - the API's parameter name
    archived: bool | None = False,
    query: str | None = None,
    limit: int = 200,
    only_jid: str | None = None,
) -> tuple[list[ChatSummary], int]:
    """The chat list and, from the same pass, how many chats are archived.

    `archived=None` lists both; `only_jid` (any JID form of the chat) narrows the pass to that one conversation.
    """
    page, archived_count, _ = list_chats_page(
        conn, appconn, filter=filter, archived=archived, query=query, limit=limit, only_jid=only_jid
    )
    return page, archived_count


def list_chats_page(
    conn: sqlite3.Connection,
    appconn: sqlite3.Connection,
    *,
    filter: ChatFilter = "all",  # noqa: A002 - the API's parameter name
    archived: bool | None = False,
    query: str | None = None,
    limit: int = 200,
    only_jid: str | None = None,
    before: str | None = None,
    keep_ties: bool = False,
) -> tuple[list[ChatSummary], int, bool]:
    """One page of the chat list, how many chats are archived, and whether older chats remain.

    The first page is pinned chats first, then newest first. With `before` (the last_message timestamp of the last
    chat the caller has) the next page is the unpinned chats strictly older than it: pinned chats are all on the first
    page. `keep_ties` keeps chats that share the boundary instant together, so a cursor never skips one.
    """
    convs = _conversations(conn)
    states = _states(conn, convs)
    archived_count = sum(1 for c in convs if states.get(c.jid, _State()).archived)
    convs = [c for c in convs if archived is None or states.get(c.jid, _State()).archived == archived]
    if only_jid is not None:
        convs = [c for c in convs if only_jid == c.jid or only_jid in c.forms]

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
                marked_unread=counter.dot(conv.forms + [conv.jid], state.marked_unread, state.marked_changed_at),
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
    if before is not None:
        parsed = parse_ts(before)
        if parsed is None:
            raise ValueError("bad timestamp")
        summaries = [
            s for s in summaries if not s.pinned and _sort_key(s.last_message.timestamp_iso) < parsed.timestamp()
        ]
    end = limit
    if keep_ties and len(summaries) > limit:
        edge = _sort_key(summaries[limit - 1].last_message.timestamp_iso)
        while end < len(summaries) and _sort_key(summaries[end].last_message.timestamp_iso) == edge:
            end += 1
    return summaries[:end], archived_count, len(summaries) > end


def _sort_key(iso: str) -> float:
    parsed = parse_ts(iso)
    return parsed.timestamp() if parsed else 0.0


def get_chat(conn: sqlite3.Connection, appconn: sqlite3.Connection, jid: str) -> ChatSummary | None:
    """One chat (archived or not) by any of its JID forms; None when it is unknown or has no messages."""
    found = list_chats_with_count(conn, appconn, archived=None, only_jid=jid, limit=1)[0]
    return found[0] if found else None


# --- chat view ----------------------------------------------------------------------------------------------------

PAGE_SIZE = 60
IMPORT_PREFIX = "import-"  # database.IsImportedMessageID: synthetic ids from chat exports


@dataclass
class MediaView:
    type: str
    filename: str | None
    duration_s: int | None
    has_file: bool
    size_bytes: int | None = None


@dataclass
class QuoteView:
    id: str
    sender_name: str | None
    preview: str


@dataclass
class ReactionView:
    emoji: str
    count: int
    mine: bool


@dataclass
class PollOption:
    text: str
    votes: int


@dataclass
class PollView:
    question: str
    options: list[PollOption]
    total: int


@dataclass
class ContactCard:
    name: str
    phones: list[str]


@dataclass
class MessageView:
    id: str
    chat_jid: str
    sender_jid: str | None
    sender_name: str | None
    from_me: bool
    timestamp_iso: str
    text: str | None
    media: MediaView | None
    reply_to: QuoteView | None
    forwarded: bool
    edited: bool
    deleted: bool
    system: bool
    reactions: list[ReactionView]
    poll: PollView | None
    contact_cards: list[ContactCard] | None
    # Rows that came from a chat export have no WhatsApp message key: no reply, reaction or read receipt.
    actionable: bool
    # No delivery receipts are stored, so there is nothing to tick.
    ticks: Literal["sent", "delivered", "read"] | None = None


_CONTACT_HEAD = re.compile(r"\[Contact: (.*)\]")
_PHONE_MARK = "📞 "


def parse_contact_cards(content: str) -> list[ContactCard] | None:
    """The bridge writes '[Contact: Name]' plus one '📞 number' line per phone; several contacts are separated by a
    blank line. Anything that is not exactly that stays ordinary text (None)."""
    cards: list[ContactCard] = []
    for block in content.strip().split("\n\n"):
        lines = block.split("\n")
        head = _CONTACT_HEAD.fullmatch(lines[0])
        if not head or not all(line.startswith(_PHONE_MARK) for line in lines[1:]):
            return None
        cards.append(ContactCard(head.group(1), [line[len(_PHONE_MARK) :].strip() for line in lines[1:]]))
    return cards or None


def _own_ids(conn: sqlite3.Connection, store_dir: Path | None, forms: list[str]) -> set[str]:
    """Bare phone and LID ids of our own account: whatsmeow's device row, plus senders of our own messages here."""
    own: set[str] = set()
    if store_dir is not None and (store_dir / "whatsapp.db").exists():
        try:
            wa = sqlite3.connect(f"{(store_dir / 'whatsapp.db').resolve().as_uri()}?mode=ro", uri=True)
            try:
                for jid, lid in wa.execute("SELECT jid, lid FROM whatsmeow_device"):
                    own.update(_bare(v) for v in (jid, lid) if v)
            finally:
                wa.close()
        except sqlite3.Error:
            pass
    for (sender,) in conn.execute(
        f"SELECT DISTINCT sender FROM messages WHERE is_from_me = 1 AND chat_jid IN ({_marks(forms)})", forms
    ):
        if sender:
            own.add(_bare(sender))
    return own


def _forms_of(conn: sqlite3.Connection, chat_jid: str) -> list[str]:
    for conv in _conversations(conn):
        if chat_jid == conv.jid or chat_jid in conv.forms:
            return conv.forms
    return [chat_jid]


def message_media_type(conn: sqlite3.Connection, chat_jid: str, message_id: str) -> str | None:
    """media_type of a message in any JID form of the chat, or None when there is no such message."""
    forms = _forms_of(conn, chat_jid)
    row = conn.execute(
        f"SELECT media_type FROM messages WHERE id=? AND chat_jid IN ({_marks(forms)})", (message_id, *forms)
    ).fetchone()
    return (row[0] or None) if row else None


def _has_file(store_dir: Path | None, forms: list[str], msg_id: str, filename: str | None) -> bool:
    """Auto-download keeps store/<jid>/<filename>; later downloads keep store/media/<jid>/<msgid><ext>."""
    if store_dir is None:
        return False
    bad = ("/", "\\")
    for jid in forms:
        if jid.startswith(".") or any(c in jid for c in bad):
            continue
        if (
            filename
            and not any(c in Path(filename).name for c in bad)
            and (store_dir / jid / Path(filename).name).is_file()
        ):
            return True
        if not any(c in msg_id for c in bad):
            folder = store_dir / "media" / jid
            if folder.is_dir() and any(folder.glob(f"{glob_escape(msg_id)}.*")):
                return True
    return False


def glob_escape(text: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", text)


def _sender_label(sender: str | None, stored: str | None, names: dict) -> str | None:
    if sender and sender in names:
        return names[sender].name
    if sender and sender.startswith("import:"):
        return sender[7:] or None
    return stored or None


def _message_select(cols: set[str], forms: list[str]) -> str:
    """The SELECT shared by the page query and the single-message query; callers append their own conditions."""

    def col(name: str, default: str = "NULL") -> str:
        return name if name in cols else default

    return f"""
        SELECT id, chat_jid, sender, sender_name, content, timestamp, COALESCE(is_from_me, 0), media_type, filename,
               {col("deleted_at")}, COALESCE({col("quoted_message_id")}, {col("reply_to_message_id")}),
               {col("quoted_sender_name")}, {col("quoted_text_preview")},
               COALESCE({col("is_edited", "0")}, 0) OR COALESCE({col("edit_count", "0")}, 0) > 0,
               COALESCE({col("is_forwarded", "0")}, 0), COALESCE({col("is_system_message", "0")}, 0),
               julianday(timestamp), {col("file_length")}
        FROM messages
        WHERE chat_jid IN ({_marks(forms)}) AND julianday(timestamp) IS NOT NULL
          AND {NOT_POLL_VOTE}"""


def get_message(conn: sqlite3.Connection, chat_jid: str, message_id: str) -> MessageView | None:
    """One message by (chat jid, id), built exactly like a page row; None when it is unknown or not shown."""
    forms = _forms_of(conn, chat_jid)
    rows = conn.execute(
        _message_select(_columns(conn, "messages"), forms) + " AND id = ? AND chat_jid = ?",
        [*forms, message_id, chat_jid],
    ).fetchall()
    if not rows:  # the caller may name any form of the chat: fall back to the id within the conversation
        rows = conn.execute(
            _message_select(_columns(conn, "messages"), forms) + " AND id = ? LIMIT 1", [*forms, message_id]
        ).fetchall()
    return _build(conn, forms, rows)[0] if rows else None


def conversation_index(conn: sqlite3.Connection) -> dict[str, str]:
    """Every JID form of every conversation -> the JID the chat list uses for it (its phone form when it has one)."""
    return {form: conv.jid for conv in _conversations(conn) for form in (*conv.forms, conv.jid)}


def list_messages_page(
    conn: sqlite3.Connection, chat_jid: str, *, before_ts: str | None = None, limit: int = PAGE_SIZE
) -> tuple[list[MessageView], bool]:
    """The newest `limit` messages older than `before_ts` over every JID form of the chat, oldest first, and
    whether anything older exists. Time order is by instant (SQLite julianday), so mixed UTC offsets sort right;
    rows sharing the oldest instant are kept together so a page boundary never splits them."""
    forms = _forms_of(conn, chat_jid)
    cols = _columns(conn, "messages")
    select = _message_select(cols, forms)
    select_base = select
    args: list[object] = list(forms)
    if before_ts is not None:
        parsed = parse_ts(before_ts)
        if parsed is None:
            raise ValueError("bad timestamp")
        select += " AND julianday(timestamp) < julianday(?)"
        args.append(parsed.isoformat(sep=" "))
    rows = conn.execute(select + " ORDER BY julianday(timestamp) DESC LIMIT ?", [*args, limit]).fetchall()
    has_more = False
    if rows:
        oldest = rows[-1][16]
        seen = {(r[0], r[1]) for r in rows}
        ties = conn.execute(select_base + " AND julianday(timestamp) = ?", [*forms, oldest]).fetchall()
        rows.extend(r for r in ties if (r[0], r[1]) not in seen)
        has_more = (
            conn.execute(select_base + " AND julianday(timestamp) < ? LIMIT 1", [*forms, oldest]).fetchone() is not None
        )
    rows.sort(key=lambda r: (r[16], r[0]))
    return _build(conn, forms, rows), has_more


def list_messages(
    conn: sqlite3.Connection, chat_jid: str, *, before_ts: str | None = None, limit: int = PAGE_SIZE
) -> list[MessageView]:
    return list_messages_page(conn, chat_jid, before_ts=before_ts, limit=limit)[0]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(_columns(conn, table))


def _quotes(
    conn: sqlite3.Connection, forms: list[str], rows: list[tuple], store_dir: Path | None
) -> dict[str, QuoteView]:
    """Reply quotes. The quoted message is looked up in the store so that a quoted message deleted later shows
    'Message deleted' and never the preview text that was stored when the reply arrived."""
    wanted = {r[10]: r for r in rows if r[10]}
    if not wanted:
        return {}
    found: dict[str, tuple] = {}
    ids = list(wanted)
    for chunk in _chunks(ids):
        for q in conn.execute(
            f"SELECT id, sender, sender_name, content, media_type, filename, COALESCE(is_from_me, 0), chat_jid,"
            f" {'deleted_at' if 'deleted_at' in _columns(conn, 'messages') else 'NULL'}"
            f" FROM messages WHERE id IN ({_marks(chunk)}) AND chat_jid IN ({_marks(forms)})",
            [*chunk, *forms],
        ):
            found[q[0]] = q
    senders = {q[1] for q in found.values() if q[1] and not q[6]}
    names = resolve_display_names(conn, senders) if senders else {}
    out: dict[str, QuoteView] = {}
    for qid, row in wanted.items():
        q = found.get(qid)
        if q is None:
            stored_name, stored_text = row[11], row[12]
            preview = " ".join((stored_text or "").split())[:PREVIEW_MAX] or "Original message unavailable"
            out[row[0]] = QuoteView(qid, stored_name or None, preview)
            continue
        voice = _voice_seconds(store_dir, forms, q[7], q[5]) if q[4] in ("audio", "ptt") else None
        preview = _preview(q[3], q[4], q[5], bool(q[8]), voice)
        who = "You" if q[6] else (row[11] or _sender_label(q[1], q[2], names))
        out[row[0]] = QuoteView(qid, who, preview)
    return out


def _polls(conn: sqlite3.Connection, forms: list[str], poll_ids: list[str]) -> dict[str, PollView]:
    """Poll options and live tally straight from the store (poll_options, poll_current_votes): no bridge call."""
    if not poll_ids or not _table_exists(conn, "poll_options"):
        return {}
    options: dict[str, list[str]] = {}
    for row in conn.execute(
        f"SELECT message_id, option_name FROM poll_options WHERE message_id IN ({_marks(poll_ids)})"
        f" AND chat_jid IN ({_marks(forms)}) ORDER BY option_index",
        [*poll_ids, *forms],
    ):
        options.setdefault(row[0], []).append(row[1])
    tallies: dict[str, dict[str, int]] = {}
    voters: dict[str, int] = {}
    if _table_exists(conn, "poll_current_votes"):
        for pid, encoded in conn.execute(
            f"SELECT poll_message_id, selected_options FROM poll_current_votes"
            f" WHERE poll_message_id IN ({_marks(poll_ids)}) AND chat_jid IN ({_marks(forms)})",
            [*poll_ids, *forms],
        ):
            try:
                picked = json.loads(encoded)
            except ValueError:
                continue
            if not isinstance(picked, list) or not picked:
                continue  # a retracted vote
            voters[pid] = voters.get(pid, 0) + 1
            for name in picked:
                tallies.setdefault(pid, {})[str(name)] = tallies.get(pid, {}).get(str(name), 0) + 1
    return {
        pid: PollView(
            "", [PollOption(o, tallies.get(pid, {}).get(o, 0)) for o in options.get(pid, [])], voters.get(pid, 0)
        )
        for pid in poll_ids
    }


def _reactions(
    conn: sqlite3.Connection, forms: list[str], ids: list[str], own: set[str]
) -> dict[str, list[ReactionView]]:
    """Per message: one entry per emoji with its count. An empty emoji is a removal tombstone and is left out."""
    if not ids or not _table_exists(conn, "message_reactions"):
        return {}
    counts: dict[str, dict[str, list]] = {}
    for chunk in _chunks(ids):
        for mid, sender, emoji in conn.execute(
            f"SELECT message_id, sender, emoji FROM message_reactions WHERE message_id IN ({_marks(chunk)})"
            f" AND chat_jid IN ({_marks(forms)}) AND emoji <> '' ORDER BY reacted_at, sender",
            [*chunk, *forms],
        ):
            entry = counts.setdefault(mid, {}).setdefault(emoji, [0, False])
            entry[0] += 1
            entry[1] = entry[1] or _bare(sender) in own
    return {
        mid: [ReactionView(e, c, m) for e, (c, m) in sorted(per.items(), key=lambda kv: -kv[1][0])]
        for mid, per in counts.items()
    }


def _build(conn: sqlite3.Connection, forms: list[str], rows: list[tuple]) -> list[MessageView]:
    store_dir = _db_dir(conn)
    own = _own_ids(conn, store_dir, forms)
    senders = {r[2] for r in rows if r[2] and not r[6]}
    names = resolve_display_names(conn, senders) if senders else {}
    quotes = _quotes(conn, forms, rows, store_dir)
    live = [r for r in rows if not r[9]]
    polls = _polls(
        conn,
        forms,
        [r[0] for r in live if not r[7] and (r[4] or "").startswith("[Poll: ") and (r[4] or "").endswith("]")],
    )
    reactions = _reactions(conn, forms, [r[0] for r in live], own)
    out: list[MessageView] = []
    for r in rows:
        (mid, chat, sender, sender_name, content, stamp, from_me, media_type, filename, deleted_at) = r[:10]
        deleted = bool(deleted_at)
        stamped = parse_ts(stamp)
        text: str | None = content or None
        media = poll = cards = None
        if deleted:
            text = None  # the stored text of a deleted message is never sent to the client
        elif media_type:
            voice = _voice_seconds(store_dir, forms, chat, filename) if media_type in ("audio", "ptt") else None
            media = MediaView(
                media_type, filename or None, voice, _has_file(store_dir, forms, mid, filename), r[17] or None
            )
        elif content and mid in polls:
            poll = polls[mid]
            poll.question = content[7:-1]
            text = None
        elif content and (parsed := parse_contact_cards(content)):
            cards, text = parsed, None
        out.append(
            MessageView(
                id=mid,
                chat_jid=chat,
                sender_jid=sender or None,
                sender_name=None if from_me else _sender_label(sender, sender_name, names),
                from_me=bool(from_me),
                timestamp_iso=stamped.isoformat() if stamped else str(stamp),
                text=text,
                media=media,
                reply_to=None if deleted else quotes.get(mid),
                forwarded=bool(r[14]) and not deleted,
                edited=bool(r[13]) and not deleted,
                deleted=deleted,
                system=bool(r[15]),
                reactions=[] if deleted else reactions.get(mid, []),
                poll=poll,
                contact_cards=cards,
                actionable=not mid.startswith(IMPORT_PREFIX),
            )
        )
    return out


def members_line(conn: sqlite3.Connection, chat_jid: str) -> str | None:
    """'Ana, Nikola, you' for a group, from who has written here lately (the store keeps no member list)."""
    if not chat_jid.endswith("@g.us"):
        return None
    forms = _forms_of(conn, chat_jid)
    rows = conn.execute(
        f"SELECT sender, sender_name FROM messages WHERE chat_jid IN ({_marks(forms)}) AND COALESCE(is_from_me, 0) = 0"
        " AND sender IS NOT NULL AND COALESCE(is_system_message, 0) = 0 ORDER BY julianday(timestamp) DESC LIMIT 300",
        forms,
    ).fetchall()
    senders = {s for s, _ in rows}
    names = resolve_display_names(conn, senders) if senders else {}
    seen: dict[str, None] = {}
    for sender, stored in rows:
        label = _sender_label(sender, stored, names)
        if label and not _is_number_like(label):
            seen.setdefault(label)
    ordered = sorted(seen, key=fold)[:8]
    mine = conn.execute(
        f"SELECT 1 FROM messages WHERE chat_jid IN ({_marks(forms)}) AND is_from_me = 1 LIMIT 1", forms
    ).fetchone()
    return ", ".join([*ordered, "you"]) if mine or ordered else None

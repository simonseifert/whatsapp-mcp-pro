"""Import WhatsApp's own "Export chat" files into messages.db.

Linked-device history sync only delivers what the phone chooses to send, so
older chats stop at some date. The phone's "Export chat" goes further back, but
carries display names instead of JIDs, minute (Android) or second (iOS)
timestamps in the phone's local time, and no message IDs. This module turns
such an export into rows the rest of the stack already reads: list_messages,
recall (its indexer embeds any row with content), download_media and
transcription.

Run it as a CLI, never as an agent tool; the default is a dry run:

    WA_STORE_PATH=<data>/store uv run python -m lib.chat_import EXPORT.zip \\
        --chat 4915112345678@s.whatsapp.net [--me "Alice"] [--apply]

Imported rows are told apart by their ID: ``import-<hash>``. Real WhatsApp
message IDs are uppercase hex (``3EB0…``, ``AC…``), so the prefix cannot clash,
and an ID needs no schema change, which keeps the importer usable on any store
the bridge has ever written. The bridge skips these rows where a real ID is
required (history-request anchors) and serves their media from the local copy.

Safe while the bridge runs: the bridge opens messages.db in WAL mode with a
5 s busy timeout, and the write here is one short ``BEGIN IMMEDIATE``
transaction of plain INSERTs (milliseconds per thousand rows), taken after the
slow work (parsing, matching, media copies, backup) is done.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import sys
import unicodedata
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .naming import sender_forms
from .utils import MESSAGES_DB_PATH, canonical_chat_jid, logger

IMPORT_ID_PREFIX = "import-"
IMPORT_SENDER_PREFIX = "import:"
DEFAULT_TZ = "Europe/Zagreb"

# Larger attachments are skipped rather than streamed into the store; the
# bridge refuses to download media above the same order of magnitude.
MAX_MEDIA_BYTES = 512 * 1024 * 1024

# Exact matching tolerates clock noise between the phone's displayed time and
# the server timestamp the bridge stores; the text has to agree as well, so a
# wide window costs nothing. Fuzzy matching (sender + time only) must be tight.
EXACT_SLACK_S = 120
FUZZY_SLACK_S = 1

# Invisible marks the exporters sprinkle in: iOS prefixes every system line,
# attachment and omitted marker with U+200E, Android sometimes starts the file
# with a BOM, and names can carry bidi isolates.
_INVISIBLE = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff\u200b"))

_HEADER = re.compile(
    r"""^[\u200e\u200f\ufeff]*
    (?P<open>\[)?
    (?P<date>\d{1,4}[./-]\s?\d{1,2}[./-]\s?\d{1,4})\.?
    ,?\s+
    (?P<time>\d{1,2}[:.]\d{2}(?:[:.]\d{2})?)
    (?:\s*(?P<ampm>[AaPp]\.?\s?[Mm]\.?))?
    (?(open)\]\s|\s[-\u2013]\s)
    (?P<rest>.*)$""",
    re.X,
)
_DATE_SPLIT = re.compile(r"[./-]\s?")

# Deleted-message placeholders, compared after normalisation (see _norm).
_DELETED = {
    "this message was deleted",
    "you deleted this message",
    "diese nachricht wurde gelöscht",
    "du hast diese nachricht gelöscht",
    "ova poruka je izbrisana",
    "izbrisali ste ovu poruku",
    "obrisali ste ovu poruku",
    "ova poruka je obrisana",
}
_EDITED = re.compile(r"\s*<[^<>]*(?:edited|bearbeitet|uređen|uredjen)[^<>]*>\s*$", re.I)
# Android: "<Media omitted>", "<Medien ausgeschlossen>", "<Medijski zapis izostavljen>".
_ANDROID_OMITTED = re.compile(r"^<[^<>:]*(?:omitted|ausgeschlossen|weggelassen|izostavljen\w*)>$", re.I)
# iOS (always behind U+200E): "image omitted", "Bild weggelassen", "slika izostavljena".
_IOS_OMITTED = re.compile(r"(?:omitted|weggelassen|ausgeschlossen|izostavljen\w*)$", re.I)
# iOS: "<attached: 00000012-PHOTO-2026-10-23-09-41-12.jpg>", localised label.
_IOS_ATTACHED = re.compile(r"^<[^<>:\n]{2,30}:\s*(?P<file>[^<>\n]+\.[A-Za-z0-9]{1,6})>\s*(?:\n(?P<caption>.*))?$", re.S)
# Android: "IMG-20261023-WA0001.jpg (file attached)", localised parenthetical.
_ANDROID_ATTACHED = re.compile(
    r"^(?P<file>[^\n()]+?\.[A-Za-z0-9]{1,6}) \((?P<note>[^()\n]{3,40})\)(?:\n(?P<caption>.*))?$", re.S
)
_ATTACH_WORDS = re.compile(r"attach|angehängt|datei|datoteka|priložen|prilog|fichier|archivo|allegato", re.I)
_PHONE_NAME = re.compile(r"^\+?[\d\s\-().]{7,}$")
_WELL_FORMED_JID = re.compile(r"^(?:\d{5,20}@s\.whatsapp\.net|\d{5,20}@lid|\d{5,20}(?:-\d{5,20})?@g\.us)$")


class ChatImportError(RuntimeError):
    """An export or store the importer refuses to guess about."""


# --------------------------------------------------------------------------- export reading


@dataclass
class Export:
    """A chat transcript plus a way to read the attachments that came with it."""

    text: str
    title: str
    media_names: set[str]
    open_media: Callable[[str], Iterator[bytes]]
    media_size: Callable[[str], int]


def _decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ChatImportError(f"export is not UTF-8 text: {exc}") from exc


def _title_from(name: str) -> str:
    stem = Path(name).stem
    m = re.match(r"^WhatsApp Chat (?:with |- |mit |s )?(.+)$", stem, re.I)
    return (m.group(1) if m else stem).strip()


def read_export(path: str | os.PathLike[str]) -> Export:
    """Open a ``.zip`` export or a bare ``.txt`` (attachments looked up beside it).

    Zip members are read by basename and never extracted, so a crafted member
    path cannot write anywhere.

    Raises:
        ChatImportError: no transcript found, or more than one candidate.
    """
    p = Path(path)
    if not p.is_file():
        raise ChatImportError(f"no such file: {p}")
    if zipfile.is_zipfile(p):
        zf = zipfile.ZipFile(p)
        members = {Path(i.filename).name: i for i in zf.infolist() if not i.is_dir() and Path(i.filename).name}
        if "_chat.txt" in members:
            chat_name = "_chat.txt"
        else:
            txts = [n for n in members if n.lower().endswith(".txt")]
            chats = [n for n in txts if n.lower().startswith("whatsapp chat")] or txts
            if len(chats) != 1:
                raise ChatImportError(
                    f"expected one chat transcript in the zip, found {len(chats)}: {sorted(chats)[:5]}"
                )
            chat_name = chats[0]
        text = _decode(zf.read(members[chat_name]))
        media = {n for n in members if n != chat_name}

        def open_zip(name: str) -> Iterator[bytes]:
            with zf.open(members[name]) as fh:
                while chunk := fh.read(1 << 20):
                    yield chunk

        title = _title_from(p.name if chat_name == "_chat.txt" else chat_name)
        return Export(text, title, media, open_zip, lambda n: members[n].file_size)

    text = _decode(p.read_bytes())
    folder = p.parent
    media = {c.name for c in folder.iterdir() if c.is_file() and c != p}

    def open_file(name: str) -> Iterator[bytes]:
        with open(folder / name, "rb") as fh:
            while chunk := fh.read(1 << 20):
                yield chunk

    return Export(text, _title_from(p.name), media, open_file, lambda n: (folder / n).stat().st_size)


# --------------------------------------------------------------------------- parsing


@dataclass
class ExportFormat:
    """What detect_format decided about an export's header lines."""

    style: str  # "ios" (bracketed, seconds) or "android" ("date, time - ")
    date_order: str  # "DMY", "MDY" or "YMD"
    has_seconds: bool

    @property
    def precision_s(self) -> int:
        """Width of the time bucket one exported timestamp stands for."""
        return 1 if self.has_seconds else 60


@dataclass
class ParsedMessage:
    """One message from the transcript, before any store lookups."""

    line_no: int
    local: datetime  # naive, phone-local
    sender_name: str
    text: str
    kind: str = "text"  # text | media | omitted | deleted | system
    attachment: str | None = None
    is_edited: bool = False


def _date_parts(date: str) -> list[str]:
    return [p for p in _DATE_SPLIT.split(date.strip().rstrip(".")) if p]


def detect_format(lines: list[str], date_order: str | None = None) -> ExportFormat:
    """Decide the header style and date order from every header line in the file.

    A single file never mixes styles, but one "01/02" line cannot tell day from
    month, so the order is inferred from the whole file: any first field above
    12 means DMY, any second field above 12 means MDY.

    Raises:
        ChatImportError: no header lines, conflicting evidence, or every date ambiguous
        and no ``date_order`` override.
    """
    matches = [m for line in lines if (m := _HEADER.match(line))]
    if not matches:
        raise ChatImportError("no WhatsApp message lines recognised; is this a chat export?")
    ios = sum(1 for m in matches if m.group("open"))
    style = "ios" if ios * 2 > len(matches) else "android"
    matches = [m for m in matches if bool(m.group("open")) == (style == "ios")]
    has_seconds = (
        Counter(m.group("time").count(":") + m.group("time").count(".") for m in matches).most_common(1)[0][0] == 2
    )

    if date_order:
        order = date_order.upper()
        if order not in {"DMY", "MDY", "YMD"}:
            raise ChatImportError(f"--date-order must be DMY, MDY or YMD, not {date_order!r}")
        return ExportFormat(style, order, has_seconds)

    parts = [_date_parts(m.group("date")) for m in matches]
    if all(len(p[0]) == 4 for p in parts):
        return ExportFormat(style, "YMD", has_seconds)
    first_big = any(int(p[0]) > 12 for p in parts)
    second_big = any(int(p[1]) > 12 for p in parts)
    if first_big and second_big:
        raise ChatImportError("dates disagree on day/month order; pass --date-order")
    if first_big:
        return ExportFormat(style, "DMY", has_seconds)
    if second_big:
        return ExportFormat(style, "MDY", has_seconds)
    # Dots are never used for month-first dates, so "01.02.26" is still decidable.
    if any("." in m.group("date") for m in matches):
        return ExportFormat(style, "DMY", has_seconds)
    raise ChatImportError(
        "every date in this export reads both ways (day and month <= 12); pass --date-order DMY or MDY"
    )


def _parse_local(m: re.Match[str], fmt: ExportFormat) -> datetime:
    p = [int(x) for x in _date_parts(m.group("date"))]
    if fmt.date_order == "YMD":
        y, mo, d = p
    elif fmt.date_order == "MDY":
        mo, d, y = p
    else:
        d, mo, y = p
    if y < 100:
        y += 2000
    t = [int(x) for x in re.split(r"[:.]", m.group("time"))]
    hour, minute, second = t[0], t[1], (t[2] if len(t) > 2 else 0)
    if ampm := m.group("ampm"):
        hour = hour % 12 + (12 if ampm.lower().startswith("p") else 0)
    return datetime(y, mo, d, hour, minute, second)


def _norm(text: str) -> str:
    """Compare form: invisible marks gone, whitespace collapsed, case folded."""
    s = unicodedata.normalize("NFC", text).translate(_INVISIBLE)
    s = re.sub(r"\s+", " ", s).strip().rstrip(".").strip()
    return s.casefold()


def clean_name(name: str) -> str:
    """Display name as shown, minus iOS's "~ " marker for non-contacts and invisible marks."""
    s = unicodedata.normalize("NFC", name).translate(_INVISIBLE)
    s = re.sub(r"\s+", " ", s).strip()
    return s.lstrip("~").strip()


def _classify(msg: ParsedMessage, fmt: ExportFormat, media_names: set[str]) -> None:
    raw = msg.text
    lrm = raw.startswith("\u200e")
    body = raw.translate(_INVISIBLE).strip()
    if _EDITED.search(body):
        body = _EDITED.sub("", body).strip()
        msg.is_edited = True

    if _norm(body) in _DELETED:
        msg.kind = "deleted"
        msg.text = ""
        return
    first, _, caption = body.partition("\n")
    if fmt.style == "android" and (_ANDROID_OMITTED.match(first) or first == "null"):
        msg.kind, msg.text = "omitted", caption.strip()
        return
    if fmt.style == "ios" and lrm and "\n" not in body and len(body) <= 120 and _IOS_OMITTED.search(body):
        msg.kind, msg.text = "omitted", ""
        return
    att = _IOS_ATTACHED.match(body) or _ANDROID_ATTACHED.match(body)
    if att:
        name = att.group("file").strip()
        note = att.groupdict().get("note")
        if note is None or name in media_names or _ATTACH_WORDS.search(note):
            msg.kind = "media"
            msg.attachment = name
            msg.text = (att.group("caption") or "").strip()
            return
    if fmt.style == "ios" and lrm:
        # iOS writes group events and call notices as "Name: \u200e<event>": the
        # leading mark is what tells them apart from something a person typed.
        msg.kind, msg.text = "system", body
        return
    msg.text = body


def parse_export(
    text: str, date_order: str | None = None, media_names: Iterable[str] = ()
) -> tuple[ExportFormat, list[ParsedMessage]]:
    """Parse a transcript into messages, system lines included (``kind == "system"``).

    Raises:
        ChatImportError: see detect_format.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    fmt = detect_format(lines, date_order)
    names = set(media_names)
    out: list[ParsedMessage] = []
    for i, line in enumerate(lines, 1):
        m = _HEADER.match(line)
        if m and bool(m.group("open")) != (fmt.style == "ios"):
            m = None  # a pasted line in the other app's format is message text
        if m:
            try:
                local = _parse_local(m, fmt)
            except ValueError:
                m = None  # "31/02" or similar: a continuation line that only looks like a header
        if not m:
            if out:
                out[-1].text += "\n" + line
            continue
        rest = m.group("rest")
        name, sep, body = rest.partition(": ")
        if not sep or not name.strip():
            # Android writes group events without a "Name: " part.
            out.append(ParsedMessage(i, local, "", rest, kind="system"))
            continue
        out.append(ParsedMessage(i, local, clean_name(name), body))
    for msg in out:
        if msg.kind == "text":
            msg.text = msg.text.rstrip("\n")
            _classify(msg, fmt, names)
    return fmt, out


# --------------------------------------------------------------------------- time


@dataclass
class Resolved:
    """A parsed message pinned to an instant, plus how sure that is."""

    msg: ParsedMessage
    utc: datetime
    stored: str  # "YYYY-MM-DD HH:MM:SS+HH:MM", the bridge's format
    dst_ambiguous: bool = False


def _stored_form(dt: datetime) -> str:
    off = dt.utcoffset() or timedelta(0)
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    hh, mm = divmod(abs(total) // 60, 60)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f"{sign}{hh:02d}:{mm:02d}"


def resolve_times(msgs: list[ParsedMessage], tz: ZoneInfo) -> list[Resolved]:
    """Attach a UTC instant to each message.

    In the autumn DST hour every local time exists twice. The transcript is in
    order, so the first reading (fold=0) is taken until it would put a message
    before the one above it, which is when the clock went back.
    """
    out: list[Resolved] = []
    prev: datetime | None = None
    for msg in msgs:
        a = msg.local.replace(tzinfo=tz, fold=0)
        b = msg.local.replace(tzinfo=tz, fold=1)
        ambiguous = a.utcoffset() != b.utcoffset() and all(
            x.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == msg.local for x in (a, b)
        )
        chosen = a
        if ambiguous and prev is not None and a.astimezone(UTC) < prev:
            chosen = b
        utc = chosen.astimezone(UTC)
        # Re-derive the local form from the instant so a time that falls into
        # the spring-forward gap is stored as the wall time it really was.
        out.append(Resolved(msg, utc, _stored_form(utc.astimezone(tz)), ambiguous))
        prev = utc if prev is None else max(prev, utc)
    return out


# --------------------------------------------------------------------------- media


_MEDIA_EXT = {"image": ".jpg", "video": ".mp4", "audio": ".ogg", "document": ".bin"}
_IMAGE = {"jpg", "jpeg", "png", "heic", "webp", "gif"}
_VIDEO = {"mp4", "mov", "3gp", "mkv", "webm", "m4v"}
_AUDIO = {"opus", "ogg", "m4a", "mp3", "aac", "amr", "wav"}


def media_type_for(filename: str) -> str:
    """Bridge media_type for an exported attachment, from its name.

    WhatsApp's own names say what the file was (iOS ``-PHOTO-``/``-AUDIO-``,
    Android ``IMG-``/``PTT-``); the extension decides the rest. iOS exports
    GIFs as mp4, which the name prefix catches.
    """
    upper = filename.upper()
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if re.search(r"-(PHOTO|STICKER)-", upper) or re.match(r"^(IMG|STK)-", upper):
        return "image"
    if re.search(r"-(VIDEO|GIF)-", upper) or upper.startswith("VID-"):
        return "video"
    if "-AUDIO-" in upper or re.match(r"^(PTT|AUD)-", upper):
        return "audio"
    if ext in _IMAGE:
        return "image"
    if ext in _VIDEO:
        return "video"
    if ext in _AUDIO:
        return "audio"
    return "document"


def sanitize_path(s: str) -> str:
    """Mirror of mediastore.SanitizePath in the bridge; keep them identical."""
    if not s:
        return "_"
    # Go works on bytes, so a multi-byte rune becomes one '_' per byte there.
    out = "".join(c if (c.isascii() and (c.isalnum() or c in "_-@")) else "_" * len(c.encode()) for c in s)
    if out[0] in ".-":
        out = "_" + out[1:]
    return out


def media_extension(media_type: str, filename: str) -> str:
    """Mirror of mediastore.Extension: documents keep their own extension."""
    ext = _MEDIA_EXT.get(media_type, ".bin")
    if media_type == "document" and filename:
        own = Path(filename).suffix.lstrip(".")
        if own and len(own) <= 10:
            ext = "." + sanitize_path(own)
    return ext


def media_path(store_dir: str, chat_jid: str, message_id: str, media_type: str, filename: str) -> str:
    """Where /api/download looks for a saved copy (mediastore.Path), absolute."""
    return os.path.join(
        os.path.abspath(store_dir),
        "media",
        sanitize_path(chat_jid),
        sanitize_path(message_id) + media_extension(media_type, filename),
    )


# --------------------------------------------------------------------------- store side


@dataclass
class Existing:
    """A row already in the store, reduced to what matching needs."""

    id: str
    utc: datetime
    sender: str
    is_from_me: bool
    norm: str
    media_type: str | None


@dataclass
class NewRow:
    """A message the store does not have yet, in the bridge's column shapes."""

    id: str
    chat_jid: str
    sender: str
    sender_name: str
    content: str
    timestamp: str
    is_from_me: bool
    utc: datetime
    is_edited: bool = False
    media_type: str | None = None
    filename: str | None = None
    media_src: str | None = None  # attachment name inside the export
    media_dest: str | None = None  # where apply_plan copies it (mediastore layout)


@dataclass
class Plan:
    """Everything the importer decided, before anything is written."""

    chat_jid: str
    chat_exists: bool
    chat_title: str
    fmt: ExportFormat
    tz: str
    me_name: str
    me_source: str
    my_sender: str
    mapping: dict[str, tuple[str, str]] = field(default_factory=dict)  # name -> (sender, how)
    unmapped: list[str] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)
    new_rows: list[NewRow] = field(default_factory=list)
    first_stored_utc: datetime | None = None
    max_rowid: int = 0
    chat_set: list[str] = field(default_factory=list)


def _parse_stored(ts: str, tz: ZoneInfo) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _identity_forms(conn: sqlite3.Connection, jid: str) -> set[str]:
    out = {jid}
    if _table_exists(conn, "identities"):
        for row in conn.execute(
            "SELECT jid, lid, phone_jid FROM identities WHERE jid = ? OR lid = ? OR phone_jid = ?", (jid, jid, jid)
        ):
            out.update(x for x in row if x)
    return out


def _chat_set(conn: sqlite3.Connection, chat: str, target: str) -> list[str]:
    """Every chat_jid this conversation's existing rows may sit under."""
    found = {chat, target}
    if not target.endswith("@g.us"):
        found |= _identity_forms(conn, target)
    try:
        found |= {r[0] for r in conn.execute("SELECT jid FROM chats WHERE merged_into = ?", (target,))}
    except sqlite3.OperationalError:
        pass
    return sorted(found)


def _canonical_identity(conn: sqlite3.Connection, handle: str) -> str:
    if _table_exists(conn, "identities"):
        row = conn.execute(
            "SELECT jid FROM identities WHERE jid = ? OR lid = ? OR phone_jid = ? LIMIT 1", (handle, handle, handle)
        ).fetchone()
        if row and row[0]:
            return str(row[0])
    return handle


def _name_index(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Normalised name -> canonical JIDs of every person known by that name."""
    index: dict[str, set[str]] = defaultdict(set)
    if _table_exists(conn, "identities"):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(identities)")}
        name_cols = [
            c
            for c in ("display_name", "full_name", "first_name", "push_name", "business_name", "nickname", "chat_name")
            if c in cols
        ]
        if name_cols:
            for row in conn.execute(
                f"SELECT jid, {', '.join(name_cols)} FROM identities WHERE COALESCE(kind, 'user') = 'user'"
            ):
                for name in row[1:]:
                    if name and str(name).strip():
                        index[_norm(clean_name(str(name)))].add(row[0])
    if _table_exists(conn, "contact_nicknames"):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(contact_nicknames)")}
        # An agent's guess is not evidence of who wrote a message.
        where = "WHERE COALESCE(source, 'user') = 'user'" if "source" in cols else ""
        for jid, nick in conn.execute(f"SELECT jid, nickname FROM contact_nicknames {where}"):
            if nick and str(nick).strip():
                index[_norm(clean_name(str(nick)))].add(_canonical_identity(conn, jid))
    return index


def _load_existing(conn: sqlite3.Connection, chats: list[str], tz: ZoneInfo) -> list[Existing]:
    ph = ",".join("?" * len(chats))
    rows: list[Existing] = []
    for mid, ts, sender, from_me, content, mtype in conn.execute(
        f"SELECT id, timestamp, COALESCE(sender, ''), is_from_me, COALESCE(content, ''), media_type "
        f"FROM messages WHERE chat_jid IN ({ph})",
        chats,
    ):
        utc = _parse_stored(ts, tz)
        if utc is not None:
            rows.append(Existing(mid, utc, sender, bool(from_me), _norm(content), mtype or None))
    rows.sort(key=lambda r: r.utc)
    return rows


def _own_sender(conn: sqlite3.Connection, chats: list[str]) -> str:
    """The sender value the bridge writes on this account's own messages."""
    ph = ",".join("?" * len(chats))
    row = (
        conn.execute(
            f"SELECT sender FROM messages WHERE is_from_me = 1 AND COALESCE(sender, '') <> '' "
            f"AND chat_jid IN ({ph}) AND id NOT LIKE '{IMPORT_ID_PREFIX}%' GROUP BY sender ORDER BY COUNT(*) DESC LIMIT 1",
            chats,
        ).fetchone()
        or conn.execute(
            f"SELECT sender FROM messages WHERE is_from_me = 1 AND COALESCE(sender, '') <> '' "
            f"AND id NOT LIKE '{IMPORT_ID_PREFIX}%' GROUP BY sender ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
    )
    return str(row[0]) if row else ""


class _Matcher:
    """One-to-one matching of export messages to existing rows by time window."""

    def __init__(self, existing: list[Existing]) -> None:
        self.rows = existing
        self.keys = [r.utc.timestamp() for r in existing]
        self.claimed: set[int] = set()

    def window(self, start: float, end: float) -> Iterator[int]:
        import bisect

        i = bisect.bisect_left(self.keys, start)
        while i < len(self.keys) and self.keys[i] <= end:
            if i not in self.claimed:
                yield i
            i += 1


def _content_key(r: Resolved) -> str:
    return _norm(r.msg.text) if r.msg.kind != "media" else f"{r.msg.attachment}\n{_norm(r.msg.text)}"


def _same_content(r: Resolved, row: Existing) -> bool:
    if r.msg.kind == "media":
        # The bridge stores a caption, or a voice-note transcript, as content;
        # an attachment matches any media row whose caption agrees or is absent.
        if row.media_type is None:
            return False
        cap = _norm(r.msg.text)
        return not cap or not row.norm or cap == row.norm
    return row.media_type is None and row.norm == _norm(r.msg.text) and bool(row.norm)


def _import_id(chat: str, r: Resolved, ordinal: int) -> str:
    # Keyed on the export's own name rather than the JID it maps to, so a
    # re-run with a better mapping still produces the same IDs.
    key = "\x1f".join(
        [chat, r.utc.strftime("%Y-%m-%dT%H:%M:%S"), _norm(r.msg.sender_name), _content_key(r), str(ordinal)]
    )
    return IMPORT_ID_PREFIX + hashlib.sha256(key.encode()).hexdigest()[:24]


def _read_whatsapp_push_name(store_dir: str) -> str:
    path = os.path.join(store_dir, "whatsapp.db")
    if not os.path.exists(path):
        return ""
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as conn:
            row = conn.execute("SELECT push_name FROM whatsmeow_device LIMIT 1").fetchone()
            return str(row[0]) if row and row[0] else ""
    except sqlite3.Error:
        return ""


def build_plan(
    conn: sqlite3.Connection,
    export: Export,
    chat_jid: str,
    *,
    me: str | None = None,
    tz_name: str = DEFAULT_TZ,
    date_order: str | None = None,
    store_dir: str | None = None,
) -> Plan:
    """Parse, map and dedupe an export against the store. Reads only.

    Raises:
        ChatImportError: unusable chat JID, unknown time zone, unparseable export,
        or no way to tell which name is the account owner.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # ZoneInfoNotFoundError, ValueError on odd keys
        raise ChatImportError(f"unknown time zone {tz_name!r}") from exc
    chat_jid = chat_jid.strip()
    exists = conn.execute("SELECT 1 FROM chats WHERE jid = ?", (chat_jid,)).fetchone() is not None
    if not exists and not _WELL_FORMED_JID.match(chat_jid):
        raise ChatImportError(f"{chat_jid!r} is neither a known chat nor a well-formed chat JID")
    target = canonical_chat_jid(conn, chat_jid)
    is_group = target.endswith("@g.us")
    chats = _chat_set(conn, chat_jid, target)

    fmt, parsed = parse_export(export.text, date_order, export.media_names)
    resolved = resolve_times(parsed, tz)
    existing = _load_existing(conn, chats, tz)
    max_rowid = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()[0]

    counts: Counter[str] = Counter()
    counts["lines_parsed"] = len(parsed)
    counts["dst_ambiguous"] = sum(1 for r in resolved if r.dst_ambiguous)
    kept: list[Resolved] = []
    for r in resolved:
        kind = r.msg.kind
        if kind == "system":
            counts["skipped_system"] += 1
        elif kind == "deleted":
            counts["skipped_deleted"] += 1
        elif kind == "omitted" and not r.msg.text:
            counts["skipped_media_omitted"] += 1
        elif kind == "text" and not r.msg.text.strip():
            counts["skipped_empty"] += 1
        else:
            kept.append(r)
    counts["messages"] = len(kept)

    names = sorted({r.msg.sender_name for r in kept})
    pre = _Matcher(existing)
    precision = fmt.precision_s

    def exact_hits(r: Resolved) -> list[Existing]:
        t = r.utc.timestamp()
        return [
            pre.rows[i]
            for i in pre.window(t - EXACT_SLACK_S, t + precision + EXACT_SLACK_S)
            if _same_content(r, pre.rows[i]) and not pre.rows[i].id.startswith(IMPORT_ID_PREFIX)
        ]

    # Evidence per name: which stored senders (and from-me flags) its messages
    # already match exactly. This is what identifies "me" and group members
    # without trusting a display name.
    evidence: dict[str, Counter[tuple[bool, str]]] = defaultdict(Counter)
    for r in kept:
        hits = exact_hits(r)
        if len(hits) == 1:
            evidence[r.msg.sender_name][(hits[0].is_from_me, hits[0].sender)] += 1

    me_name, me_source = _pick_me(names, me, evidence, store_dir, conn, target, is_group)

    my_sender = _own_sender(conn, chats)
    mapping: dict[str, tuple[str, str]] = {me_name: (my_sender, "me")}
    name_index = _name_index(conn) if is_group else {}
    chat_senders = {s for row in existing if not row.is_from_me and (s := row.sender)}
    for name in names:
        if name == me_name:
            continue
        if not is_group:
            mapping[name] = (target, "chat partner")
            continue
        got = _map_group_member(conn, name, evidence.get(name, Counter()), name_index, chat_senders)
        if got:
            mapping[name] = got
    unmapped = [n for n in names if n not in mapping]

    plan = Plan(
        chat_jid=target,
        chat_exists=exists,
        chat_title=export.title,
        fmt=fmt,
        tz=tz_name,
        me_name=me_name,
        me_source=me_source,
        my_sender=my_sender,
        mapping=mapping,
        unmapped=unmapped,
        counts=counts,
        first_stored_utc=next((e.utc for e in existing if not e.id.startswith(IMPORT_ID_PREFIX)), None),
        max_rowid=int(max_rowid),
        chat_set=chats,
    )
    _dedupe_and_build(plan, kept, existing, export, store_dir or "", conn)
    return plan


def _pick_me(
    names: list[str],
    me: str | None,
    evidence: dict[str, Counter[tuple[bool, str]]],
    store_dir: str | None,
    conn: sqlite3.Connection,
    target: str,
    is_group: bool,
) -> tuple[str, str]:
    if me:
        want = _norm(clean_name(me))
        for n in names:
            if _norm(n) == want:
                return n, "--me"
        raise ChatImportError(f"--me {me!r} does not appear as a sender in this export; senders: {names}")
    scores = {n: sum(c for (fm, _), c in evidence.get(n, Counter()).items() if fm) for n in names}
    against = {n: sum(c for (fm, _), c in evidence.get(n, Counter()).items() if not fm) for n in names}
    ranked = sorted(names, key=lambda n: scores[n], reverse=True)
    if ranked and scores[ranked[0]] > 0 and scores[ranked[0]] > against[ranked[0]]:
        if len(ranked) == 1 or scores[ranked[1]] == 0:
            return ranked[0], f"matched {scores[ranked[0]]} stored own messages"
    push = _norm(clean_name(_read_whatsapp_push_name(store_dir or "")))
    if push:
        hits = [n for n in names if _norm(n) == push]
        if len(hits) == 1:
            return hits[0], "account push name"
    if not is_group and len(names) == 2:
        partner_names = {
            k for k, v in _name_index(conn).items() if target in v or _canonical_identity(conn, target) in v
        }
        others = [n for n in names if _norm(n) in partner_names]
        if len(others) == 1:
            return next(n for n in names if n != others[0]), "the other name is the chat partner"
    if len(names) == 1 and not is_group:
        raise ChatImportError(
            f"only one sender ({names[0]!r}); pass --me with your own name as it appears in the export"
        )
    raise ChatImportError(f"cannot tell which sender is you; pass --me. Senders: {names}")


def _map_group_member(
    conn: sqlite3.Connection,
    name: str,
    evidence: Counter[tuple[bool, str]],
    name_index: dict[str, set[str]],
    chat_senders: set[str],
) -> tuple[str, str] | None:
    # 1. Unsaved contacts are exported as their number, which is not a guess.
    if _PHONE_NAME.match(name):
        digits = re.sub(r"\D", "", name)
        if len(digits) >= 7:
            return _canonical_identity(conn, f"{digits}@s.whatsapp.net"), "phone number"
    # 2. Stored messages this name's lines already match, all from one person.
    people = {_canonical_identity(conn, s) for (fm, s), _ in evidence.items() if not fm and s}
    if len(people) == 1:
        return people.pop(), f"matched {sum(evidence.values())} stored messages"
    # 3. A directory/nickname name that only one person in this chat carries.
    candidates = name_index.get(_norm(name), set())
    if candidates:
        seen = {c for c in candidates if chat_senders & set(sender_forms(conn, c))}
        if len(seen) == 1:
            return seen.pop(), "directory name, sender in this chat"
        if not seen and len(candidates) == 1:
            return next(iter(candidates)), "directory name (not yet seen in this chat)"
    return None


def _dedupe_and_build(
    plan: Plan,
    kept: list[Resolved],
    existing: list[Existing],
    export: Export,
    store_dir: str,
    conn: sqlite3.Connection,
) -> None:
    counts = plan.counts
    matcher = _Matcher(existing)
    precision = plan.fmt.precision_s
    forms_cache: dict[str, set[str]] = {}

    def forms(sender: str) -> set[str]:
        if sender not in forms_cache:
            forms_cache[sender] = set(sender_forms(conn, sender)) if sender else set()
        return forms_cache[sender]

    mapped_people: set[str] = set()
    for name, (jid, _) in plan.mapping.items():
        if name != plan.me_name:
            mapped_people |= forms(jid)

    def sender_ok(r: Resolved, row: Existing) -> bool:
        is_me = r.msg.sender_name == plan.me_name
        if row.is_from_me != is_me:
            return False
        if is_me:
            return True
        got = plan.mapping.get(r.msg.sender_name)
        if got:
            return row.sender in forms(got[0]) or not row.sender
        # An unmapped name may be anyone except the people already identified.
        return row.sender not in mapped_people

    ordinals: Counter[tuple[str, str, str]] = Counter()
    ids: list[str] = []
    for r in kept:
        key = (r.utc.isoformat(), _norm(r.msg.sender_name), _content_key(r))
        ids.append(_import_id(plan.chat_jid, r, ordinals[key]))
        ordinals[key] += 1

    existing_ids = {e.id for e in existing}
    status: list[str] = [""] * len(kept)
    for idx, r in enumerate(kept):
        if ids[idx] in existing_ids:
            status[idx] = "already_imported"
            for i in matcher.window(r.utc.timestamp() - 1, r.utc.timestamp() + 1):
                if matcher.rows[i].id == ids[idx]:
                    matcher.claimed.add(i)
                    break

    # Pass 1: same sender, same content, close in time; nearest row wins.
    for idx, r in enumerate(kept):
        if status[idx]:
            continue
        t = r.utc.timestamp()
        best = None
        for i in matcher.window(t - EXACT_SLACK_S, t + precision + EXACT_SLACK_S):
            row = matcher.rows[i]
            if sender_ok(r, row) and _same_content(r, row):
                d = abs(matcher.keys[i] - (t + precision / 2))
                if best is None or d < best[0]:
                    best = (d, i)
        if best:
            matcher.claimed.add(best[1])
            status[idx] = "matched_exact"

    # Pass 2: the bridge renders some messages differently from the export
    # (mentions as numbers, locations, polls, contact cards). A row from the
    # same sender inside the exported minute/second that nothing else claimed
    # is the same message; importing it would be a duplicate.
    for idx, r in enumerate(kept):
        if status[idx]:
            continue
        t = r.utc.timestamp()
        for i in matcher.window(t - FUZZY_SLACK_S, t + precision + FUZZY_SLACK_S):
            row = matcher.rows[i]
            if sender_ok(r, row) and not row.id.startswith(IMPORT_ID_PREFIX):
                matcher.claimed.add(i)
                status[idx] = "matched_fuzzy"
                break

    for idx, r in enumerate(kept):
        if status[idx]:
            counts[status[idx]] += 1
            continue
        counts["new"] += 1
        if plan.first_stored_utc and r.utc >= plan.first_stored_utc:
            counts["new_inside_stored_range"] += 1
        name = r.msg.sender_name
        is_me = name == plan.me_name
        # Unmapped people get a sender no JID parser will mistake for a number.
        sender = plan.mapping[name][0] if name in plan.mapping else IMPORT_SENDER_PREFIX + name
        new = NewRow(
            id=ids[idx],
            chat_jid=plan.chat_jid,
            sender=sender,
            sender_name=name,
            content=r.msg.text,
            timestamp=r.stored,
            is_from_me=is_me,
            utc=r.utc,
            is_edited=r.msg.is_edited,
        )
        if name in plan.unmapped:
            counts["new_from_unmapped"] += 1
        if r.msg.kind == "media" and r.msg.attachment:
            new.media_type, new.filename = media_type_for(r.msg.attachment), r.msg.attachment
            if r.msg.attachment not in export.media_names:
                counts["media_missing_from_export"] += 1
            elif export.media_size(r.msg.attachment) > MAX_MEDIA_BYTES:
                counts["media_too_large"] += 1
            else:
                new.media_src = r.msg.attachment
                new.media_dest = media_path(store_dir, plan.chat_jid, new.id, new.media_type, r.msg.attachment)
                counts["media_to_copy"] += 1
        plan.new_rows.append(new)


# --------------------------------------------------------------------------- apply


def _copy_media(export: Export, src: str, dest: str) -> tuple[bytes, int]:
    """Copy one attachment into place, atomically, and return its sha256 and size."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    h = hashlib.sha256()
    size = 0
    tmp = dest + ".tmp"
    with open(tmp, "wb") as out:
        for chunk in export.open_media(src):
            h.update(chunk)
            size += len(chunk)
            out.write(chunk)
    os.replace(tmp, dest)
    return h.digest(), size


def backup_store(db_path: str, backup_dir: str) -> str:
    """Online backup of messages.db, safe against the running bridge."""
    os.makedirs(backup_dir, exist_ok=True)
    dest = os.path.join(backup_dir, f"messages-preimport-{datetime.now():%Y%m%d-%H%M%S}.db")
    with closing(sqlite3.connect(db_path, timeout=30)) as src, closing(sqlite3.connect(dest)) as dst:
        src.backup(dst)
    return dest


def apply_plan(conn: sqlite3.Connection, plan: Plan, export: Export) -> int:
    """Copy media, then insert the planned rows in one short transaction.

    ``conn`` must be in autocommit mode (``isolation_level=None``). Media goes
    first: an orphan file from a failed insert is harmless, a row pointing at a
    missing file is not.

    Raises:
        ChatImportError: the bridge stored rows in this chat's imported time range
        while the plan was being made; re-run so they are matched.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    use = [
        c
        for c in (
            "id",
            "chat_jid",
            "sender",
            "sender_name",
            "content",
            "timestamp",
            "is_from_me",
            "media_type",
            "filename",
            "is_edited",
            "file_sha256",
            "file_length",
        )
        if c in cols
    ]
    sql = f"INSERT OR IGNORE INTO messages ({', '.join(use)}) VALUES ({', '.join('?' * len(use))})"

    rows = []
    for row in plan.new_rows:
        values: dict[str, object] = {
            "id": row.id,
            "chat_jid": row.chat_jid,
            "sender": row.sender,
            "sender_name": row.sender_name,
            "content": row.content,
            "timestamp": row.timestamp,
            "is_from_me": int(row.is_from_me),
            # '' rather than NULL for a text row, as the bridge writes it: the
            # bridge's GetMessages scans these into strings and failed the
            # whole chat on a NULL.
            "media_type": row.media_type or "",
            "filename": row.filename or "",
            "is_edited": int(row.is_edited),
        }
        if row.media_src and row.media_dest:
            # The bridge serves a local copy only if it hashes to file_sha256,
            # which is what keeps a stray file from passing as this message's.
            values["file_sha256"], values["file_length"] = _copy_media(export, row.media_src, row.media_dest)
        rows.append(tuple(values.get(c) for c in use))

    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if plan.new_rows:
            # Matching ran outside this transaction. If the bridge stored rows
            # for this chat inside the imported time span since then (a history
            # sync landing), they were never compared; stop rather than risk
            # duplicating them.
            lo = min(r.utc for r in plan.new_rows) - timedelta(minutes=5)
            hi = max(r.utc for r in plan.new_rows) + timedelta(minutes=5)
            tz = ZoneInfo(plan.tz)
            ph = ",".join("?" * len(plan.chat_set))
            for (ts,) in conn.execute(
                f"SELECT timestamp FROM messages WHERE rowid > ? AND chat_jid IN ({ph})",
                (plan.max_rowid, *plan.chat_set),
            ):
                utc = _parse_stored(ts, tz)
                if utc is not None and lo <= utc <= hi:
                    raise ChatImportError("the bridge stored messages in this time range meanwhile; re-run the import")
        if not plan.chat_exists:
            conn.execute("INSERT OR IGNORE INTO chats (jid, name) VALUES (?, ?)", (plan.chat_jid, plan.chat_title))
        cur = conn.executemany(sql, rows)
        inserted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(rows)
        if plan.new_rows:
            latest = max(plan.new_rows, key=lambda r: r.utc).timestamp
            # Only ever moves forward: an import of old history must not make a
            # chat look idle. datetime() compares instants, not offset strings.
            conn.execute(
                "UPDATE chats SET last_message_time = ? WHERE jid = ? "
                "AND (last_message_time IS NULL OR datetime(last_message_time) < datetime(?))",
                (latest, plan.chat_jid, latest),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return int(inserted)


# --------------------------------------------------------------------------- report / CLI


def format_report(plan: Plan, sample: int = 5) -> str:
    """Human summary of a plan: mapping, counts, and a few new rows."""
    c = plan.counts
    out = [
        f"chat           {plan.chat_jid}{'' if plan.chat_exists else '  (new chat row will be created)'}",
        f"format         {plan.fmt.style}, {plan.fmt.date_order}, {'seconds' if plan.fmt.has_seconds else 'minutes'}, tz {plan.tz}",
        f"me             {plan.me_name!r} ({plan.me_source}) -> {plan.my_sender or '(no own sender known)'}",
        "senders:",
    ]
    for name, (jid, how) in sorted(plan.mapping.items()):
        if name != plan.me_name:
            out.append(f"  {name!r:30} -> {jid}  [{how}]")
    for name in plan.unmapped:
        out.append(f"  {name!r:30} -> {IMPORT_SENDER_PREFIX}{name}  [UNMAPPED]")
    out.append("counts:")
    for key in (
        "lines_parsed",
        "skipped_system",
        "skipped_deleted",
        "skipped_media_omitted",
        "skipped_empty",
        "messages",
        "already_imported",
        "matched_exact",
        "matched_fuzzy",
        "new",
        "new_inside_stored_range",
        "new_from_unmapped",
        "media_to_copy",
        "media_missing_from_export",
        "media_too_large",
        "dst_ambiguous",
    ):
        out.append(f"  {key:28} {c.get(key, 0)}")
    if plan.new_rows:
        first = min(plan.new_rows, key=lambda r: r.utc).timestamp
        last = max(plan.new_rows, key=lambda r: r.utc).timestamp
        out.append(f"new range      {first} .. {last}")
    if plan.first_stored_utc:
        out.append(f"store starts   {plan.first_stored_utc:%Y-%m-%d %H:%M:%S}Z")
    if sample and plan.new_rows:
        out.append(f"sample of new rows ({min(sample, len(plan.new_rows))}):")
        for row in plan.new_rows[:sample]:
            text = row.content.replace("\n", " ")
            text = text if len(text) <= 60 else text[:57] + "..."
            media = f" [{row.media_type}]" if row.media_type else ""
            out.append(f"  {row.timestamp}  {row.sender_name}:{media} {text}")
    return "\n".join(out)


def _emit(text: str) -> None:
    # The report is this command's output, not a log line: stdout, unprefixed,
    # so it can be read or piped. Diagnostics still go through the logger.
    sys.stdout.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Dry run unless ``--apply``."""
    ap = argparse.ArgumentParser(prog="python -m lib.chat_import", description=__doc__.split("\n\n")[0])
    ap.add_argument("export", help="WhatsApp export: the .zip, or the .txt (attachments looked up beside it)")
    ap.add_argument("--chat", required=True, help="target chat JID, e.g. 4915112345678@s.whatsapp.net or …@g.us")
    ap.add_argument("--me", help="your own name as it appears in the export (detected when omitted)")
    ap.add_argument("--tz", default=DEFAULT_TZ, help=f"phone's time zone when exported (default {DEFAULT_TZ})")
    ap.add_argument("--date-order", choices=["DMY", "MDY", "YMD"], help="override day/month order detection")
    ap.add_argument("--db", default=MESSAGES_DB_PATH, help="messages.db (default: from WA_STORE_PATH)")
    ap.add_argument(
        "--backup-dir", default=os.path.expanduser("~/backups/whatsapp"), help="where --apply backs up first"
    )
    ap.add_argument("--sample", type=int, default=5, help="new rows to show in the report (0 = none)")
    ap.add_argument("--apply", action="store_true", help="write; without it nothing is changed")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        _emit(f"error: no messages.db at {args.db}")
        return 2
    store_dir = os.path.dirname(os.path.abspath(args.db))
    try:
        export = read_export(args.export)
        with closing(sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)) as ro:
            plan = build_plan(
                ro, export, args.chat, me=args.me, tz_name=args.tz, date_order=args.date_order, store_dir=store_dir
            )
        _emit(format_report(plan, args.sample))
        if not args.apply:
            _emit("\ndry run: nothing written. Re-run with --apply to import.")
            return 0
        if not plan.new_rows:
            _emit("\nnothing new to import.")
            return 0
        backup = backup_store(args.db, args.backup_dir)
        _emit(f"\nbackup         {backup}")
        with closing(sqlite3.connect(args.db, timeout=30, isolation_level=None)) as rw:
            inserted = apply_plan(rw, plan, export)
        _emit(f"imported       {inserted} rows, {sum(1 for r in plan.new_rows if r.media_dest)} media files")
        logger.info("chat import: %d rows into %s", inserted, plan.chat_jid)
        return 0
    except ChatImportError as exc:
        _emit(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

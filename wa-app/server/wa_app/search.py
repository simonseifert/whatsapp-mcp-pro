"""Message search: an FTS5 index in app.db over the bridge's messages (never written back to it).

Indexed: text messages, media captions and stored voice transcripts, including chats marked local only (the index
never leaves this server). Not indexed: deleted, system, poll, poll vote and contact card rows.

Every write reads the message's current state from the bridge first, so an edit replaces the indexed text and a
revoke removes the row whatever order the events and the backfill arrive in. Reads also re-check the bridge, so a
deleted message can never be shown even if the index has not caught up yet.
"""

import logging
import re
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

from . import appdb, store
from .config import Settings
from .live import Event
from .unread import NOT_POLL_VOTE

logger = logging.getLogger("wa_app.search")

BATCH = 2000
DEFAULT_LIMIT = 30
SNIPPET_TOKENS = 14
META_ROWID = "search_rowid"
META_EDIT = "search_edit_id"

# Reads and writes of one message happen under this lock, so two writers never interleave a stale read with a write.
_index_lock = threading.Lock()


@dataclass
class Hit:
    chat_jid: str
    chat_name: str
    message_id: str
    ts: str
    sender_name: str | None
    snippet: str


# --- query handling -----------------------------------------------------------------------------------------------

_TOKEN = re.compile(r"\w+", re.UNICODE)


def fts_query(text: str) -> str | None:
    """User text -> an FTS5 MATCH expression, or None when there is nothing to search for. Words only, each quoted so
    no FTS syntax ('"', '*', NEAR, OR, a column filter) ever reaches the parser; the last word matches as a prefix."""
    tokens = _TOKEN.findall(text)
    if not tokens or (len(tokens) == 1 and len(tokens[0]) < 2):
        return None
    quoted = [f'"{t}"' for t in tokens]
    quoted[-1] += "*"
    return " ".join(quoted)


# --- writing ------------------------------------------------------------------------------------------------------


def _epoch(value: str | None) -> int | None:
    parsed = store.parse_ts(value)
    return int(parsed.timestamp()) if parsed else None


def _drop(appconn: sqlite3.Connection, chat_jid: str, message_id: str) -> None:
    row = appconn.execute(
        "SELECT fts_rowid FROM msg_fts_keys WHERE chat_jid=? AND message_id=?", (chat_jid, message_id)
    ).fetchone()
    if row:
        appconn.execute("DELETE FROM msg_fts WHERE rowid=?", (row[0],))
        appconn.execute("DELETE FROM msg_fts_keys WHERE chat_jid=? AND message_id=?", (chat_jid, message_id))


def _put(appconn: sqlite3.Connection, forms: list[str], chat_jid: str, message_id: str, ts: int, body: str) -> None:
    """Replace the indexed text of one message, under whichever form of the chat it was indexed before."""
    for form in dict.fromkeys([chat_jid, *forms]):
        _drop(appconn, form, message_id)
    cur = appconn.execute(
        "INSERT INTO msg_fts (body, chat_jid, message_id, ts) VALUES (?,?,?,?)", (body, chat_jid, message_id, ts)
    )
    appconn.execute(
        "INSERT OR REPLACE INTO msg_fts_keys (chat_jid, message_id, fts_rowid) VALUES (?,?,?)",
        (chat_jid, message_id, cur.lastrowid),
    )


def _body(content: str | None, media_type: str | None, transcript: str | None) -> str | None:
    """The text to index for one row, or None when it has none worth finding."""
    text = (content or "").strip()
    if text.startswith(("[Poll: ", "[Contact: ")):
        return None
    if not text and media_type in ("audio", "ptt"):
        text = (transcript or "").strip()
    return text or None


def _transcripts(appconn: sqlite3.Connection, ids: list[str]) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for chunk in store._chunks(ids):
        rows = appconn.execute(
            f"SELECT message_id, chat_jid, text FROM transcripts WHERE message_id IN ({store._marks(chunk)})", chunk
        ).fetchall()
        out.update({(m, c): t or "" for m, c, t in rows})
    return out


def _transcript_for(transcripts: dict[tuple[str, str], str], forms: list[str], raw_chat: str, mid: str) -> str | None:
    for form in dict.fromkeys([raw_chat, *forms]):
        if (mid, form) in transcripts:
            return transcripts[(mid, form)]
    return None


def _columns(ro: sqlite3.Connection) -> tuple[str, str]:
    cols = store._columns(ro, "messages")
    return (
        "deleted_at" if "deleted_at" in cols else "NULL",
        "COALESCE(is_system_message, 0)" if "is_system_message" in cols else "0",
    )


_SELECT = "SELECT rowid, id, chat_jid, content, timestamp, media_type, {deleted}, {system} FROM messages"


def _apply_rows(
    appconn: sqlite3.Connection, rows: list[tuple], index: dict[str, str], forms_of: Callable[[str], list[str]]
) -> int:
    """Index (or drop) each row in the current state it was read in. The caller holds both locks."""
    transcripts = _transcripts(appconn, [r[1] for r in rows if r[5] in ("audio", "ptt") and not (r[3] or "").strip()])
    written = 0
    for _, mid, raw_chat, content, stamp, media_type, deleted, system in rows:
        chat = index.get(raw_chat, raw_chat)
        forms = forms_of(chat)
        ts = _epoch(stamp)
        body = None
        if not deleted and not system and ts is not None:
            body = _body(content, media_type, _transcript_for(transcripts, forms, raw_chat, mid))
        if body is None or ts is None:
            for form in dict.fromkeys([chat, raw_chat, *forms]):
                _drop(appconn, form, mid)
            continue
        _put(appconn, forms, chat, mid, ts, body)
        written += 1
    return written


def _forms_cache(ro: sqlite3.Connection) -> tuple[dict[str, str], Callable[[str], list[str]]]:
    conversations = store._conversations(ro)
    index = {form: c.jid for c in conversations for form in (*c.forms, c.jid)}
    by_jid = {c.jid: c.forms for c in conversations}

    def forms_of(chat: str) -> list[str]:
        return by_jid.get(chat, [chat])

    return index, forms_of


def _meta_int(appconn: sqlite3.Connection, key: str) -> int | None:
    row = appconn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    try:
        return int(row[0]) if row else None
    except (TypeError, ValueError):
        return None


def _set_meta(appconn: sqlite3.Connection, key: str, value: int) -> None:
    appconn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))


def index_messages(
    appconn: sqlite3.Connection,
    ro: sqlite3.Connection,
    *,
    since_rowid: int | None = None,
    max_batches: int | None = None,
) -> int:
    """Backfill the index from the bridge's rows after `since_rowid` (default: where the last run stopped, kept in
    meta 'search_rowid'), 2000 rows at a time, saving the position after each batch so a restart resumes. Returns how
    many messages were indexed. `max_batches` stops early (used to bound one unit of work)."""
    with appdb.db_lock:
        start = since_rowid if since_rowid is not None else (_meta_int(appconn, META_ROWID) or 0)
    deleted, system = _columns(ro)
    total = 0
    batches = 0
    while max_batches is None or batches < max_batches:
        total_done, last = _batch(appconn, ro, start, deleted, system)
        if last is None:
            break
        start = last
        total += total_done
        batches += 1
    return total


def _batch(
    appconn: sqlite3.Connection, ro: sqlite3.Connection, start: int, deleted: str, system: str
) -> tuple[int, int | None]:
    with _index_lock:
        rows = ro.execute(
            _SELECT.format(deleted=deleted, system=system)
            + f" WHERE rowid > ? AND {NOT_POLL_VOTE} ORDER BY rowid LIMIT ?",
            (start, BATCH),
        ).fetchall()
        if not rows:
            top = ro.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()[0]
            if top > start:  # only poll votes remained
                with appdb.db_lock:
                    _set_meta(appconn, META_ROWID, top)
            return 0, None
        index, forms_of = _forms_cache(ro)
        with appdb.db_lock:
            appconn.execute("BEGIN")
            try:
                written = _apply_rows(appconn, rows, index, forms_of)
                _set_meta(appconn, META_ROWID, rows[-1][0])
                appconn.execute("COMMIT")
            except BaseException:
                appconn.execute("ROLLBACK")
                raise
        return written, rows[-1][0]


def reindex(appconn: sqlite3.Connection, ro: sqlite3.Connection, pairs: list[tuple[str, str]]) -> None:
    """Re-read each (chat jid in any form, message id) from the bridge and make the index match it."""
    if not pairs:
        return
    deleted, system = _columns(ro)
    with _index_lock:
        index, forms_of = _forms_cache(ro)
        rows: list[tuple] = []
        gone: list[tuple[str, str]] = []
        for chat, mid in pairs:
            forms = forms_of(index.get(chat, chat))
            found = ro.execute(
                _SELECT.format(deleted=deleted, system=system)
                + f" WHERE id=? AND chat_jid IN ({store._marks(forms)}) LIMIT 1",
                (mid, *forms),
            ).fetchall()
            rows.extend(found)
            if not found:
                gone.append((index.get(chat, chat), mid))
        with appdb.db_lock:
            appconn.execute("BEGIN")
            try:
                _apply_rows(appconn, rows, index, forms_of)
                for chat, mid in gone:
                    for form in dict.fromkeys([chat, *forms_of(chat)]):
                        _drop(appconn, form, mid)
                appconn.execute("COMMIT")
            except BaseException:
                appconn.execute("ROLLBACK")
                raise


def reconcile(appconn: sqlite3.Connection, ro: sqlite3.Connection) -> None:
    """Catch up on what the rowid backfill cannot see: edits and revokes of messages that were already indexed, made
    while the app was not running."""
    pairs: list[tuple[str, str]] = []
    top: int | None = None
    if store._columns(ro, "message_edits"):
        with appdb.db_lock:
            since = _meta_int(appconn, META_EDIT)
        top = ro.execute("SELECT COALESCE(MAX(id), 0) FROM message_edits").fetchone()[0]
        if since is None:  # first run: the backfill reads current text anyway
            since = top
        pairs += ro.execute("SELECT DISTINCT chat_jid, message_id FROM message_edits WHERE id > ?", (since,)).fetchall()
    if "deleted_at" in store._columns(ro, "messages"):
        revoked = ro.execute("SELECT chat_jid, id FROM messages WHERE deleted_at IS NOT NULL").fetchall()
        with appdb.db_lock:
            known = {tuple(r) for r in appconn.execute("SELECT chat_jid, message_id FROM msg_fts_keys")}
        index, forms_of = _forms_cache(ro)
        pairs += [
            (c, m)
            for c, m in revoked
            if any((form, m) in known for form in [index.get(c, c), *forms_of(index.get(c, c))])
        ]
    for i in range(0, len(pairs), BATCH):
        reindex(appconn, ro, pairs[i : i + BATCH])
    if top is not None:  # only now: a failed batch leaves the cursor so the next run retries it
        with appdb.db_lock:
            _set_meta(appconn, META_EDIT, top)


# --- live ---------------------------------------------------------------------------------------------------------


class Indexer:
    """Keeps the index current. All work runs on one thread, in order, so a live event never races the backfill."""

    def __init__(self, settings: Settings, appconn: sqlite3.Connection) -> None:
        self.settings = settings
        self.appconn = appconn
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wa-search")

    def on_events(self, events: list[Event]) -> None:
        """Poller listener: a new message is indexed, an edited one replaced, a revoked one removed."""
        pairs: dict[tuple[str, str], None] = {}
        resync = False
        for event in events:
            if event.name == "message":
                message = event.data.get("message") or {}
                pairs[(str(event.data.get("chat_jid")), str(message.get("id")))] = None
            elif event.name == "message_changed":
                pairs[(str(event.data.get("chat_jid")), str(event.data.get("id")))] = None
            elif event.name == "resync":
                resync = True
        if pairs or resync:
            self._executor.submit(self._safe, list(pairs), resync)

    def _safe(self, pairs: list[tuple[str, str]], resync: bool) -> None:
        try:
            ro = store.open_ro(self.settings)
            try:
                if resync:  # a flood or a chat merge: the events do not name every row
                    index_messages(self.appconn, ro)
                    reconcile(self.appconn, ro)
                reindex(self.appconn, ro, pairs)
            finally:
                ro.close()
        except Exception as exc:  # the type only: messages may quote chat text
            logger.warning("search index update failed (%s)", type(exc).__name__)

    def index_one(self, chat_jid: str, message_id: str) -> None:
        """Re-index one message now (a voice transcript was just stored)."""
        self._executor.submit(self._safe, [(chat_jid, message_id)], False)

    def backfill_step(self) -> bool:
        """One batch of backfill on the index thread; False when nothing was left to do."""

        def work() -> bool:
            ro = store.open_ro(self.settings)
            try:
                deleted, system = _columns(ro)
                with appdb.db_lock:
                    start = _meta_int(self.appconn, META_ROWID) or 0
                _, last = _batch(self.appconn, ro, start, deleted, system)
                return last is not None
            finally:
                ro.close()

        return self._executor.submit(work).result()

    def reconcile_now(self) -> None:
        def work() -> None:
            ro = store.open_ro(self.settings)
            try:
                reconcile(self.appconn, ro)
            finally:
                ro.close()

        self._executor.submit(work).result()

    def flush(self) -> None:
        """Wait until everything queued so far has run (tests, shutdown)."""
        self._executor.submit(lambda: None).result()

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


# --- reading ------------------------------------------------------------------------------------------------------


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def parse_cursor(value: str | None) -> tuple[int, int] | None:
    """The 'before' cursor from a previous page: '<epoch>.<rowid>'. Raises ValueError when it is not one."""
    if value is None or value == "":
        return None
    head, _, tail = value.partition(".")
    return int(head), int(tail)


def search(
    appconn: sqlite3.Connection,
    ro: sqlite3.Connection,
    query: str,
    *,
    chat_jid: str | None = None,
    before: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[Hit], str | None]:
    """Newest-first hits and the cursor for the next page (None at the end). Raises ValueError for a bad cursor."""
    cursor = parse_cursor(before)
    match = fts_query(query)
    if match is None:
        return [], None
    conversations = store._conversations(ro)
    index = {form: c.jid for c in conversations for form in (*c.forms, c.jid)}
    forms_by_chat = {c.jid: c.forms for c in conversations}
    scope: list[str] | None = None
    if chat_jid:
        canonical = index.get(chat_jid, chat_jid)
        scope = list(dict.fromkeys([canonical, chat_jid, *forms_by_chat.get(canonical, [])]))
    has_deleted = "deleted_at" in store._columns(ro, "messages")
    names: dict[str, str] = {}
    hits: list[Hit] = []
    next_cursor: str | None = None
    while len(hits) < limit:
        sql = "SELECT rowid, chat_jid, message_id, ts, snippet(msg_fts, 0, '[', ']', '…', ?) FROM msg_fts WHERE msg_fts MATCH ?"
        args: list[object] = [SNIPPET_TOKENS, match]
        if scope is not None:
            sql += f" AND chat_jid IN ({store._marks(scope)})"
            args += scope
        if cursor is not None:
            sql += " AND (ts < ? OR (ts = ? AND rowid < ?))"
            args += [cursor[0], cursor[0], cursor[1]]
        sql += " ORDER BY ts DESC, rowid DESC LIMIT ?"
        args.append(limit - len(hits) + 1)
        with appdb.db_lock:
            rows = appconn.execute(sql, args).fetchall()
        if not rows:
            next_cursor = None
            break
        more = len(rows) > limit - len(hits)
        for rowid, chat, mid, ts, snippet in rows[: limit - len(hits)]:
            cursor = (ts, rowid)
            canonical = index.get(chat, chat)
            live = _live_sender(ro, scope_forms(forms_by_chat, canonical, chat), mid, has_deleted)
            if live is None:  # deleted or gone from the bridge: never shown
                continue
            if canonical not in names:
                with appdb.db_lock:
                    summary = store.get_chat(ro, appconn, canonical)
                names[canonical] = summary.name if summary else canonical
            hits.append(Hit(canonical, names[canonical], mid, _iso(ts), live[0], snippet))
        next_cursor = f"{cursor[0]}.{cursor[1]}" if more and cursor else None
        if not more:
            break
    return hits, next_cursor


def scope_forms(forms_by_chat: dict[str, list[str]], canonical: str, raw: str) -> list[str]:
    return list(dict.fromkeys([raw, canonical, *forms_by_chat.get(canonical, [])]))


def _live_sender(ro: sqlite3.Connection, forms: list[str], mid: str, has_deleted: bool) -> tuple[str | None] | None:
    """(sender label,) when the message still exists and is not deleted, else None."""
    row = ro.execute(
        "SELECT sender, sender_name, COALESCE(is_from_me, 0), "
        + ("deleted_at" if has_deleted else "NULL")
        + f" FROM messages WHERE id=? AND chat_jid IN ({store._marks(forms)}) LIMIT 1",
        (mid, *forms),
    ).fetchone()
    if row is None or row[3]:
        return None
    sender, stored, from_me, _ = row
    if from_me:
        return ("You",)
    names = store.resolve_display_names(ro, {sender}) if sender else {}
    return (store._sender_label(sender, stored, names),)

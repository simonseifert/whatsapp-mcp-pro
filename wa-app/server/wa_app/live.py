"""Live updates: a poller that watches the bridge's store for changes, and the SSE fan-out to open clients.

The bridge writes `messages.db`; nothing pushes. Once a second the Poller reads what changed since its last look
(new message rows by rowid, applied edits, deletes, reactions, chat state) and turns it into events. Events carry the
JID the chat list uses for the conversation, so a client can match them to the chat it has open however the store
files the rows (LID or phone form). A new rowid does not mean a new message: history sync and backfill insert old
messages too, and INSERT OR REPLACE re-inserts an existing one, so clients place a `message` by its timestamp and
dedupe by (chat_jid, id).
"""

import asyncio
import contextlib
import json
import logging
import secrets
import sqlite3
import time
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import appdb, store
from .config import Settings
from .tokens import token_hash as auth_hash

logger = logging.getLogger("wa_app.live")

POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 15.0
RETRY_MS = 3000
HISTORY = 500  # events kept for Last-Event-ID resume
CLIENT_QUEUE = 100  # events a client may lag behind before it is dropped (it reconnects and resumes)
MAX_MESSAGES_PER_TICK = 100  # more than this in one tick is a history sync: tell clients to refetch instead

_CHAT_STATE_COLUMNS = (
    "pinned_changed_at",
    "muted_until_changed_at",
    "archived_changed_at",
    "marked_unread_changed_at",
    "last_read_at",
)


_CHAT_STATE_VALUES = ("pinned", "muted_until", "archived", "marked_unread", "last_read_at", "history_unread_count")


_CLOSE = "_close"  # sentinel event name: the stream ends


@dataclass
class Event:
    name: str
    data: dict[str, Any]
    cursor: int = 0


# --- hub ----------------------------------------------------------------------------------------------------------


@dataclass(eq=False)
class Client:
    id: str
    queue: asyncio.Queue[Event]
    visible: bool = False
    chat: str | None = None  # the chat this client has open (its canonical jid), reported with its visibility
    dropped: bool = False
    session: str | None = None  # hash of the session token it was opened with
    resume_point: int = 0  # the hello's id: everything after it is still queued for this client


_hub: "Hub | None" = None


class Hub:
    """Fan-out to the connected clients. Used from the event loop only."""

    def __init__(self, history: int = HISTORY, queue_size: int = CLIENT_QUEUE) -> None:
        global _hub
        self._history: deque[Event] = deque(maxlen=history)
        self._queue_size = queue_size
        self._clients: dict[str, Client] = {}
        # Cursors are milliseconds-since-epoch * 1000 at start, then +1 per event: monotonic across restarts, so a
        # Last-Event-ID from before a restart is recognised as unresumable.
        self._base = time.time_ns() // 1_000_000 * 1000
        self._cursor = self._base
        _hub = self

    def publish(self, name: str, data: dict[str, Any]) -> Event:
        self._cursor += 1
        event = Event(name, data, self._cursor)
        self._history.append(event)
        for client in list(self._clients.values()):
            try:
                client.queue.put_nowait(event)
            except asyncio.QueueFull:
                client.dropped = True  # a slow client is dropped; it reconnects and resumes from its last id
                del self._clients[client.id]
                logger.info("dropped a slow live client")
        return event

    @property
    def cursor(self) -> int:
        return self._cursor

    def _end(self, client: Client) -> None:
        """Ends one client's stream at once: flag it and wake its reader (a full queue is already waking it)."""
        client.dropped = True
        self._clients.pop(client.id, None)
        with contextlib.suppress(asyncio.QueueFull):
            client.queue.put_nowait(Event(_CLOSE, {}))

    def close(self) -> None:
        """Shutdown: every open stream ends now instead of at its next heartbeat."""
        for client in list(self._clients.values()):
            self._end(client)

    def drop_session(self, session: str) -> None:
        """A session ended (logout, expiry): its streams end now."""
        for client in list(self._clients.values()):
            if client.session == session:
                self._end(client)

    def subscribe(self, last_event_id: int | None = None, session: str | None = None) -> Client:
        client = Client(
            secrets.token_urlsafe(9),
            asyncio.Queue(maxsize=self._queue_size),
            session=session,
            resume_point=self._cursor,
        )
        if last_event_id is not None:
            gap = last_event_id < self._base or (bool(self._history) and self._history[0].cursor > last_event_id + 1)
            pending = [] if gap else [e for e in self._history if e.cursor > last_event_id]
            # A replay that would not fit the queue is treated like a gap: one resync instead of QueueFull.
            if gap or len(pending) >= self._queue_size:
                client.resume_point = last_event_id  # a drop before the resync flushes reproduces the gap
                client.queue.put_nowait(Event("resync", {}, self._cursor))
            else:
                client.resume_point = last_event_id  # the replay below is queued after the hello
                for event in pending:
                    client.queue.put_nowait(event)
        self._clients[client.id] = client
        return client

    def unsubscribe(self, client: Client) -> None:
        self._clients.pop(client.id, None)

    def client_count(self) -> int:
        return len(self._clients)

    def set_visible(self, client_id: str, visible: bool, chat: str | None = None) -> bool:
        client = self._clients.get(client_id)
        if client is None:
            return False
        client.visible = visible
        client.chat = chat
        return True

    def chat_open(self, chat_jid: str) -> bool:
        """True when any visible client reports this chat open (also read from the notifier's thread)."""
        return any(c.visible and c.chat == chat_jid for c in list(self._clients.values()))

    def visible_count(self) -> int:
        return sum(1 for c in self._clients.values() if c.visible)


def current_hub() -> Hub:
    assert _hub is not None, "no Hub created yet"
    return _hub


def drop_session(session: str) -> None:
    """End the open streams of a session that is over (called by auth when a session is deleted)."""
    if _hub:
        _hub.drop_session(session)


def drop_all_sessions() -> None:
    """Every session ended at once (log out everywhere): all open streams end now."""
    if _hub:
        _hub.close()


def visible_clients() -> int:
    """How many connected clients report their page as visible (M2's 'app open')."""
    return _hub.visible_count() if _hub else 0


def _frame(event: Event) -> str:
    return f"id: {event.cursor}\nevent: {event.name}\ndata: {json.dumps(event.data)}\n\n"


async def sse_stream(
    hub: Hub,
    last_event_id: int | None,
    heartbeat: float = HEARTBEAT_SECONDS,
    session: str | None = None,
    still_valid: Callable[[str], bool] | None = None,
) -> AsyncGenerator[str, None]:
    """The SSE body. With `session` and `still_valid`, the session is re-checked (off the event loop) at least every
    `heartbeat` seconds and the stream ends when it is over."""
    client = hub.subscribe(last_event_id, session)
    try:
        yield f"retry: {RETRY_MS}\n\n"
        # The hello's id is the resume point captured in subscribe(), before any replay or publish was queued: a drop
        # right after it resumes from there and loses nothing that was still queued.
        yield f"id: {client.resume_point}\nevent: hello\ndata: {json.dumps({'client': client.id})}\n\n"
        checked = time.monotonic()
        while not client.dropped:
            try:
                event = await asyncio.wait_for(client.queue.get(), heartbeat)
            except TimeoutError:
                event = None
            if session and still_valid and time.monotonic() - checked >= heartbeat:
                checked = time.monotonic()
                if not await run_in_threadpool(still_valid, session):
                    return
            if event is None:
                yield ": heartbeat\n\n"
            elif event.name == _CLOSE:
                return
            else:
                yield _frame(event)
    finally:
        hub.unsubscribe(client)


def routes(
    hub: Hub, still_valid: Callable[[str], bool] | None = None, cookie: str | tuple[str, ...] = ""
) -> list[Route]:
    async def stream(request: Request) -> Response:
        raw = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
        last = int(raw) if raw and raw.isdigit() else None
        names = (cookie,) if isinstance(cookie, str) else cookie
        token = next((request.cookies[n] for n in names if n and request.cookies.get(n)), None)
        return StreamingResponse(
            sse_stream(hub, last, session=auth_hash(token) if token else None, still_valid=still_valid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def visibility(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("visible"), bool):
            return JSONResponse({"error": "visible (true/false) required"}, status_code=400)
        client = body.get("client")
        chat = body.get("chat")
        if chat is not None and not isinstance(chat, str):
            return JSONResponse({"error": "chat must be a string or null"}, status_code=400)
        if not isinstance(client, str) or not hub.set_visible(client, body["visible"], chat):
            return JSONResponse({"error": "Unknown client"}, status_code=404)
        return JSONResponse({"ok": True})

    return [
        Route("/api/live/visibility", visibility, methods=["POST"]),
        Route("/api/live", stream, methods=["GET"]),
    ]


# --- poller -------------------------------------------------------------------------------------------------------


class _Mark:
    """A high-water mark over a time column. Timestamps have second granularity, so the rows exactly at the mark
    are looked at again next tick and only reported when their key is new."""

    def __init__(self, value: float = 0.0, seen: frozenset = frozenset()) -> None:
        self.value = value
        self.seen = seen

    def advance(self, rows: list[tuple]) -> tuple["_Mark", list[tuple]]:
        """rows: (key..., julianday). Returns the new mark and the rows not reported before."""
        fresh = [r for r in rows if r[-1] > self.value or r[:-1] not in self.seen]
        top = max((r[-1] for r in rows), default=self.value)
        if top > self.value:
            return _Mark(top, frozenset(r[:-1] for r in rows if r[-1] == top)), fresh
        return _Mark(self.value, self.seen | frozenset(r[:-1] for r in rows if r[-1] == top)), fresh


@dataclass
class _Cursors:
    rowid: int = 0
    edit_id: int = 0
    deleted: _Mark = field(default_factory=_Mark)
    reacted: _Mark = field(default_factory=_Mark)
    chat_state: _Mark = field(default_factory=_Mark)
    receipted: _Mark = field(default_factory=_Mark)


class Poller:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cursors: _Cursors | None = None
        self._appconn: sqlite3.Connection | None = None
        self._failing = False
        self._schema: dict[str, set[str]] = {}
        # Called from the poller thread with each poll's events (the M2 draft worker registers here). They must be
        # thread-safe and quick; a failing one never stops the poll.
        self.on_events: list[Callable[[list[Event]], None]] = []
        # Its own thread: sends (up to ~60 s), avatars, thumbnails and transcription share AnyIO's default pool, and a
        # burst of them must never make a live tick wait. One thread also keeps the poller's connections in one place.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wa-poller")

    # Reads of the bridge's tables. Each returns rows and tolerates a store that lacks the table or column.

    def _has(self, conn: sqlite3.Connection, table: str, column: str | None = None) -> bool:
        """Table/column existence, looked up once per baseline when present (the schema does not change under a running app)."""
        cols = self._schema.get(table)
        if not cols:  # an absent table is looked up again: the bridge may create it later
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if cols:
                self._schema[table] = cols
        return bool(cols) and (column is None or column in cols)

    def _deleted(self, conn: sqlite3.Connection, since: float) -> list[tuple]:
        if not self._has(conn, "messages", "deleted_at"):
            return []
        return conn.execute(
            "SELECT chat_jid, id, julianday(deleted_at) FROM messages"
            " WHERE deleted_at IS NOT NULL AND julianday(deleted_at) >= ?",
            (since,),
        ).fetchall()

    def _reactions(self, conn: sqlite3.Connection, since: float) -> list[tuple]:
        if not self._has(conn, "message_reactions"):
            return []
        return conn.execute(
            "SELECT chat_jid, message_id, sender, emoji, julianday(reacted_at) FROM message_reactions"
            " WHERE julianday(reacted_at) >= ?",
            (since,),
        ).fetchall()

    def _receipts(self, conn: sqlite3.Connection, since: float) -> list[tuple]:
        """(chat_jid, message_id, participant, status, time): the status is part of the key, so a receipt raised from
        delivered to read in the same second is reported again."""
        if not self._has(conn, "message_receipts"):
            return []
        at = "CASE WHEN typeof(at) IN ('integer', 'real') THEN at / 86400.0 + 2440587.5 ELSE julianday(at) END"
        return conn.execute(
            f"SELECT chat_jid, message_id, participant, status, t FROM"
            f" (SELECT chat_jid, message_id, participant, status, {at} AS t FROM message_receipts)"
            f" WHERE t IS NOT NULL AND t >= ?",
            (since,),
        ).fetchall()

    def _chat_states(self, conn: sqlite3.Connection, since: float) -> list[tuple]:
        """(chat_jid, *state values, newest change time). The values are part of the key: two changes to one chat in
        the same second (the timestamps' resolution) still differ by what they changed."""
        if not self._has(conn, "chat_state"):
            return []
        times = [c for c in _CHAT_STATE_COLUMNS if self._has(conn, "chat_state", c)]
        values = [c for c in _CHAT_STATE_VALUES if self._has(conn, "chat_state", c)]
        if not times:
            return []
        newest = (
            "MAX(" + ", ".join(f"COALESCE(julianday({c}), 0)" for c in times) + ")"
            if len(times) > 1
            else f"COALESCE(julianday({times[0]}), 0)"
        )
        cols = ", ".join(["chat_jid", *values])
        return conn.execute(
            f"SELECT {cols}, t FROM (SELECT {cols}, {newest} AS t FROM chat_state) WHERE t > 0 AND t >= ?",
            (since,),
        ).fetchall()

    def _baseline(self, conn: sqlite3.Connection) -> _Cursors:
        self._schema = {}
        rowid = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()[0]
        edit = (
            conn.execute("SELECT COALESCE(MAX(id), 0) FROM message_edits").fetchone()[0]
            if self._has(conn, "message_edits")
            else 0
        )
        return _Cursors(
            rowid=rowid,
            edit_id=edit,
            deleted=_Mark().advance(self._deleted(conn, 0.0))[0],
            reacted=_Mark().advance(self._reactions(conn, 0.0))[0],
            chat_state=_Mark().advance(self._chat_states(conn, 0.0))[0],
            receipted=_Mark().advance(self._receipts(conn, 0.0))[0],
        )

    def poll_once(self) -> list[Event]:
        """What changed since the last call. The first call only takes a baseline. Never raises on a busy or
        missing store: it logs and the next tick tries again from the same place."""
        try:
            ro = store.open_ro(self.settings)
        except sqlite3.Error as exc:
            self._note_failure(exc)
            return []
        try:
            if self._cursors is None:
                self._cursors = self._baseline(ro)
                self._failing = False
                return []
            events, self._cursors = self._collect(ro, self._cursors)
            self._failing = False
            return events
        except sqlite3.Error as exc:
            self._note_failure(exc)
            return []
        finally:
            ro.close()

    def _note_failure(self, exc: Exception) -> None:
        if not self._failing:  # once per outage, not once per second
            logger.warning("live poll could not read the store (%s); will retry", exc)
        self._failing = True

    def _collect(self, ro: sqlite3.Connection, cur: _Cursors) -> tuple[list[Event], _Cursors]:
        new = _Cursors(cur.rowid, cur.edit_id, cur.deleted, cur.reacted, cur.chat_state, cur.receipted)
        inserted = ro.execute(
            "SELECT rowid, id, chat_jid FROM messages WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (cur.rowid, MAX_MESSAGES_PER_TICK + 1),
        ).fetchall()
        flood = len(inserted) > MAX_MESSAGES_PER_TICK
        shrunk = False
        if inserted:
            new.rowid = ro.execute("SELECT MAX(rowid) FROM messages").fetchone()[0] if flood else inserted[-1][0]
        else:  # rows were removed (a chat merge moves messages): the cursor is past the end, so rebase it
            top = ro.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()[0]
            if top < cur.rowid:
                new.rowid, shrunk = top, True
        edits: list[tuple] = []
        if self._has(ro, "message_edits"):
            edits = ro.execute(
                "SELECT id, chat_jid, message_id, status FROM message_edits WHERE id > ? ORDER BY id", (cur.edit_id,)
            ).fetchall()
            if edits:
                new.edit_id = edits[-1][0]
        new.deleted, deleted = cur.deleted.advance(self._deleted(ro, cur.deleted.value))
        new.reacted, reacted = cur.reacted.advance(self._reactions(ro, cur.reacted.value))
        new.chat_state, states = cur.chat_state.advance(self._chat_states(ro, cur.chat_state.value))
        new.receipted, receipts = cur.receipted.advance(self._receipts(ro, cur.receipted.value))
        if not (inserted or edits or deleted or reacted or states or receipts or shrunk):
            return [], new

        index = store.conversation_index(ro)

        def canon(jid: str) -> str:
            return index.get(jid, jid)

        events: list[Event] = []
        touched: dict[str, None] = {}  # canonical chat jids, in order of first touch
        added: set[tuple[str, str]] = set()
        if flood or shrunk:
            events.append(Event("resync", {}))
        else:
            for _, mid, chat in inserted:
                message = store.get_message(ro, chat, mid)
                if message is None:
                    continue  # not shown in a chat (a poll vote, an unparseable timestamp)
                added.add((chat, mid))
                events.append(Event("message", {"chat_jid": canon(chat), "message": asdict(message)}))
                touched[canon(chat)] = None
        changed: dict[tuple[str, str], None] = {}
        for _, chat, mid, status in edits:
            if status == "applied":
                changed[(canon(chat), mid)] = None
        for chat, mid, _ in deleted:
            changed[(canon(chat), mid)] = None
        for chat, mid in changed:
            if not any(canon(c) == chat and m == mid for c, m in added):
                events.append(Event("message_changed", {"chat_jid": chat, "id": mid}))
            touched[chat] = None
        seen_reactions: dict[tuple[str, str], None] = {}
        ticked: dict[tuple[str, str], None] = {}  # messages whose ticks changed; the chat row only if it is the last
        for chat, mid, _, _, _ in receipts:
            key = (canon(chat), mid)
            if key not in changed and not any(canon(c) == key[0] and m == mid for c, m in added):
                ticked[key] = None
        for chat, mid in ticked:
            events.append(Event("message_changed", {"chat_jid": chat, "id": mid}))
        for chat, mid, _, _, _ in reacted:
            seen_reactions[(canon(chat), mid)] = None
        for chat, mid in seen_reactions:
            events.append(Event("reaction", {"chat_jid": chat, "message_id": mid}))
        for row in states:
            touched[canon(row[0])] = None
        if self._appconn is None:
            self._appconn = appdb.connect(self.settings)
        for chat in touched:
            summary = store.get_chat(ro, self._appconn, chat)
            if summary is not None:
                events.append(Event("chat", {"chat": asdict(summary)}))
        for chat in dict.fromkeys(c for c, _ in ticked if c not in touched):
            summary = store.get_chat(ro, self._appconn, chat)
            if summary is not None and (chat, summary.last_message.id) in ticked:
                events.append(Event("chat", {"chat": asdict(summary)}))
        return events, new

    def _tick(self) -> list[Event]:
        events = self.poll_once()
        if events:
            for listener in list(self.on_events):
                try:
                    listener(events)
                except Exception:
                    logger.exception("live event listener failed")
        return events

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def run(self, hub: Hub, interval: float = POLL_SECONDS) -> None:
        """Poll until cancelled; a failing tick never ends the loop."""
        while True:
            try:
                for event in await asyncio.get_running_loop().run_in_executor(self._executor, self._tick):
                    hub.publish(event.name, event.data)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("live poll failed")
            await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def running(poller: Poller, hub: Hub) -> AsyncIterator[None]:
    task = asyncio.create_task(poller.run(hub))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        poller.close()

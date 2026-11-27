"""History coverage: how much of the user's own writing is stored per chat, so drafts have enough to learn from."""

import asyncio
import logging
import sqlite3
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from . import store
from .logsafe import log_safe

THIN_OWN = 30  # fewer own text messages than this and a chat is "thin"
FILL_CHATS = 50  # how many of the most recently active chats coverage and backfills look at
_UNIX_EPOCH_JULIAN = 2440587.5


@dataclass(frozen=True)
class ChatCoverage:
    jid: str
    name: str
    kind: str
    messages: int
    own_texts: int
    oldest: str | None
    last_active: str | None
    thin: bool
    backfill_jid: str | None  # the stored form of the chat with the most messages; None when nothing is stored


def _skipped(jid: str) -> bool:
    return jid.endswith(("@broadcast", "@newsletter"))


def _iso(julian: float | None) -> str | None:
    if julian is None:
        return None
    return datetime.fromtimestamp(round((julian - _UNIX_EPOCH_JULIAN) * 86400), UTC).isoformat()


def _form_map(convs: list[store._Conversation]) -> dict[str, store._Conversation]:
    return {form: conv for conv in convs for form in (conv.jid, *conv.forms)}


def totals(ro: sqlite3.Connection) -> dict:
    """Stored messages, merged chats that have any (phone and LID forms count once; broadcast and newsletter JIDs
    are left out, like in coverage()) and the earliest message."""
    count, oldest = ro.execute("SELECT COUNT(*), MIN(julianday(timestamp)) FROM messages").fetchone()
    forms = _form_map(store._conversations(ro))
    stored = [r[0] for r in ro.execute("SELECT DISTINCT chat_jid FROM messages")]
    chats = {forms[j].jid if j in forms else j for j in stored if j and not _skipped(j)}
    return {"messages": count, "chats": len(chats), "oldest": _iso(oldest)}


def _stats(ro: sqlite3.Connection, forms: list[str], own: str) -> list[tuple[str, int, int, float | None]]:
    """Per stored form: message count, own text count and the earliest timestamp, in one pass over the chat index."""
    return ro.execute(
        f"SELECT chat_jid, COUNT(*), SUM(CASE WHEN {own} THEN 1 ELSE 0 END), MIN(julianday(timestamp))"
        f" FROM messages WHERE chat_jid IN ({store._marks(forms)}) GROUP BY chat_jid",
        forms,
    ).fetchall()


def _empty_chats(
    ro: sqlite3.Connection, convs: list[store._Conversation], known: set[str]
) -> list[tuple[str, str, str, str]]:
    """Real chats the list leaves out because nothing is stored yet: (jid, name, kind, last_active)."""
    out = []
    for conv in convs:
        if conv.jid in known or _skipped(conv.jid):
            continue
        last = ro.execute(
            f"SELECT MAX(last_message_time) FROM chats WHERE jid IN ({store._marks(conv.forms)})", conv.forms
        ).fetchone()[0]
        stamp = store.parse_ts(last)
        if stamp is None:
            continue  # a shell (community parent and the like), not a chat
        name = next((n for n in conv.rows.values() if n), conv.jid.split("@")[0])
        out.append((conv.jid, name, "group" if conv.jid.endswith("@g.us") else "private", stamp.isoformat()))
    return out


def coverage(ro: sqlite3.Connection, appconn: sqlite3.Connection, *, limit: int = FILL_CHATS) -> list[ChatCoverage]:
    """The `limit` most recently active chats (archived ones too), most recent first, with their own-text coverage."""
    listed, _ = store.list_chats_with_count(ro, appconn, archived=None, limit=100_000)
    entries: list[tuple[str, str, str, str]] = [
        (c.jid, c.name, c.kind, c.last_message.timestamp_iso)
        for c in listed
        if c.kind != "newsletter" and not _skipped(c.jid)
    ]
    convs = store._conversations(ro)  # once: _forms_of would rebuild it for every chat
    by_form = _form_map(convs)
    entries += _empty_chats(ro, convs, {c.jid for c in listed})
    entries.sort(key=lambda e: store._sort_key(e[3]), reverse=True)
    own = store.own_text_filter(deleted_col="deleted_at" in store._columns(ro, "messages"))
    return [
        _cover(ro, by_form[jid].forms if jid in by_form else [jid], own, jid, name, kind, last_active)
        for jid, name, kind, last_active in entries[:limit]
    ]


def _cover(
    ro: sqlite3.Connection, forms: list[str], own: str, jid: str, name: str, kind: str, last_active: str | None
) -> ChatCoverage:
    per_form = _stats(ro, forms, own)
    own_texts = sum(r[2] or 0 for r in per_form)
    earliest = [r[3] for r in per_form if r[3] is not None]
    best = max(per_form, key=lambda r: r[1], default=None)
    return ChatCoverage(
        jid=jid,
        name=name,
        kind=kind,
        messages=sum(r[1] for r in per_form),
        own_texts=own_texts,
        oldest=_iso(min(earliest)) if earliest else None,
        last_active=last_active,
        thin=own_texts < THIN_OWN,
        backfill_jid=best[0] if best else None,
    )


def chat_coverage(ro: sqlite3.Connection, appconn: sqlite3.Connection, jid: str) -> ChatCoverage | None:
    """Coverage of one chat by any of its JID forms, without scanning the others. None when the chat is unknown or is
    a broadcast or newsletter (those are never backfilled)."""
    conv = next((c for c in store._conversations(ro) if jid == c.jid or jid in c.forms), None)
    if conv is None or _skipped(conv.jid):
        return None
    own = store.own_text_filter(deleted_col="deleted_at" in store._columns(ro, "messages"))
    chat = store.get_chat(ro, appconn, conv.jid)
    if chat is not None:
        if chat.kind == "newsletter":
            return None
        return _cover(ro, conv.forms, own, conv.jid, chat.name, chat.kind, chat.last_message.timestamp_iso)
    name = next((n for n in conv.rows.values() if n), conv.jid.split("@")[0])
    return _cover(ro, conv.forms, own, conv.jid, name, "group" if conv.jid.endswith("@g.us") else "private", None)


# --- backfill queue -------------------------------------------------------------------------------------------

Depth = Literal["year", "3y", "all"]
_DEPTH_DAYS: dict[str, int | None] = {"year": 365, "3y": 1095, "all": None}
BACKFILL_BATCHES = 200
_DONE_KEPT = 100
_STATUS_MISSES = 3  # status calls in a row that fail or do not list the job before the bridge counts as gone
_DONE_BY = {"phone has nothing older", "reached until"}
logger = logging.getLogger("wa_app.history")


class BackfillQueue:
    """Backfills chats one at a time through the bridge's own per-chat job: start it, poll its status, move on.

    A failed start, a bridge that stops answering or a job that outlives the watchdog marks that chat failed and the
    queue carries on. A 409 (a job is already running, e.g. after an app restart) is waited out, not an error."""

    def __init__(
        self,
        start: Callable[[str, str | None, int], dict],
        status: Callable[[], dict],
        *,
        poll_s: float = 10.0,
        watchdog_s: float = 1800.0,
        today: Callable[[], date] = date.today,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._start = start
        self._status = status
        self._poll_s = poll_s
        self._watchdog_s = watchdog_s
        self._today = today
        self._clock = clock
        self._queue: deque[tuple[str, str | None, Depth]] = deque()
        self._running: dict | None = None
        self._done: deque[dict] = deque(maxlen=_DONE_KEPT)
        self._wake = asyncio.Event()
        self._stopped = False

    def enqueue(self, items: list[tuple[str, str | None]], depth: Depth) -> int:
        """Add `(jid, backfill_jid)` chats; ones already queued or running are left alone. Returns how many were added."""
        known = {q[0] for q in self._queue}
        if self._running:
            known.add(self._running["jid"])
        added = 0
        for jid, backfill_jid in items:
            if jid in known:
                continue
            known.add(jid)
            self._queue.append((jid, backfill_jid, depth))
            added += 1
        if added:
            self._wake.set()
        return added

    def cancel(self) -> None:
        """Drop what is queued. A job the bridge is already running finishes on its own."""
        self._queue.clear()

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    def snapshot(self) -> dict:
        return {
            "running": dict(self._running) if self._running else None,
            "queued": [q[0] for q in self._queue],
            "done": list(self._done),
        }

    async def run(self) -> None:
        while not self._stopped:
            if not self._queue:
                self._wake.clear()
                await self._wake.wait()
                continue
            jid, backfill_jid, depth = self._queue.popleft()
            self._running = {"jid": jid, "batches": 0, "oldest": None}
            try:
                result, detail, oldest = await self._process(backfill_jid, depth)
            except Exception as exc:  # noqa: BLE001  # one chat never takes the queue down; the type only, no text
                result, detail, oldest = "failed", f"unexpected error ({type(exc).__name__})", None
            self._running = None
            self._done.append({"jid": jid, "result": result, "detail": detail, "oldest": oldest})
            logger.info("history backfill %s: %s (%s)", log_safe(jid), result, log_safe(detail))

    def _until(self, depth: Depth) -> str | None:
        days = _DEPTH_DAYS[depth]
        return None if days is None else (self._today() - timedelta(days=days)).isoformat()

    async def _process(self, backfill_jid: str | None, depth: Depth) -> tuple[str, str, str | None]:
        if backfill_jid is None:
            return "skipped", "nothing to anchor on", None
        started = await asyncio.to_thread(self._start, backfill_jid, self._until(depth), BACKFILL_BATCHES)
        job = started.get("job")
        if not started.get("success") and not (isinstance(job, dict) and job.get("running")):
            return "failed", str(started.get("error") or "the bridge refused the backfill"), None
        watched = job.get("chat_jid", backfill_jid) if isinstance(job, dict) else backfill_jid
        return await self._wait(watched)

    async def _wait(self, chat_jid: str) -> tuple[str, str, str | None]:
        began = self._clock()
        misses = 0
        while True:
            if self._clock() - began > self._watchdog_s:
                return "failed", "timed out", None
            await asyncio.sleep(self._poll_s)
            reply = await asyncio.to_thread(self._status)
            job = next((j for j in reply.get("jobs") or [] if j.get("chat_jid") == chat_jid), None)
            if not reply.get("success") or job is None:
                misses += 1
                if misses >= _STATUS_MISSES:
                    return "failed", str(reply.get("error") or "the bridge lost the job"), None
                continue
            misses = 0
            oldest = job.get("oldest") or None
            if self._running is not None:
                self._running.update(batches=job.get("batches", 0), oldest=oldest)
            if job.get("running"):
                continue
            stopped_by = job.get("stopped_by") or ""
            if stopped_by in _DONE_BY:
                return "done", stopped_by, oldest
            if stopped_by == "max batches":
                return "more", stopped_by, oldest
            return "failed", str(job.get("error") or stopped_by or "the bridge stopped the backfill"), oldest

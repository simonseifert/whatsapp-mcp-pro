"""Reply drafts: the drafts table and the worker that makes them.

A draft is only ever shown. There is no code path from here to `sending`: the app fills the composer and the user
sends it with a tap (a test pins that this module never imports `sending`).

The worker's state machine, per chat (at most one job per chat):

    message event -> eligible? -> [cancel the chat's job] -> job: wait (closed app: 20 s quiet) -> row `pending`
    -> policy gate + prompt (thread) -> semaphore (local 1 / cloud 3) -> backend -> `ready` | `failed`

A newer trigger for the chat cancels the job (the backend call is cancelled with it) and marks its row `superseded`.
Threading: `on_events` runs on the poller's thread and only hands the events to the event loop; everything else runs
on the loop, and blocking work (DNS, SQLite reads of the bridge store) goes through `asyncio.to_thread`.
"""

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import appdb, live, store
from ..config import Settings
from . import backends, learning, policy
from . import config as ai_config
from . import prompt as prompts

logger = logging.getLogger("wa_app.ai.drafts")

MAX_AGE_SECONDS = 10 * 60  # a message older than this when first seen is history, not news
DEBOUNCE_SECONDS = 20.0
EXPIRY_SECONDS = 24 * 3600
LOCAL_SLOTS = 1
CLOUD_SLOTS = 3
NIGHTLY_AT = (3, 30)  # local time of the nightly rules sweep
GENERIC_ERROR = "Couldn't draft"
_LIVE = ("pending", "ready", "failed")
_QUOTE_MAX = 120

Complete = Callable[..., Awaitable[str]]


@dataclass
class DraftView:
    id: int
    chat_jid: str
    trigger_id: str
    status: str
    alternatives: list[str]
    error: str | None
    quote: store.QuoteView | None
    created_at_iso: str


# --- the drafts table ---------------------------------------------------------------------------------------------


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).astimezone().isoformat()


def _quote(
    ro: sqlite3.Connection, appconn: sqlite3.Connection, chat_jid: str, trigger_id: str
) -> store.QuoteView | None:
    """In groups the draft quotes the message it answers."""
    chat = store.get_chat(ro, appconn, chat_jid)
    if chat is None or chat.kind not in store.GROUP_KINDS:
        return None
    message = store.get_message(ro, chat_jid, trigger_id)
    if message is None:
        return None
    body = prompts._body(message, appconn, chat_jid) or ""
    preview = " ".join(body.split())
    if len(preview) > _QUOTE_MAX:
        preview = preview[: _QUOTE_MAX - 1] + "…"
    return store.QuoteView(message.id, message.sender_name, preview)


_COLS = "id, chat_jid, trigger_id, status, alternatives, error, created_at"


def _view(appconn: sqlite3.Connection, row: tuple, ro: sqlite3.Connection | None) -> DraftView:
    try:
        alternatives = [a for a in json.loads(row[4] or "[]") if isinstance(a, str)]
    except ValueError:
        alternatives = []
    quote = None
    if ro is not None:
        try:
            quote = _quote(ro, appconn, row[1], row[2])
        except sqlite3.Error:
            quote = None
    return DraftView(row[0], row[1], row[2], row[3], alternatives, row[5], quote, _iso(row[6]))


def get(appconn: sqlite3.Connection, draft_id: int, ro: sqlite3.Connection | None = None) -> DraftView | None:
    with appdb.db_lock:
        row = appconn.execute(f"SELECT {_COLS} FROM drafts WHERE id=?", (draft_id,)).fetchone()
    return _view(appconn, row, ro) if row else None


def current(
    appconn: sqlite3.Connection, chat_jid: str, ro: sqlite3.Connection | None = None, *, now: float | None = None
) -> DraftView | None:
    """The chat's latest pending, ready or failed draft; rows older than 24 h are flipped to `expired` first.
    `ro` (the bridge store) is only needed to fill `quote` in groups."""
    cutoff = int(now if now is not None else time.time()) - EXPIRY_SECONDS
    with appdb.db_lock:
        appconn.execute(
            "UPDATE drafts SET status='expired' WHERE chat_jid=? AND status IN ('pending','ready','failed')"
            " AND created_at < ?",
            (chat_jid, cutoff),
        )
        row = appconn.execute(
            f"SELECT {_COLS} FROM drafts WHERE chat_jid=? AND status IN ('pending','ready','failed')"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (chat_jid,),
        ).fetchone()
    return _view(appconn, row, ro) if row else None


def dismiss(appconn: sqlite3.Connection, draft_id: int) -> None:
    with appdb.db_lock:
        appconn.execute(
            "UPDATE drafts SET status='dismissed' WHERE id=? AND status IN ('pending','ready','failed')", (draft_id,)
        )


def _create(appconn: sqlite3.Connection, chat_jid: str, trigger_id: str, backend_id: int | None, now: float) -> int:
    with appdb.db_lock:
        # Whatever the chat was still showing is replaced by this one.
        appconn.execute(
            "UPDATE drafts SET status='superseded' WHERE chat_jid=? AND status IN ('pending','ready','failed')",
            (chat_jid,),
        )
        cur = appconn.execute(
            "INSERT INTO drafts (chat_jid, trigger_id, status, backend_id, created_at) VALUES (?,?,'pending',?,?)",
            (chat_jid, trigger_id, backend_id, int(now)),
        )
    assert cur.lastrowid is not None
    return cur.lastrowid


def _settle(
    appconn: sqlite3.Connection, draft_id: int, status: str, *, alternatives: list[str] | None = None,
    error: str | None = None, now: float,
) -> bool:  # fmt: skip
    """pending -> ready/failed/superseded. False when the row was no longer pending (dismissed, superseded)."""
    with appdb.db_lock:
        cur = appconn.execute(
            "UPDATE drafts SET status=?, alternatives=?, error=?, ready_at=? WHERE id=? AND status='pending'",
            (
                status,
                json.dumps(alternatives) if alternatives is not None else None,
                error,
                int(now) if status == "ready" else None,
                draft_id,
            ),
        )
    return cur.rowcount > 0


# --- the worker ---------------------------------------------------------------------------------------------------


def _triggers_draft(message: store.MessageView) -> bool:
    """Automatic drafts answer text, voice notes and media that has a caption; nothing else."""
    if message.poll or message.contact_cards:
        return False
    if message.media:
        if message.media.type in ("audio", "ptt"):
            return True
        return message.media.type != "sticker" and bool((message.text or "").strip())
    return bool((message.text or "").strip())


@dataclass(eq=False)
class _Job:
    chat_jid: str
    trigger_id: str
    task: "asyncio.Task[None] | None" = None
    draft_id: int | None = None
    trigger_at: float = 0.0  # when the trigger message was sent: an older own row cannot be a reply to it


@dataclass
class _Candidate:
    chat_jid: str
    message: dict[str, Any]
    received: float  # when the worker first saw it: the 10-minute rule is measured to here


class DraftWorker:
    def __init__(
        self,
        settings: Settings,
        hub: live.Hub,
        *,
        appconn: sqlite3.Connection | None = None,
        complete: Complete = backends.complete,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        visible: Callable[[], int] = live.visible_clients,
        debounce: float = DEBOUNCE_SECONDS,
        nightly_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.hub = hub
        self._appconn = appconn if appconn is not None else appdb.connect(settings)
        self._complete = complete
        self._now = now
        self._sleep = sleep
        self._visible = visible
        self._debounce = debounce
        self._nightly_sleep = nightly_sleep
        self._distills: dict[str, asyncio.Task[None]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inbox: asyncio.Queue[_Candidate] | None = None
        self._jobs: dict[str, _Job] = {}
        self._local = asyncio.Semaphore(LOCAL_SLOTS)
        self._cloud = asyncio.Semaphore(CLOUD_SLOTS)
        self._consuming = False
        # Extra observers, called on the event loop with (chat_jid, message dict) for every own message the poller
        # reports, whatever device it was sent from. Outcome learning itself is built in (`_own_message`).
        self.own_message_listeners: list[Callable[[str, dict[str, Any]], None]] = []

    # -- entry from the poller thread

    def on_events(self, events: list[live.Event]) -> None:
        """Thread-safe: called from the poller's thread. Only new-message events matter; they are handed to the loop."""
        loop, inbox = self._loop, self._inbox
        if loop is None or inbox is None:
            return  # not running (yet): nothing to draft for
        received = self._now()
        for event in events:
            if event.name != "message":
                continue
            chat, message = event.data.get("chat_jid"), event.data.get("message")
            if isinstance(chat, str) and isinstance(message, dict):
                with contextlib.suppress(RuntimeError):  # the loop closed under us: shutting down
                    loop.call_soon_threadsafe(inbox.put_nowait, _Candidate(chat, message, received))

    # -- lifecycle

    async def run(self) -> None:
        """Consume triggers until cancelled; cancelling also cancels every running draft."""
        self._loop = asyncio.get_running_loop()
        self._inbox = asyncio.Queue()
        with appdb.db_lock:  # rows a previous process left pending will never finish
            self._appconn.execute("UPDATE drafts SET status='superseded' WHERE status='pending'")
        learning.prune_feedback(self._appconn, self._now())
        nightly = asyncio.get_running_loop().create_task(self._nightly())
        try:
            while True:
                candidate = await self._inbox.get()
                self._consuming = True
                try:
                    await self._consider(candidate)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("draft trigger failed (%s)", type(exc).__name__)
                finally:
                    self._consuming = False
        finally:
            nightly.cancel()
            self.close()

    def close(self) -> None:
        """Cancel running drafts and distills; their rows stop being pending. Safe to call twice."""
        for task in list(self._distills.values()):
            task.cancel()
        self._distills.clear()
        for job in list(self._jobs.values()):
            self._cancel(job, publish=False)
        self._jobs.clear()

    async def idle(self) -> None:
        """Returns when nothing is queued or running (for tests and orderly shutdown)."""
        while True:
            tasks = [j.task for j in self._jobs.values() if j.task is not None and not j.task.done()]
            tasks += [t for t in self._distills.values() if not t.done()]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                continue
            if self._inbox is not None and (not self._inbox.empty() or self._consuming):
                await asyncio.sleep(0)
                continue
            return

    # -- triggers

    async def _consider(self, candidate: _Candidate) -> None:
        message = candidate.message
        mid = message.get("id")
        if not isinstance(mid, str):
            return
        if message.get("from_me"):
            for listener in list(self.own_message_listeners):
                try:
                    listener(candidate.chat_jid, message)
                except Exception as exc:
                    logger.error("own-message listener failed (%s)", type(exc).__name__)
            await self._own_message(candidate.chat_jid, message)
            return
        if mid.startswith("import-") or message.get("system") or message.get("deleted"):
            return
        running = self._jobs.get(candidate.chat_jid)
        if running and running.trigger_id == mid and running.task and not running.task.done():
            return  # the bridge re-inserted the same message (INSERT OR REPLACE): not a new trigger
        if await asyncio.to_thread(self._eligible, candidate):
            raw = message.get("timestamp_iso")
            stamp = store.parse_ts(raw) if isinstance(raw, str) else None
            self._start(
                candidate.chat_jid, mid, manual=False, trigger_at=stamp.timestamp() if stamp else candidate.received
            )

    # -- outcomes and rules

    async def _own_message(self, chat_jid: str, message: dict[str, Any]) -> None:
        """The user replied (any device, text or media): the chat's draft is stale. Record the outcome when it links,
        else just supersede; stop the running job; remove the card live only when something changed."""
        text, mid = message.get("text"), message.get("id")
        stamp = store.parse_ts(message["timestamp_iso"]) if isinstance(message.get("timestamp_iso"), str) else None
        sent_at = stamp.timestamp() if stamp else self._now()
        body = text if isinstance(text, str) and text.strip() else None
        try:
            changed, kind = await asyncio.to_thread(self._learn, chat_jid, mid, body, sent_at)
        except Exception as exc:
            logger.error("outcome failed (%s)", type(exc).__name__)
            return
        job = self._jobs.get(chat_jid)
        if job is not None and sent_at >= job.trigger_at - learning._SLACK:  # older: a re-delivered row, not a reply
            self._cancel(job, publish=False)  # the user got there first
            changed = True
        if changed:
            self._publish(chat_jid, None)
        if (
            kind in ("used", "edited")
            and await asyncio.to_thread(learning.pending_outcomes, self._appconn, chat_jid) == learning.DISTILL_AT
        ):
            self._schedule_distill(chat_jid)  # exactly once per crossing; a failed distill waits for the nightly sweep

    def _learn(self, chat_jid: str, mid: object, text: str | None, sent_at: float) -> tuple[bool, str | None]:
        kind = None
        if text is not None and isinstance(mid, str):
            kind = learning.record_own_message(self._appconn, chat_jid, mid, text, sent_at)
        changed = kind is not None
        return learning.supersede_live(self._appconn, chat_jid, sent_at) or changed, kind

    def _schedule_distill(self, chat_jid: str) -> None:
        """At most one distill per chat at a time."""
        running = self._distills.get(chat_jid)
        if running is not None and not running.done():
            return
        task = asyncio.get_running_loop().create_task(self._distill(chat_jid))
        self._distills[chat_jid] = task

        def forget(done: "asyncio.Task[None]") -> None:
            if self._distills.get(chat_jid) is done:
                del self._distills[chat_jid]

        task.add_done_callback(forget)

    async def _distill(self, chat_jid: str) -> None:
        async def limited(
            cfg: ai_config.BackendCfg, secret: str | None, prompt: backends.Prompt, settings: Settings
        ) -> str:
            local = await asyncio.to_thread(policy.is_local, cfg)
            async with self._local if local else self._cloud:
                if not await asyncio.to_thread(self._unchanged, chat_jid, cfg):  # raises Refused when now refused
                    raise policy.Refused("AI settings changed")
                return await self._complete(cfg, secret, prompt, settings)

        await learning.distill(self.settings, self._appconn, chat_jid, complete=limited)

    async def _nightly(self) -> None:
        """Every night at 03:30 local: distill the chats that have new outcomes since their last distill."""
        while True:
            await self._nightly_sleep(_seconds_until_nightly(self._now()))
            try:
                await asyncio.to_thread(learning.prune_feedback, self._appconn, self._now())
                await asyncio.to_thread(learning.prune_old_drafts, self._appconn, self._now())
                for chat_jid in await asyncio.to_thread(learning.chats_with_new_outcomes, self._appconn):
                    self._schedule_distill(chat_jid)
            except Exception as exc:
                logger.error("nightly sweep failed (%s)", type(exc).__name__)

    def _eligible(self, candidate: _Candidate) -> bool:
        """The trigger rules (blocking: reads app.db and the bridge store)."""
        chat_jid, mid = candidate.chat_jid, candidate.message["id"]
        with appdb.db_lock:
            enabled = ai_config.chat_ai(self._appconn, chat_jid).enabled
            seen = self._appconn.execute(
                "SELECT 1 FROM drafts WHERE chat_jid=? AND trigger_id=?", (chat_jid, mid)
            ).fetchone()
        if not enabled or seen:
            return False
        try:
            ro = store.open_ro(self.settings)
        except sqlite3.Error:
            return False
        try:
            message = store.get_message(ro, chat_jid, mid)
            if message is None or message.from_me or message.system or message.deleted:
                return False
            stamp = store.parse_ts(message.timestamp_iso)
            if stamp is None or candidate.received - stamp.timestamp() > MAX_AGE_SECONDS:
                return False
            if not _triggers_draft(message):
                return False  # polls, contact cards, stickers, caption-less media: nothing to reply to
            if prompts._body(message, self._appconn, chat_jid) is None:
                return False
            chat = store.get_chat(ro, self._appconn, chat_jid)
            if chat is not None and chat.kind == "announcement":
                return _i_am_admin(ro, chat_jid)
            return True
        except sqlite3.Error:
            return False
        finally:
            ro.close()

    def request(self, chat_jid: str, trigger_id: str | None = None) -> DraftView:
        """The one-off "Draft" button: runs now, ignores `enabled` (the policy gate still applies).
        Call on the event loop. Raises LookupError when there is no message to reply to."""
        ro = store.open_ro(self.settings)
        try:
            found = trigger_id if trigger_id is not None else self._last_incoming(ro, chat_jid)
            message = store.get_message(ro, chat_jid, found) if found else None
            if message is None or prompts._body(message, self._appconn, chat_jid) is None:
                raise LookupError("Nothing to reply to")
            if message.from_me or message.system or message.deleted:
                raise LookupError("That message can't be replied to")
            with appdb.db_lock:
                backend = ai_config.effective_backend(self._appconn, chat_jid)
            draft_id = _create(self._appconn, chat_jid, message.id, backend.id if backend else None, self._now())
            stamp = store.parse_ts(message.timestamp_iso)
            self._start(
                chat_jid, message.id, manual=True, draft_id=draft_id, trigger_at=stamp.timestamp() if stamp else 0.0
            )
            view = get(self._appconn, draft_id, ro)  # with the quote, like the SSE event
        finally:
            ro.close()
        assert view is not None
        return view

    def _last_incoming(self, ro: sqlite3.Connection, chat_jid: str) -> str | None:
        for m in reversed(store.list_messages(ro, chat_jid, limit=30)):
            if not m.from_me and not m.system and not m.deleted and prompts._body(m, self._appconn, chat_jid):
                return m.id
        return None

    def dismiss(self, draft_id: int) -> None:
        """Dismiss a draft (the card's x): stops its job too."""
        view = get(self._appconn, draft_id)
        if view is None:
            return
        dismiss(self._appconn, draft_id)
        job = self._jobs.get(view.chat_jid)
        if job and job.draft_id == draft_id:
            self._cancel(job, publish=False)
        self._publish(view.chat_jid, None)

    # -- jobs

    def _start(
        self, chat_jid: str, trigger_id: str, *, manual: bool, draft_id: int | None = None, trigger_at: float = 0.0
    ) -> _Job:
        old = self._jobs.get(chat_jid)
        if old is not None:
            self._cancel(old, publish=True)
        job = _Job(chat_jid, trigger_id, draft_id=draft_id, trigger_at=trigger_at)
        self._jobs[chat_jid] = job
        job.task = asyncio.get_running_loop().create_task(self._job(job, manual))
        if draft_id is not None:
            self._publish_draft(draft_id)
        return job

    def _cancel(self, job: _Job, *, publish: bool) -> None:
        if job.task is not None and not job.task.done():
            job.task.cancel()
        if job.draft_id is not None and _settle(self._appconn, job.draft_id, "superseded", now=self._now()):
            if publish:
                self._publish(job.chat_jid, None)  # the replacement announces itself when it starts
        if self._jobs.get(job.chat_jid) is job:
            del self._jobs[job.chat_jid]

    async def _job(self, job: _Job, manual: bool) -> None:
        try:
            if not manual and self._visible() <= 0:
                await self._sleep(self._debounce)  # closed app: wait for the chat to go quiet
            if not manual and not await asyncio.to_thread(self._still_enabled, job.chat_jid):
                return  # switched off while we waited: no row, no card
            if job.draft_id is None:
                backend = await asyncio.to_thread(self._backend_id, job.chat_jid)
                job.draft_id = _create(self._appconn, job.chat_jid, job.trigger_id, backend, self._now())
                self._publish_draft(job.draft_id)
            await self._draft(job, job.draft_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never lets a draft take the worker down; content is not logged
            logger.error("draft failed (%s)", type(exc).__name__)
            if job.draft_id is not None:  # no row yet (the insert itself failed): nothing to mark or show
                try:
                    self._finish(job, "failed", error=GENERIC_ERROR)
                except Exception as inner:
                    logger.error("could not record the failure (%s)", type(inner).__name__)
        finally:
            if self._jobs.get(job.chat_jid) is job:
                del self._jobs[job.chat_jid]

    def _still_enabled(self, chat_jid: str) -> bool:
        with appdb.db_lock:
            return ai_config.chat_ai(self._appconn, chat_jid).enabled

    def _backend_id(self, chat_jid: str) -> int | None:
        with appdb.db_lock:
            backend = ai_config.effective_backend(self._appconn, chat_jid)
        return backend.id if backend else None

    async def _draft(self, job: _Job, draft_id: int) -> None:
        try:
            raw = await self._complete_in_slot(job)
            alternatives = backends.parse_alternatives(raw)
        except policy.Refused as exc:
            self._finish(job, "failed", error=exc.reason)
            return
        except backends.BackendError as exc:
            self._finish(job, "failed", error=str(exc))
            return
        self._finish(job, "ready", alternatives=alternatives)

    async def _complete_in_slot(self, job: _Job) -> str:
        """Prepare, wait for a slot, then check again: the chat or backend may have changed while we queued, and
        a chat that became local only must not reach a cloud backend. A changed backend means preparing again."""
        for _ in range(3):
            cfg, secret, prompt, local = await asyncio.to_thread(self._prepare, job)
            async with self._local if local else self._cloud:
                if await asyncio.to_thread(self._unchanged, job.chat_jid, cfg):  # raises Refused when now refused
                    return await self._complete(cfg, secret, prompt, self.settings)
        raise backends.BackendError("AI settings changed, try again")

    def _unchanged(self, chat_jid: str, prepared: ai_config.BackendCfg) -> bool:
        with appdb.db_lock:
            chat = ai_config.chat_ai(self._appconn, chat_jid)
            cfg = ai_config.effective_backend(self._appconn, chat_jid)
        policy.check(chat, cfg)
        return cfg == prepared

    def _prepare(self, job: _Job) -> tuple[ai_config.BackendCfg, str | None, backends.Prompt, bool]:
        """Policy gate and prompt (blocking: DNS and SQLite)."""
        with appdb.db_lock:
            chat = ai_config.chat_ai(self._appconn, job.chat_jid)
            cfg = ai_config.effective_backend(self._appconn, job.chat_jid)
            secret = ai_config.backend_secret(self._appconn, self.settings, cfg.id) if cfg else None
        policy.check(chat, cfg)  # raises Refused before any content is read or sent
        assert cfg is not None
        local = policy.is_local(cfg)
        ro = store.open_ro(self.settings)
        try:
            prompt = prompts.build_draft_prompt(ro, self._appconn, job.chat_jid, job.trigger_id, chat)
        finally:
            ro.close()
        return cfg, secret, prompt, local

    def _finish(
        self, job: _Job, status: str, *, alternatives: list[str] | None = None, error: str | None = None
    ) -> None:
        assert job.draft_id is not None
        if _settle(self._appconn, job.draft_id, status, alternatives=alternatives, error=error, now=self._now()):
            self._publish_draft(job.draft_id)

    # -- events

    def _publish_draft(self, draft_id: int) -> None:
        try:
            ro: sqlite3.Connection | None = store.open_ro(self.settings)
        except sqlite3.Error:
            ro = None
        try:
            view = get(self._appconn, draft_id, ro)
        finally:
            if ro is not None:
                ro.close()
        if view is not None:
            self._publish(view.chat_jid, view)

    def _publish(self, chat_jid: str, view: DraftView | None) -> None:
        self.hub.publish("draft", {"chat_jid": chat_jid, "draft": asdict(view) if view else None})


def _seconds_until_nightly(now: float) -> float:
    local = datetime.fromtimestamp(now)
    target = local.replace(hour=NIGHTLY_AT[0], minute=NIGHTLY_AT[1], second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return (target - local).total_seconds()


def _i_am_admin(ro: sqlite3.Connection, chat_jid: str) -> bool:
    """The user's admin flag for a group, from the bridge's identities table. Unknown means no (fail closed)."""
    if "i_am_admin" not in store._columns(ro, "identities"):
        return False
    forms = store._forms_of(ro, chat_jid)
    rows = ro.execute(f"SELECT i_am_admin FROM identities WHERE jid IN ({store._marks(forms)})", forms).fetchall()
    return any(r[0] for r in rows)

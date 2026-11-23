"""What the user did with a draft, and the per-chat rules learned from it.

`record_own_message` links the first own message after a draft (from any device) to that draft and stores how close it
was; `distill` turns the recent drafts-vs-sent pairs into at most 12 plain rule lines. Nothing here sends anything:
this module never imports `sending` (the send route hands it a draft hint instead).
"""

import asyncio
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from difflib import SequenceMatcher

from .. import appdb
from ..config import Settings
from . import backends, policy
from . import config as ai_config
from . import prompt as prompts

logger = logging.getLogger("wa_app.ai.learning")

WINDOW_SECONDS = 15 * 60
USED = 0.95
EDITED = 0.35
DISTILL_AT = 10
MAX_RULE_LINES = 12
MAX_RULE_LINE = 200
PAIRS = 20
HINT_TTL = 120.0
MAX_COMPARE = 4096  # chars compared per side: the matcher is quadratic
DRAFT_KEEP_DAYS = 90
_SLACK = 2  # seconds: timestamps have 1 s resolution
_LIVE = ("pending", "ready", "failed")
_FEEDBACK_KEY = "draft_feedback:{}"

Complete = Callable[..., Awaitable[str]]


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.strip()[:MAX_COMPARE], b.strip()[:MAX_COMPARE], autojunk=False).ratio()


# --- the send hint ------------------------------------------------------------------------------------------------
# The send route says "this text came from draft N". The own message shows up later through the poller, so the hint
# waits here, per chat, until the next own message in that chat takes it (or it expires).

_hints: dict[str, tuple[int, float]] = {}
_hint_lock = threading.Lock()


def set_hint(chat_jid: str, draft_id: int) -> None:
    with _hint_lock:
        _hints[chat_jid] = (draft_id, time.time())


def clear_hint(chat_jid: str, draft_id: int) -> None:
    with _hint_lock:
        if chat_jid in _hints and _hints[chat_jid][0] == draft_id:
            del _hints[chat_jid]


def _peek_hint(chat_jid: str) -> int | None:
    """The chat's hinted draft; an expired hint is dropped. It stays until it is used (see `_use_hint`)."""
    with _hint_lock:
        found = _hints.get(chat_jid)
        if found and time.time() - found[1] > HINT_TTL:
            del _hints[chat_jid]
            return None
    return found[0] if found else None


def _use_hint(chat_jid: str, draft_id: int) -> None:
    clear_hint(chat_jid, draft_id)


# --- outcomes -----------------------------------------------------------------------------------------------------


def _alternatives(raw: str | None) -> list[str]:
    try:
        return [a for a in json.loads(raw or "[]") if isinstance(a, str) and a.strip()]
    except ValueError:
        return []


def _in_window(created_at: int, ready_at: int | None, sent_at: float) -> bool:
    base = ready_at if ready_at is not None else created_at
    return created_at - _SLACK <= sent_at <= base + WINDOW_SECONDS


def _pick(appconn: sqlite3.Connection, chat_jid: str, hint: int | None, sent_at: float) -> tuple | None:
    """(id, status, alternatives): the hinted draft if it qualifies, else the chat's latest ready, else pending."""
    cols = "id, status, alternatives, created_at, ready_at"
    if hint is not None:
        row = appconn.execute(
            f"SELECT {cols} FROM drafts WHERE id=? AND chat_jid=? AND status IN ('pending','ready','superseded',"
            "'dismissed','expired') AND NOT EXISTS (SELECT 1 FROM draft_outcomes o WHERE o.draft_id=drafts.id)",
            (hint, chat_jid),
        ).fetchone()
        if row and (row[4] is not None or row[1] == "pending") and _in_window(row[3], row[4], sent_at):
            return row
    for status in ("ready", "pending"):
        row = appconn.execute(
            f"SELECT {cols} FROM drafts WHERE chat_jid=? AND status=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (chat_jid, status),
        ).fetchone()
        if row and _in_window(row[3], row[4], sent_at):
            return row
    return None


def record_own_message(
    appconn: sqlite3.Connection, chat_jid: str, sent_id: str, sent_text: str, sent_at: float
) -> str | None:
    """Link an own message to the chat's draft and store the outcome; returns used / edited / ignored, or None when
    nothing was linked. Blocking: call from a thread. A draft that was still pending is superseded, a ready one is
    done; other live drafts of the chat go away with it. `ignored` teaches nothing and is not counted."""
    text = sent_text.strip()
    if not text:
        return None
    with appdb.db_lock:
        if appconn.execute(
            "SELECT 1 FROM draft_outcomes WHERE chat_jid=? AND sent_id=?", (chat_jid, sent_id)
        ).fetchone():
            return None  # the same message delivered twice
        hint = _peek_hint(chat_jid)
        draft = _pick(appconn, chat_jid, hint, sent_at)
        if draft is None:
            return None
        draft_id, status = draft[0], draft[1]
        alternatives = _alternatives(draft[2])
    best, ratio = None, 0.0  # the comparison is CPU work: outside the lock
    for alt in alternatives:
        score = similarity(alt, text)
        if best is None or score > ratio:
            best, ratio = alt, score
    kind = "used" if ratio >= USED else "edited" if ratio >= EDITED else "ignored"
    if best is None:
        kind = "ignored"
    with appdb.db_lock:
        thumb, note = _stored_feedback(appconn, draft_id)
        appconn.execute("BEGIN")
        try:
            appconn.execute(
                "INSERT OR IGNORE INTO draft_outcomes (draft_id, chat_jid, sent_id, sent_text, best_alternative,"
                " ratio, kind, thumb, feedback, at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (draft_id, chat_jid, sent_id, text, best, ratio, kind, thumb, note, int(sent_at)),
            )
            appconn.execute("DELETE FROM meta WHERE key=?", (_FEEDBACK_KEY.format(draft_id),))
            if status == "ready":
                appconn.execute("UPDATE drafts SET status='done' WHERE id=?", (draft_id,))
            appconn.execute(  # a pending draft, and whatever else the chat still shows, is overtaken by the reply
                "UPDATE drafts SET status='superseded' WHERE chat_jid=? AND status IN ('pending','ready','failed')",
                (chat_jid,),
            )
            if kind != "ignored":
                appconn.execute("INSERT OR IGNORE INTO chat_ai (chat_jid) VALUES (?)", (chat_jid,))
                appconn.execute(
                    "UPDATE chat_ai SET outcomes_since_distill = outcomes_since_distill + 1 WHERE chat_jid=?",
                    (chat_jid,),
                )
            appconn.execute("COMMIT")
        except BaseException:
            appconn.execute("ROLLBACK")
            raise
        if hint == draft_id:
            _use_hint(chat_jid, draft_id)
    return kind


def supersede_live(appconn: sqlite3.Connection, chat_jid: str, sent_at: float) -> bool:
    """An own message makes the chat's pending, ready or failed drafts stale even when it links to none of them.
    A message older than the draft (a re-delivered old row) changes nothing. True when a row changed."""
    with appdb.db_lock:
        cur = appconn.execute(
            "UPDATE drafts SET status='superseded' WHERE chat_jid=? AND status IN ('pending','ready','failed')"
            " AND created_at <= ?",
            (chat_jid, int(sent_at) + _SLACK),
        )
    return cur.rowcount > 0


def prune_feedback(appconn: sqlite3.Connection, now: float) -> None:
    """Drop stored feedback of drafts that are over a day old and never got an outcome."""
    with appdb.db_lock:
        appconn.execute(
            "DELETE FROM meta WHERE key LIKE 'draft_feedback:%' AND CAST(substr(key, 16) AS INTEGER) NOT IN"
            " (SELECT id FROM drafts WHERE created_at >= ?)",
            (int(now) - 24 * 3600,),
        )


def prune_old_drafts(appconn: sqlite3.Connection, now: float) -> None:
    """Drop drafts (and their outcomes) older than DRAFT_KEEP_DAYS: they hold copies of replies and sent texts."""
    cutoff = int(now) - DRAFT_KEEP_DAYS * 24 * 3600
    with appdb.db_lock:
        appconn.execute("BEGIN")
        try:
            appconn.execute(
                "DELETE FROM draft_outcomes WHERE draft_id IN (SELECT id FROM drafts WHERE created_at < ?)", (cutoff,)
            )
            appconn.execute("DELETE FROM drafts WHERE created_at < ?", (cutoff,))
            appconn.execute("COMMIT")
        except BaseException:
            appconn.execute("ROLLBACK")
            raise


def pending_outcomes(appconn: sqlite3.Connection, chat_jid: str) -> int:
    with appdb.db_lock:
        row = appconn.execute("SELECT outcomes_since_distill FROM chat_ai WHERE chat_jid=?", (chat_jid,)).fetchone()
    return int(row[0]) if row else 0


def chats_with_new_outcomes(appconn: sqlite3.Connection) -> list[str]:
    with appdb.db_lock:
        rows = appconn.execute("SELECT chat_jid FROM chat_ai WHERE outcomes_since_distill > 0").fetchall()
    return [r[0] for r in rows]


# --- feedback -----------------------------------------------------------------------------------------------------


def _stored_feedback(appconn: sqlite3.Connection, draft_id: int) -> tuple[int | None, str | None]:
    row = appconn.execute("SELECT value FROM meta WHERE key=?", (_FEEDBACK_KEY.format(draft_id),)).fetchone()
    if not row:
        return None, None
    try:
        data = json.loads(row[0])
    except ValueError:
        return None, None
    return data.get("thumb"), data.get("note")


def set_feedback(appconn: sqlite3.Connection, draft_id: int, thumb: int | None, note: str | None) -> None:
    """The thumb and note for a draft: on its outcome when it has one, else kept until the outcome is written."""
    with appdb.db_lock:
        cur = appconn.execute("UPDATE draft_outcomes SET thumb=?, feedback=? WHERE draft_id=?", (thumb, note, draft_id))
        if cur.rowcount == 0:
            appconn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (_FEEDBACK_KEY.format(draft_id), json.dumps({"thumb": thumb, "note": note})),
            )


# --- distillation -------------------------------------------------------------------------------------------------


def parse_rules(raw: str) -> str | None:
    """At most 12 '- ' lines, each cleaned like prompt text (no fences, brackets or control characters), 2000 chars."""
    kept: list[str] = []
    total = 0
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("- ", "* ", "• ")):
            continue
        body = prompts.clean_line(stripped[2:], MAX_RULE_LINE).strip()
        if not body:
            continue
        entry = f"- {body}"
        if len(kept) >= MAX_RULE_LINES or total + len(entry) + (1 if kept else 0) > ai_config.MAX_RULES:
            break
        kept.append(entry)
        total += len(entry) + (1 if len(kept) > 1 else 0)
    return "\n".join(kept) or None


def _gather(settings: Settings, appconn: sqlite3.Connection, chat_jid: str):  # noqa: ANN202
    with appdb.db_lock:
        chat = ai_config.chat_ai(appconn, chat_jid)
        cfg = ai_config.effective_backend(appconn, chat_jid)
        secret = ai_config.backend_secret(appconn, settings, cfg.id) if cfg else None
        counted = appconn.execute("SELECT outcomes_since_distill FROM chat_ai WHERE chat_jid=?", (chat_jid,)).fetchone()
        taken = int(counted[0]) if counted else 0
        rows = appconn.execute(
            "SELECT best_alternative, sent_text FROM draft_outcomes WHERE chat_jid=? AND kind IN ('used','edited')"
            " AND COALESCE(best_alternative, '') <> '' ORDER BY at DESC, draft_id DESC LIMIT ?",
            (chat_jid, PAIRS),
        ).fetchall()
    pin = policy.check(chat, cfg)  # local-only and no-backend refusals: before any chat content leaves
    pairs = [(r[0], r[1]) for r in reversed(rows)]
    return chat, cfg, secret, taken, pairs, pin


def _store(appconn: sqlite3.Connection, chat_jid: str, old: str | None, new: str, taken: int) -> bool:
    with appdb.db_lock:
        cur = appconn.execute(  # not if the user edited the rules while the model was thinking
            "UPDATE chat_ai SET rules=?, outcomes_since_distill=MAX(0, outcomes_since_distill - ?)"
            " WHERE chat_jid=? AND rules IS ?",
            (new, taken, chat_jid, old),
        )
    return cur.rowcount > 0


async def distill(
    settings: Settings, appconn: sqlite3.Connection, chat_jid: str, complete: Complete = backends.complete
) -> str | None:
    """Rewrite the chat's rules from its recent outcomes (the current rules, user edits included, are the base).
    Returns the new rules, or None when nothing changed: refused by the policy, nothing to learn from, backend
    failure or unusable output. On failure the old rules and the counter stay; only the next trigger tries again."""
    try:
        chat, cfg, secret, taken, pairs, pin = await asyncio.to_thread(_gather, settings, appconn, chat_jid)
        if cfg is None or not pairs:
            return None
        extra = {"pin": pin} if pin else {}
        raw = await complete(cfg, secret, prompts.build_distill_prompt(chat.rules, pairs), settings, **extra)
        rules = parse_rules(raw)
        if rules is None:
            return None
        stored = await asyncio.to_thread(_store, appconn, chat_jid, chat.rules, rules, taken)
    except asyncio.CancelledError:
        raise
    except policy.Refused as exc:
        logger.info("distill refused (%s)", exc.reason)
        return None
    except Exception as exc:  # the type only: messages may quote chat text
        logger.error("distill failed (%s)", type(exc).__name__)
        return None
    return rules if stored else None

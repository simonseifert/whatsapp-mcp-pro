"""History coverage: how much of the user's own writing is stored per chat, so drafts have enough to learn from."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from . import store

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


def totals(ro: sqlite3.Connection) -> dict:
    count, chats, oldest = ro.execute(
        "SELECT COUNT(*), COUNT(DISTINCT chat_jid), MIN(julianday(timestamp)) FROM messages"
    ).fetchone()
    return {"messages": count, "chats": chats, "oldest": _iso(oldest)}


def _stats(ro: sqlite3.Connection, forms: list[str], own: str) -> list[tuple[str, int, int, float | None]]:
    """Per stored form: message count, own text count and the earliest timestamp, in one pass over the chat index."""
    return ro.execute(
        f"SELECT chat_jid, COUNT(*), SUM(CASE WHEN {own} THEN 1 ELSE 0 END), MIN(julianday(timestamp))"
        f" FROM messages WHERE chat_jid IN ({store._marks(forms)}) GROUP BY chat_jid",
        forms,
    ).fetchall()


def _empty_chats(ro: sqlite3.Connection, known: set[str]) -> list[tuple[str, str, str, str]]:
    """Real chats the list leaves out because nothing is stored yet: (jid, name, kind, last_active)."""
    out = []
    for conv in store._conversations(ro):
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
    """The `limit` most recently active chats (not archived), most recent first, with their own-text coverage."""
    listed, _ = store.list_chats_with_count(ro, appconn, archived=None, limit=100_000)
    entries: list[tuple[str, str, str, str]] = [
        (c.jid, c.name, c.kind, c.last_message.timestamp_iso)
        for c in listed
        if not c.archived and c.kind != "newsletter" and not _skipped(c.jid)
    ]
    entries += _empty_chats(ro, {c.jid for c in listed})
    entries.sort(key=lambda e: store._sort_key(e[3]), reverse=True)
    own = store.own_text_filter(deleted_col="deleted_at" in store._columns(ro, "messages"))
    out = []
    for jid, name, kind, last_active in entries[:limit]:
        forms = store._forms_of(ro, jid)
        per_form = _stats(ro, forms, own)
        own_texts = sum(r[2] or 0 for r in per_form)
        earliest = [r[3] for r in per_form if r[3] is not None]
        best = max(per_form, key=lambda r: r[1], default=None)
        out.append(
            ChatCoverage(
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
        )
    return out

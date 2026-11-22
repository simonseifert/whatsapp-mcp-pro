"""Unread counts for the chat list and the chat view: one definition of "unread".

Incoming, non-system, non-deleted messages after the read marker. The marker is the later of the phone's
(bridge chat_state.last_read_at) and the app's own (app.db read_state.read_at), so a chat read on either side is
read on both. With no marker at all, the count history sync reported (chat_state.history_unread_count), else 0.
`marked_unread` (the phone's manual "mark as unread" dot) is not part of the count; the chat list passes it through.
"""

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import _Conversation, _State


# Poll votes are stored by the bridge as their own messages; the chat view never shows them, so they must not count
# as unread, be sent a receipt or become a chat's preview either. One definition, used everywhere.
NOT_POLL_VOTE = "(content IS NULL OR content NOT LIKE '[Poll Vote:%')"


def _placeholders(items: Sequence[object]) -> str:
    return ",".join("?" * len(items))


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None or b is None:
        return a or b
    return max(a, b)


class UnreadCounter:
    def __init__(self, conn: sqlite3.Connection, appconn: sqlite3.Connection) -> None:
        self.conn = conn
        # The app's read markers, loaded once per pass (a handful of rows).
        self.app_read: dict[str, int] = {}
        self.app_opened: dict[str, int] = {}
        try:
            for jid, at, opened in appconn.execute("SELECT chat_jid, read_at, opened_at FROM read_state"):
                if at is not None:
                    self.app_read[jid] = at
                if opened is not None:
                    self.app_opened[jid] = opened
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):  # only an app db without the table (a bare connection) is empty
                raise
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        self._not_deleted = "AND deleted_at IS NULL" if "deleted_at" in columns else ""

    def marker(self, forms: Sequence[str], last_read_at: str | None) -> datetime | None:
        from .store import parse_ts

        app = [self.app_read[f] for f in forms if f in self.app_read]
        return _later(parse_ts(last_read_at), datetime.fromtimestamp(max(app), UTC) if app else None)

    def dot(self, forms: Sequence[str], marked_unread: bool, changed_at: str | None) -> bool:
        """The phone's manual "mark as unread" dot, hidden once the app opened the chat after it was set."""
        if not marked_unread:
            return False
        opened = [self.app_opened[f] for f in forms if f in self.app_opened]
        if not opened:
            return True
        from .store import parse_ts

        changed = parse_ts(changed_at)
        return changed is not None and changed > datetime.fromtimestamp(max(opened), UTC)

    def count(self, forms: Sequence[str], last_read_at: str | None, history_unread: int | None) -> int:
        marker = self.marker(forms, last_read_at)
        if marker is None:
            return max(history_unread or 0, 0)
        bound = marker.isoformat(sep=" ")
        # The raw-string bound lets the (chat_jid, timestamp) index narrow the scan; the datetime() compare is
        # exact across the mixed UTC offsets the bridge stores.
        row = self.conn.execute(
            f"""
            SELECT COUNT(*) FROM messages
            WHERE chat_jid IN ({_placeholders(forms)})
              AND COALESCE(is_from_me, 0) = 0 AND COALESCE(is_system_message, 0) = 0 {self._not_deleted}
              AND {NOT_POLL_VOTE}
              AND timestamp >= date(?, '-1 day') AND datetime(timestamp) > datetime(?)
            """,
            (*forms, bound, bound),
        ).fetchone()
        return int(row[0])


def conversation(conn: sqlite3.Connection, chat_jid: str) -> "tuple[_Conversation, _State] | None":
    from . import store

    for conv in store._conversations(conn):
        if chat_jid == conv.jid or chat_jid in conv.forms:
            return conv, store._states(conn, [conv]).get(conv.jid, store._State())
    return None


def read_marker(conn: sqlite3.Connection, appconn: sqlite3.Connection, chat_jid: str) -> datetime | None:
    """The later of the phone's and the app's read time for the chat (any of its JID forms); None when neither."""
    found = conversation(conn, chat_jid)
    if found is None:
        return None
    conv, state = found
    return UnreadCounter(conn, appconn).marker([conv.jid, *conv.forms], state.last_read_at)


def count(conn: sqlite3.Connection, appconn: sqlite3.Connection, chat_jid: str) -> int:
    """Unread incoming messages in the chat; 0 for an unknown chat."""
    found = conversation(conn, chat_jid)
    if found is None:
        return 0
    conv, state = found
    return UnreadCounter(conn, appconn).count([conv.jid, *conv.forms], state.last_read_at, state.history_unread)

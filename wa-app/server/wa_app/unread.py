"""Unread counts for the chat list.

Task 2: incoming, non-system, non-deleted messages after the bridge's chat_state.last_read_at; with no
marker, the count history sync reported (chat_state.history_unread_count), else 0. Task 6 adds the app's own
read marker (app.db read_state) here, so the list and the chat view keep one definition of "unread".
"""

import sqlite3
from collections.abc import Sequence


def _placeholders(items: Sequence[object]) -> str:
    return ",".join("?" * len(items))


class UnreadCounter:
    def __init__(self, conn: sqlite3.Connection, appconn: sqlite3.Connection) -> None:
        self.conn = conn
        self.appconn = appconn  # read by Task 6 (app.db read_state)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        self._not_deleted = "AND deleted_at IS NULL" if "deleted_at" in columns else ""

    def count(self, forms: Sequence[str], last_read_at: str | None, history_unread: int | None) -> int:
        if not last_read_at:
            return max(history_unread or 0, 0)
        # The raw-string bound lets the (chat_jid, timestamp) index narrow the scan; the datetime() compare is
        # exact across the mixed UTC offsets the bridge stores.
        row = self.conn.execute(
            f"""
            SELECT COUNT(*) FROM messages
            WHERE chat_jid IN ({_placeholders(forms)})
              AND COALESCE(is_from_me, 0) = 0 AND COALESCE(is_system_message, 0) = 0 {self._not_deleted}
              AND timestamp >= date(?, '-1 day') AND datetime(timestamp) > datetime(?)
            """,
            (*forms, last_read_at, last_read_at),
        ).fetchone()
        return int(row[0])

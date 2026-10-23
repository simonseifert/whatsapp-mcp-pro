"""The brute-force recall search as it was before the cached matrix.

Kept (minus the query embedding and the index_status call) as the oracle the
cached implementation is checked against, and as the "before" side of
scripts/bench_recall.py. Not imported by the server.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import numpy as np

from lib.naming import sender_forms
from lib.utils import canonical_chat_jid, to_sql_utc


def reference_search(
    conn: sqlite3.Connection,
    model: str,
    qvec: np.ndarray,
    chat_jid: str | None = None,
    sender: str | None = None,
    after: str | None = None,
    before: str | None = None,
    is_from_me: bool | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Fetch every embedding that passes the filters, np.stack, full argsort."""
    sql_parts = [
        """
        SELECT m.id, m.chat_jid, m.sender, m.content, m.timestamp, m.is_from_me,
               c.name AS chat_name, e.embedding
        FROM message_embeddings e
        JOIN messages m
            ON m.id = e.message_id AND m.chat_jid = e.chat_jid
        LEFT JOIN chats c ON c.jid = m.chat_jid
        WHERE e.model = ?
        """
    ]
    params: list[Any] = [model]
    if chat_jid:
        jids = sorted({chat_jid, canonical_chat_jid(conn, chat_jid)})
        sql_parts.append(f" AND m.chat_jid IN ({','.join('?' * len(jids))})")
        params.extend(jids)
    if sender:
        forms = sender_forms(conn, sender)
        sql_parts.append(f" AND m.sender IN ({','.join('?' * len(forms))})")
        params.extend(forms)
    after_utc = to_sql_utc(after)
    before_utc = to_sql_utc(before)
    if after_utc:
        sql_parts.append(" AND datetime(m.timestamp) >= ?")
        params.append(after_utc)
    if before_utc:
        sql_parts.append(" AND datetime(m.timestamp) <= ?")
        params.append(before_utc)
    if is_from_me is not None:
        sql_parts.append(" AND m.is_from_me = ?")
        params.append(1 if is_from_me else 0)

    rows = conn.execute("".join(sql_parts), params).fetchall()
    if not rows:
        return []
    embeddings = np.stack([np.frombuffer(r[7], dtype=np.float32) for r in rows])
    sims = embeddings @ qvec
    top = np.argsort(-sims)[:limit]
    out = []
    for i in top:
        r = rows[int(i)]
        out.append(
            {
                "message_id": r[0],
                "chat_jid": r[1],
                "chat_name": r[6],
                "sender": r[2],
                "is_from_me": bool(r[5]),
                "timestamp": r[4],
                "content": r[3],
                "similarity": round(float(sims[int(i)]), 4),
            }
        )
    return out

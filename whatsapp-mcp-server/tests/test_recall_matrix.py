"""The cached embedding matrix must rank exactly like the old fetch-everything search.

recall() used to SELECT every embedding blob and np.stack them per call. It now
keeps the vectors in memory and only asks SQL which rows match. These tests run
both over one synthetic store, with every filter recall has, and again after
each kind of write the store really sees: the backfill appending, the bridge's
LID merge rewriting chat_jid in place, deletes, a reused rowid, VACUUM, a model
change, and all of that concurrently with readers.

All data is synthetic: fictional numbers in the 49155000 test range and random
vectors.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
import threading
import time
from typing import Any

import numpy as np
import pytest

from lib import recall as recall_mod
from tests.recall_reference import reference_search

MODEL = "test/current-model"
OTHER_MODEL = "test/other-model"
OTHER_DIM = 128

PHONE = "4915500000201@s.whatsapp.net"
LID = "990000000000201@lid"
OTHER_PERSON = "4915500000202@s.whatsapp.net"
GROUP = "120363000000000009@g.us"
SIDE_CHAT = "4915500000203@s.whatsapp.net"

# Every way messages.sender holds the same person, plus two that are not them.
SENDER_FORMS = [PHONE, PHONE.split("@")[0], LID, LID.split("@")[0], OTHER_PERSON, "me"]
CHATS = [PHONE, LID, GROUP, SIDE_CHAT]
OFFSETS = ["+00:00", "+02:00", "-05:00", "+05:30"]


def _unit(rng: np.random.Generator, dim: int) -> np.ndarray:
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _qvec(text: str, dim: int) -> np.ndarray:
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "little")
    return _unit(np.random.default_rng(seed), dim)


def _build(path: str, n_messages: int = 600, seed: int = 7) -> None:
    rng = np.random.default_rng(seed)
    pick = random.Random(seed)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, merged_into TEXT);
        CREATE TABLE messages (
            id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,
            is_from_me BOOLEAN, PRIMARY KEY (id, chat_jid)
        );
        CREATE TABLE identities (jid TEXT PRIMARY KEY, lid TEXT, phone_jid TEXT, phone TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO chats VALUES (?, ?, ?)",
        [(PHONE, "Chat A", None), (LID, "Chat A (lid)", PHONE), (GROUP, "Group B", None), (SIDE_CHAT, None, None)],
    )
    conn.execute("INSERT INTO identities VALUES (?, ?, ?, ?)", (PHONE, LID, PHONE, PHONE.split("@")[0]))
    recall_mod._ensure_table(conn)

    msgs = []
    for i in range(n_messages):
        day = pick.randrange(1, 29)
        ts = f"2026-09-{day:02d} {pick.randrange(24):02d}:{pick.randrange(60):02d}:00{pick.choice(OFFSETS)}"
        msgs.append((f"m{i:05d}", pick.choice(CHATS), pick.choice(SENDER_FORMS), f"text {i}", ts, pick.random() < 0.3))
    conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)", msgs)

    # Shuffled so rowid order says nothing about message order. Some messages
    # stay unembedded, some carry another model's narrower vector, and a few
    # embeddings point at messages that no longer exist.
    order = list(range(n_messages))
    pick.shuffle(order)
    emb = []
    for i in order:
        mid, chat = msgs[i][0], msgs[i][1]
        roll = pick.random()
        if roll < 0.1:
            continue
        if roll < 0.2:
            emb.append((mid, chat, _unit(rng, OTHER_DIM).tobytes(), OTHER_MODEL, 1000 + i // 50))
        else:
            emb.append((mid, chat, _unit(rng, recall_mod.EMBED_DIM).tobytes(), MODEL, 1000 + i // 50))
    for j in range(5):
        emb.append((f"gone{j}", GROUP, _unit(rng, recall_mod.EMBED_DIM).tobytes(), MODEL, 999))
    conn.executemany("INSERT INTO message_embeddings VALUES (?, ?, ?, ?, ?)", emb)
    conn.commit()
    conn.close()


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = str(tmp_path / "messages.db")
    _build(path)
    monkeypatch.setattr(recall_mod, "MESSAGES_DB_PATH", path)
    monkeypatch.setattr(recall_mod, "MODEL_NAME", MODEL)
    monkeypatch.setattr(recall_mod, "_ensure_indexer_running", lambda: None)
    monkeypatch.setattr(recall_mod, "_ensure_unloader_running", lambda: None)
    monkeypatch.setattr(recall_mod, "_embed", lambda texts: np.stack([_qvec(t, _dim()) for t in texts]))
    recall_mod._drop_matrix()
    yield path
    recall_mod._drop_matrix()


def _dim() -> int:
    return OTHER_DIM if recall_mod.MODEL_NAME == OTHER_MODEL else recall_mod.EMBED_DIM


FILTERS: list[dict[str, Any]] = [
    {},
    {"limit": 1},
    {"limit": 200},
    {"chat_jid": PHONE},
    {"chat_jid": LID},  # merged: must also search its phone twin
    {"chat_jid": GROUP, "limit": 50},
    {"chat_jid": "4915500009999@s.whatsapp.net"},  # unknown chat: no rows
    {"sender": PHONE, "limit": 100},
    {"sender": LID, "limit": 100},
    {"sender": PHONE.split("@")[0], "limit": 100},
    {"sender": OTHER_PERSON},
    {"after": "2026-09-10T00:00:00Z"},
    {"before": "2026-09-10T00:00:00+02:00"},
    {"after": "2026-09-05T12:00:00-03:00", "before": "2026-09-20T00:00:00Z", "limit": 30},
    {"after": "2026-09-14"},  # naive: local time, converted to UTC
    {"is_from_me": True},
    {"is_from_me": False, "limit": 40},
    {"chat_jid": LID, "sender": LID, "after": "2026-09-03T00:00:00Z", "is_from_me": False, "limit": 25},
]
QUERIES = ["first query", "second query", "a third one"]


def _assert_matches_reference(path: str, query: str, **kw: Any) -> list[dict[str, Any]]:
    got = recall_mod.recall(query, **kw)
    assert "error" not in got, got
    conn = sqlite3.connect(path)
    try:
        want = reference_search(
            conn, recall_mod.MODEL_NAME, _qvec(query, _dim()), limit=min(kw.pop("limit", 10), 200), **kw
        )
    finally:
        conn.close()
    # Random float vectors: an exact tie has probability ~0, so the order must
    # match outright rather than up to ties.
    assert [r["message_id"] for r in got["results"]] == [r["message_id"] for r in want], (query, kw)
    assert got["results"] == want, (query, kw)
    return got["results"]


@pytest.mark.parametrize("kw", FILTERS, ids=[str(f) for f in FILTERS])
def test_matches_reference_for_every_filter(store, kw):
    for q in QUERIES:
        _assert_matches_reference(store, q, **dict(kw))


def test_filters_actually_filter(store):
    """Guard against the comparison passing vacuously on empty results."""
    assert len(_assert_matches_reference(store, "first query", limit=200)) == 200
    merged = _assert_matches_reference(store, "first query", chat_jid=LID, limit=200)
    assert {r["chat_jid"] for r in merged} == {PHONE, LID}
    by_sender = _assert_matches_reference(store, "first query", sender=LID, limit=200)
    assert {r["sender"] for r in by_sender} == set(SENDER_FORMS[:4])


def _matrix() -> recall_mod._EmbeddingMatrix:
    m = recall_mod._matrix
    assert m is not None
    return m


def _check_all(path: str) -> None:
    for kw in FILTERS[:6] + FILTERS[-1:]:
        _assert_matches_reference(path, "first query", **dict(kw))


def test_stays_correct_across_store_writes(store):
    rng = np.random.default_rng(1)
    _check_all(store)
    assert _matrix().rebuilds == 1

    # Backfill appends: picked up incrementally, no reload.
    conn = sqlite3.connect(store)
    missing = conn.execute(
        """SELECT m.id, m.chat_jid FROM messages m LEFT JOIN message_embeddings e
           ON e.message_id = m.id AND e.chat_jid = m.chat_jid WHERE e.message_id IS NULL"""
    ).fetchall()
    assert missing
    conn.executemany(
        "INSERT INTO message_embeddings VALUES (?, ?, ?, ?, ?)",
        [(mid, chat, _unit(rng, recall_mod.EMBED_DIM).tobytes(), MODEL, 5000) for mid, chat in missing],
    )
    conn.commit()
    _check_all(store)
    assert _matrix().rebuilds == 1

    # The bridge's LID merge: chat_jid rewritten in place, rowid and vector kept.
    conn.execute("UPDATE messages SET chat_jid = ? WHERE chat_jid = ?", (PHONE, LID))
    conn.execute("UPDATE OR IGNORE message_embeddings SET chat_jid = ? WHERE chat_jid = ?", (PHONE, LID))
    conn.execute("DELETE FROM message_embeddings WHERE chat_jid = ?", (LID,))
    conn.commit()
    _check_all(store)
    assert _matrix().rebuilds == 1

    # Deleted messages and embeddings simply stop being candidates.
    conn.execute("DELETE FROM messages WHERE rowid % 7 = 0")
    conn.execute("DELETE FROM message_embeddings WHERE rowid % 11 = 0")
    conn.commit()
    _check_all(store)
    assert _matrix().rebuilds == 1

    # Rowid reuse: delete the newest embedding and the next insert gets its
    # rowid back. Same embedded_at, different message: only message_id tells.
    top_rowid, top_stamp = conn.execute(
        "SELECT rowid, embedded_at FROM message_embeddings ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    conn.execute("DELETE FROM message_embeddings WHERE rowid = ?", (top_rowid,))
    # A row that is still a candidate: a stale vector nobody selects is harmless.
    victim = conn.execute(
        """SELECT e.message_id, e.chat_jid FROM message_embeddings e JOIN messages m
           ON m.id = e.message_id AND m.chat_jid = e.chat_jid WHERE e.model = ? ORDER BY e.rowid LIMIT 1""",
        (MODEL,),
    ).fetchone()
    conn.execute("DELETE FROM message_embeddings WHERE message_id = ? AND chat_jid = ?", victim)
    conn.execute(
        "INSERT INTO message_embeddings VALUES (?, ?, ?, ?, ?)",
        (*victim, _unit(rng, recall_mod.EMBED_DIM).tobytes(), MODEL, top_stamp),
    )
    conn.commit()
    assert conn.execute("SELECT MAX(rowid) FROM message_embeddings").fetchone()[0] == top_rowid
    _check_all(store)
    assert _matrix().rebuilds == 2

    # An in-place UPDATE that moves a row into the current model below the
    # highest rowid seen: not an append, so it must be caught as a mismatch.
    row = conn.execute(
        """SELECT e.rowid FROM message_embeddings e JOIN messages m
           ON m.id = e.message_id AND m.chat_jid = e.chat_jid WHERE e.model = ? LIMIT 1""",
        (OTHER_MODEL,),
    ).fetchone()
    conn.execute(
        "UPDATE message_embeddings SET model = ?, embedding = ? WHERE rowid = ?",
        (MODEL, _unit(rng, recall_mod.EMBED_DIM).tobytes(), row[0]),
    )
    conn.commit()
    _check_all(store)
    assert _matrix().rebuilds == 3

    # VACUUM may renumber every rowid of a table without INTEGER PRIMARY KEY.
    conn.execute("VACUUM")
    conn.close()
    _check_all(store)


def test_model_change_swaps_the_matrix(store, monkeypatch):
    _check_all(store)
    before = _matrix()
    monkeypatch.setattr(recall_mod, "MODEL_NAME", OTHER_MODEL)
    _check_all(store)
    assert _matrix() is not before
    assert _matrix().rows < before.rows
    monkeypatch.setattr(recall_mod, "MODEL_NAME", MODEL)
    _check_all(store)


def test_no_matches_keeps_the_warning(store):
    result = recall_mod.recall("first query", chat_jid="4915500009999@s.whatsapp.net")
    assert result["results"] == []
    assert "warning" in result


def test_concurrent_readers_and_backfill(store):
    """serve_http.py runs tool calls on worker threads while the backfill writes."""
    conn = sqlite3.connect(store)
    conn.execute("PRAGMA journal_mode=WAL")  # as the live store
    conn.close()

    errors: list[BaseException] = []
    stop = threading.Event()

    def writer() -> None:
        rng = np.random.default_rng(3)
        w = sqlite3.connect(store, timeout=10, isolation_level=None)
        try:
            for batch in range(30):
                rows = []
                for j in range(20):
                    mid = f"new{batch:03d}-{j:02d}"
                    w.execute(
                        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
                        (mid, GROUP, PHONE, "new", "2026-09-29 10:00:00+00:00", 0),
                    )
                    rows.append((mid, GROUP, _unit(rng, recall_mod.EMBED_DIM).tobytes(), MODEL, 9000 + batch))
                w.executemany("INSERT OR REPLACE INTO message_embeddings VALUES (?, ?, ?, ?, ?)", rows)
                if batch % 7 == 3:
                    # Force a reused rowid mid-flight.
                    w.execute(
                        "DELETE FROM message_embeddings WHERE rowid = (SELECT MAX(rowid) FROM message_embeddings)"
                    )
                time.sleep(0.002)
        except BaseException as exc:  # noqa: BLE001 - surfaced through errors
            errors.append(exc)
        finally:
            w.close()
            stop.set()

    def reader(seed: int) -> None:
        pick = random.Random(seed)
        try:
            while not stop.is_set():
                kw = dict(pick.choice(FILTERS))
                res = recall_mod.recall(pick.choice(QUERIES), **kw)
                assert "error" not in res, res
                sims = [r["similarity"] for r in res["results"]]
                assert sims == sorted(sims, reverse=True)
        except BaseException as exc:  # noqa: BLE001 - surfaced through errors
            errors.append(exc)

    threads = [threading.Thread(target=reader, args=(i,)) for i in range(6)]
    threads.append(threading.Thread(target=writer))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors[:3]
    _check_all(store)

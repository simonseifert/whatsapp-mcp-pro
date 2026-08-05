"""Tests for lib.recall.

Recall had no tests, which is how the model-scoping bug survived: with one model
in the table every query looked correct, and the failure only appears the day
MODEL_NAME changes. Most of what follows pins that behaviour specifically —
these are regression tests for a bug that was invisible, not coverage theatre.

The embedding model is stubbed throughout. Loading the real one costs ~470 MB of
downloads and several seconds, and none of the logic under test depends on the
vectors being semantically meaningful — only on which rows are selected and how
they are filtered.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from lib import recall as recall_mod

OLD_MODEL = "sentence-transformers/old-model-v1"
NEW_MODEL = "sentence-transformers/new-model-v2"


@pytest.fixture
def recall_db(temp_messages_db, monkeypatch):
    """Point recall at a temp DB with deterministic, stubbed embeddings.

    Threads are stubbed out too: the indexer and the model unloader both spawn
    background loops that would outlive the test and race the assertions.
    """
    monkeypatch.setattr(recall_mod, "MESSAGES_DB_PATH", temp_messages_db)
    monkeypatch.setattr(recall_mod, "_ensure_indexer_running", lambda: None)
    monkeypatch.setattr(recall_mod, "_ensure_unloader_running", lambda: None)

    def fake_embed(texts: list[str]) -> np.ndarray:
        # Deterministic and unit-norm: the first component encodes the text
        # length so different strings get different, comparable vectors.
        out = np.zeros((len(texts), recall_mod.EMBED_DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            out[i, 0] = 1.0
            out[i, 1] = (len(t) % 7) / 10.0
            out[i] /= np.linalg.norm(out[i])
        return out

    monkeypatch.setattr(recall_mod, "_embed", fake_embed)
    monkeypatch.setattr(recall_mod, "_get_model", lambda: object())
    return temp_messages_db


def _embeddings_in(db: str) -> list[tuple[str, str]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT message_id, model FROM message_embeddings").fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------- limit guard


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_recall_rejects_non_positive_limit(recall_db, bad_limit):
    """np.argsort(...)[:limit] silently misbehaves for these.

    limit=-1 returned every row except the last and limit=0 returned nothing,
    both without complaint — the caller could not tell an empty result from a
    bad argument.
    """
    result = recall_mod.recall("anything", limit=bad_limit)
    assert "error" in result
    assert "positive" in result["error"]


def test_recall_clamps_oversized_limit(recall_db):
    """A semantic search asking for 10k rows is a table scan wearing a hat."""
    result = recall_mod.recall("anything", limit=10_000)
    assert "error" not in result
    assert len(result.get("results", [])) <= recall_mod.MAX_RECALL_LIMIT


# ------------------------------------------------------- model-aware scoping


def test_index_status_ignores_other_models(recall_db, monkeypatch):
    """Embeddings from a previous model must not be counted as done.

    Before the fix index_status counted every row in message_embeddings, so
    after a model change it reported the index complete while nothing usable
    had been embedded.
    """
    monkeypatch.setattr(recall_mod, "MODEL_NAME", OLD_MODEL)
    recall_mod._index_pending()
    assert index_embedded(recall_mod.index_status()) > 0, "sanity: old model indexed something"

    monkeypatch.setattr(recall_mod, "MODEL_NAME", NEW_MODEL)
    status = recall_mod.index_status()
    assert index_embedded(status) == 0, "rows from another model must not count as embedded"
    assert status["model"] == NEW_MODEL
    assert status["remaining"] == status["total"]


def index_embedded(status: dict) -> int:
    assert "error" not in status, status
    return status["embedded"]


def test_backfill_re_embeds_after_model_change(recall_db, monkeypatch):
    """The pending join must treat other-model rows as work still to do.

    It matched on (message_id, chat_jid) alone, so every row looked already
    embedded and the backfill did nothing forever.

    Rows are REPLACED, not accumulated: the table is keyed on
    (message_id, chat_jid) with no model component, so a message carries
    exactly one embedding and re-embedding overwrites it.
    """
    monkeypatch.setattr(recall_mod, "MODEL_NAME", OLD_MODEL)
    recall_mod._index_pending()
    first = _embeddings_in(recall_db)
    assert first and all(m == OLD_MODEL for _, m in first)

    monkeypatch.setattr(recall_mod, "MODEL_NAME", NEW_MODEL)
    recall_mod._index_pending()
    after = _embeddings_in(recall_db)

    assert len(after) == len(first), "one embedding per message; re-embed replaces"
    assert {m for _, m in after} == {NEW_MODEL}, "every row must carry the new model"


def test_recall_survives_a_half_migrated_index(recall_db, monkeypatch):
    """Mixing models is not merely wrong, it raises.

    Two models cannot coexist for one message — the table is keyed without a
    model component — but they absolutely coexist ACROSS messages while a
    re-index is partway through. Those old rows have a different vector width,
    so an unfiltered np.stack over the mixed set throws rather than degrading.
    That half-migrated window is the real exposure.
    """
    monkeypatch.setattr(recall_mod, "MODEL_NAME", OLD_MODEL)
    recall_mod._index_pending()

    # Simulate a re-index that got partway. The stub embeds at EMBED_DIM, so
    # the CURRENT model stays that width and the stale row gets a different
    # one — mirroring reality, where the query vector and the current-model
    # rows always agree and only the leftovers differ.
    conn = sqlite3.connect(recall_db)
    conn.execute(
        "UPDATE message_embeddings SET embedding = ?, model = ? WHERE message_id = ?",
        (np.zeros(128, dtype=np.float32).tobytes(), NEW_MODEL, "msg1"),
    )
    conn.commit()
    conn.close()

    # Still on OLD_MODEL: the stale NEW_MODEL row must be filtered out. Without
    # the model predicate it reaches the matmul and raises
    # "size 384 is different from 128".
    result = recall_mod.recall("hello", limit=5)
    assert "error" not in result, f"stale-width embeddings leaked into the query: {result}"
    assert "msg1" not in [r.get("id") for r in result.get("results", [])], (
        "a row embedded by another model must not be returned"
    )


# ------------------------------------------------------------- happy path


def test_recall_returns_indexed_messages(recall_db, monkeypatch):
    monkeypatch.setattr(recall_mod, "MODEL_NAME", OLD_MODEL)
    recall_mod._index_pending()

    result = recall_mod.recall("hello", limit=5)
    assert "error" not in result
    assert result["results"], "indexed messages should be findable"
    hit = result["results"][0]
    for field in ("content", "chat_jid", "timestamp"):
        assert field in hit, f"result missing {field}: {hit}"


def test_index_status_is_stable_when_nothing_indexed(recall_db, monkeypatch):
    monkeypatch.setattr(recall_mod, "MODEL_NAME", NEW_MODEL)
    status = recall_mod.index_status()
    assert "error" not in status
    assert status["embedded"] == 0
    assert status["total"] >= 1
    assert status["remaining"] == status["total"]


# ---------------------------------------------------- optional extra missing


def test_missing_extra_reports_rather_than_crashes(recall_db, monkeypatch):
    """recall is an opt-in extra. Without sentence-transformers installed it
    must return a readable error, not an ImportError traceback through MCP."""

    def boom(*_a, **_k):
        raise ImportError("No module named 'sentence_transformers'")

    monkeypatch.setattr(recall_mod, "_embed", boom)
    result = recall_mod.recall("anything", limit=5)
    assert "error" in result
    assert "sentence_transformers" in result["error"] or "module" in result["error"].lower()

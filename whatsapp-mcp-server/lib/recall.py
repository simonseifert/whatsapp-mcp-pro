"""Semantic search ("recall") over WhatsApp message history.

Embeds every text message (and any voice-note transcripts already stored as
text) into a vector store kept inside messages.db itself, then ranks matches
to a natural-language query by cosine similarity.

Multilingual: uses paraphrase-multilingual-MiniLM-L12-v2 (50+ languages
including Croatian, German, Italian, French, Spanish, etc.).

Design choices:
- Model is lazy-loaded on first use (avoids slowing MCP server boot).
- NO work at import time: indexing starts on the first recall /
  recall_index_status call. (An import-time kickoff loaded torch + the model
  in every spawned instance — 82 orphans × ~1.7 GB caused the 2026-07-03
  jetsam storm + kernel panic on the 8 GB M1.)
- The model is unloaded after 15 min without embeddings, so the long-running
  shared server doesn't hold ~1 GB of RAM between recall sessions.
- A daemon thread incrementally indexes pending messages in the background.
- Brute-force cosine search at query time, over a float32 matrix kept in
  memory and extended as the backfill adds rows (_EmbeddingMatrix). It costs
  1.5 KB per embedded message (~50 MB at 32k, ~250 MB at 160k) for as long as
  the process lives; filters stay in SQL, so only the vectors are cached.
- Embeddings stored normalized, so dot product == cosine similarity.

Pro tier — added in whatsapp-mcp-extended-pro fork.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from operator import itemgetter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

from .naming import sender_forms
from .utils import MESSAGES_DB_PATH, canonical_chat_jid, logger, to_sql_utc

MAX_RECALL_LIMIT = 200  # a semantic search returning more than this is a scan, not a search
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBED_DIM = 384
INDEX_BATCH_SIZE = 64

# 0 disables unloading. Cycling turned out to cost more than it saved: each
# load/unload ratcheted the CPU heap ~38MB because freed arenas are not returned
# to the OS, so an idle-unload guaranteed a reload and a permanent increment.
# Resident on CPU is a flat ~600-700MB. Set RECALL_MODEL_IDLE_UNLOAD_SECONDS to
# a positive number to restore the old behaviour.
MODEL_IDLE_UNLOAD_SECONDS = int(os.environ.get("RECALL_MODEL_IDLE_UNLOAD_SECONDS", "0"))

_model = None
_model_lock = threading.Lock()
_last_model_use = 0.0
_unloader_thread: threading.Thread | None = None
_unloader_lock = threading.Lock()
_indexer_thread: threading.Thread | None = None
_indexer_lock = threading.Lock()
_indexer_state: dict[str, Any] = {"status": "idle", "indexed": 0, "pending": 0}


# ---------- model + table ----------


def _get_model():
    """Lazy-load the multilingual sentence-transformer model.

    First call downloads ~470 MB of model weights to ~/.cache/huggingface.
    """
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer

            logger.info("[recall] loading embedding model %s", MODEL_NAME)
            t0 = time.time()
            # device="cpu" is deliberate. Left to choose, sentence-transformers
            # picks mps on Apple Silicon and the 470MB model becomes a ~1GB
            # Metal buffer pool that is invisible to RSS and is never returned
            # to the OS by gc — measured 1056MB of "Owned physical footprint
            # (graphics)" still held with the model unloaded. At this volume
            # (~264 embeddings/day, batches of 64) CPU encode is sub-second, so
            # the GPU bought nothing and cost a gigabyte on an 8GB box.
            device = os.environ.get("RECALL_DEVICE", "cpu")
            # Cache first: otherwise every load phones the HF Hub to revalidate
            # (an unauthenticated request per restart, and a stall when offline).
            # Only the very first run on a machine needs the network.
            try:
                _model = SentenceTransformer(MODEL_NAME, device=device, local_files_only=True)
            except OSError:
                logger.info("[recall] %s not cached locally, downloading", MODEL_NAME)
                _model = SentenceTransformer(MODEL_NAME, device=device)
            logger.info("[recall] embedding model loaded in %.1fs", time.time() - t0)
    return _model


def _embed(texts: list[str]) -> np.ndarray:
    global _last_model_use
    model = _get_model()
    _last_model_use = time.monotonic()
    _ensure_unloader_running()
    result = model.encode(
        texts,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
        batch_size=INDEX_BATCH_SIZE,
    )
    _last_model_use = time.monotonic()
    return result


def _unloader_loop() -> None:
    """Free the embedding model after MODEL_IDLE_UNLOAD_SECONDS of no use.

    An in-flight encode keeps its own reference, so dropping _model mid-call
    is safe; the next _embed simply reloads.
    """
    global _model
    while True:
        time.sleep(60)
        with _model_lock:
            if _model is None:
                continue
            idle = time.monotonic() - _last_model_use
            if idle < MODEL_IDLE_UNLOAD_SECONDS:
                continue
            logger.info("[recall] unloading embedding model after %.0fs idle", idle)
            _model = None
        # Whoever opted into unloading wants the memory back; the matrix is the
        # other big resident and is rebuilt (~0.2 s at 32k rows, ~1 s at 160k) by the next call.
        _drop_matrix()
        import gc

        gc.collect()


def _ensure_unloader_running() -> None:
    if MODEL_IDLE_UNLOAD_SECONDS <= 0:
        return  # cycling disabled: see MODEL_IDLE_UNLOAD_SECONDS
    global _unloader_thread
    with _unloader_lock:
        if _unloader_thread is not None and _unloader_thread.is_alive():
            return
        _unloader_thread = threading.Thread(target=_unloader_loop, daemon=True, name="recall-model-unloader")
        _unloader_thread.start()


def start_periodic_indexing(interval_seconds: int = 600) -> None:
    """Opt-in freshness ticker for the long-running shared server.

    Re-runs the incremental indexer on a timer so the first recall call is
    always warm instead of catching up on a backlog. Called explicitly by
    serve_http.py — NEVER at import time (see module docstring). Ticks with
    nothing pending cost one SQLite query and never load the model; the
    idle unloader still frees the model during quiet periods.
    """

    def _loop() -> None:
        while True:
            try:
                _ensure_indexer_running()
            except Exception:
                logger.exception("[recall] periodic index tick failed")
            time.sleep(interval_seconds)

    threading.Thread(target=_loop, daemon=True, name="recall-periodic-indexer").start()


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(MESSAGES_DB_PATH, timeout=10.0, isolation_level=None)


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_embeddings (
            message_id  TEXT NOT NULL,
            chat_jid    TEXT NOT NULL,
            embedding   BLOB NOT NULL,
            model       TEXT NOT NULL,
            embedded_at INTEGER NOT NULL,
            PRIMARY KEY (message_id, chat_jid)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msgemb_chat ON message_embeddings(chat_jid)")
    # Covers recall's per-call candidate query. Without it that query walks the
    # table itself, i.e. every 1.5 KB embedding page, just to read the keys:
    # ~45 ms vs ~22 ms at 32k rows.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_msgemb_model_key ON message_embeddings(model, message_id, chat_jid, embedded_at)"
    )


# ---------- in-memory embedding matrix ----------


class _EmbeddingMatrix:
    """Every embedding of one model in one store, as a float32 matrix keyed by rowid.

    recall() used to SELECT every embedding blob and np.stack them on every call:
    ~70-90 MB transient and ~200 ms at 32k messages, linear in history. This keeps
    the matrix resident (384 float32 = 1.5 KB per message, ~50 MB at 32k, ~250 MB
    at 160k) and extends it with rows above the highest rowid seen, which is
    where the backfill's INSERTs land.

    Only vectors are cached. Which rows match, and their text, chat and sender,
    still come from SQL on every call, so merges (UPDATE chat_jid in place),
    deleted messages and edits are seen without any invalidation. What the
    cache cannot see on its own is a rowid that now holds a different row: SQLite
    hands the max rowid out again after it is deleted (INSERT OR REPLACE of the
    newest row does exactly that), and VACUUM renumbers tables without an
    INTEGER PRIMARY KEY. Each candidate is therefore checked against the
    (message_id, embedded_at) cached for its rowid, and any mismatch rebuilds.

    Readers get views of [:n]. Appends write past n, and a grow or rebuild
    allocates fresh arrays, so a view handed out earlier is never written to and
    scoring can run outside the lock.
    """

    def __init__(self, db_path: str, model: str) -> None:
        import numpy as np

        self.db_path = db_path
        self.model = model
        self.lock = threading.Lock()
        self._n = 0
        self._dim = 0
        self._rowids = np.empty(0, dtype=np.int64)
        self._stamps = np.empty(0, dtype=np.int64)
        self._mids = np.empty(0, dtype=object)
        self._vecs = np.empty((0, 0), dtype=np.float32)
        self.rebuilds = 0

    @property
    def rows(self) -> int:
        """Number of cached embeddings."""
        return self._n

    def _reserve(self, needed: int) -> None:
        import numpy as np

        if needed <= len(self._rowids):
            return
        # 1/8 headroom: the backfill adds a few hundred rows a day, so a regrow
        # (a full copy) is rare, and the slack stays a few MB rather than the
        # 2x a doubling strategy would leave idle.
        cap = needed + needed // 8 + 1024
        n = self._n
        rowids = np.empty(cap, dtype=np.int64)
        stamps = np.empty(cap, dtype=np.int64)
        mids = np.empty(cap, dtype=object)
        vecs = np.empty((cap, self._dim), dtype=np.float32)
        rowids[:n] = self._rowids[:n]
        stamps[:n] = self._stamps[:n]
        mids[:n] = self._mids[:n]
        if n:
            vecs[:n] = self._vecs[:n]
        self._rowids, self._stamps, self._mids, self._vecs = rowids, stamps, mids, vecs

    def _append(self, rows: list[tuple[int, str, int, bytes]]) -> None:
        import numpy as np

        if not rows:
            return
        if not self._dim:
            self._dim = len(rows[0][3]) // 4
        self._reserve(self._n + len(rows))
        n = self._n
        for i, (rowid, mid, stamp, blob) in enumerate(rows):
            vec = np.frombuffer(blob, dtype=np.float32)
            if vec.shape[0] != self._dim:
                # The old np.stack raised here too: same model, two widths means
                # the store is corrupt or MODEL_NAME was reused for a new model.
                raise ValueError(
                    f"embedding width {vec.shape[0]} != {self._dim} under model {self.model}; re-index needed"
                )
            self._rowids[n + i] = rowid
            self._mids[n + i] = mid
            self._stamps[n + i] = stamp
            self._vecs[n + i] = vec
        self._n = n + len(rows)

    def _load(self, conn: sqlite3.Connection, after_rowid: int) -> int:
        cur = conn.execute(
            """
            SELECT rowid, message_id, embedded_at, embedding FROM message_embeddings
            WHERE model = ? AND rowid > ? ORDER BY rowid
            """,
            (self.model, after_rowid),
        )
        added = 0
        # Chunked so a cold build never holds every blob as Python bytes at once.
        while chunk := cur.fetchmany(4096):
            self._append(chunk)
            added += len(chunk)
        return added

    def _rebuild(self, conn: sqlite3.Connection) -> None:
        import numpy as np

        count = conn.execute("SELECT COUNT(*) FROM message_embeddings WHERE model = ?", (self.model,)).fetchone()[0]
        self._n = 0
        self._dim = 0
        self._rowids = np.empty(0, dtype=np.int64)
        self._stamps = np.empty(0, dtype=np.int64)
        self._mids = np.empty(0, dtype=object)
        self._vecs = np.empty((0, 0), dtype=np.float32)
        t0 = time.monotonic()
        first = conn.execute(
            "SELECT embedding FROM message_embeddings WHERE model = ? LIMIT 1", (self.model,)
        ).fetchone()
        if first is not None:
            self._dim = len(first[0]) // 4
            self._reserve(count)
        self._load(conn, -(2**63))
        self.rebuilds += 1
        logger.info("[recall] loaded %d embeddings into memory in %.2fs", self._n, time.monotonic() - t0)

    def _positions(self, cand_rowids: np.ndarray, cand_mids: np.ndarray, cand_stamps: np.ndarray) -> np.ndarray | None:
        import numpy as np

        n = self._n
        if n == 0:
            return None if len(cand_rowids) else np.empty(0, dtype=np.intp)
        pos = np.searchsorted(self._rowids[:n], cand_rowids)
        safe = np.minimum(pos, n - 1)
        ok = (
            (pos < n)
            & (self._rowids[safe] == cand_rowids)
            & (self._stamps[safe] == cand_stamps)
            & (self._mids[safe] == cand_mids)
        )
        return safe if bool(ok.all()) else None

    def lookup(
        self,
        conn: sqlite3.Connection,
        cand_rowids: np.ndarray,
        cand_mids: np.ndarray,
        cand_stamps: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Bring the cache up to ``conn``'s snapshot and locate the candidates.

        ``conn`` must be inside the read transaction the candidates came from,
        so every candidate is loadable from it.

        Returns:
            (matrix view of every cached row, row index of each candidate in it).

        Raises:
            RuntimeError: when a candidate is still missing after a full reload,
                which would mean the store changed under an open transaction.
        """
        with self.lock:
            if self._n == 0:
                self._rebuild(conn)
            else:
                self._load(conn, int(self._rowids[self._n - 1]))
            pos = self._positions(cand_rowids, cand_mids, cand_stamps)
            if pos is None:
                logger.info("[recall] embedding rowids were reused or renumbered, reloading")
                self._rebuild(conn)
                pos = self._positions(cand_rowids, cand_mids, cand_stamps)
                if pos is None:
                    raise RuntimeError("recall candidates missing from a freshly loaded embedding matrix")
            return self._vecs[: self._n], pos


_matrix: _EmbeddingMatrix | None = None
_matrix_lock = threading.Lock()


def _get_matrix() -> _EmbeddingMatrix:
    """The cache for the current store and model; a new one if either changed."""
    global _matrix
    with _matrix_lock:
        if _matrix is None or _matrix.db_path != MESSAGES_DB_PATH or _matrix.model != MODEL_NAME:
            # Dropping the old one is the whole invalidation for a model change:
            # its rows can never be candidates under the new model anyway.
            _matrix = _EmbeddingMatrix(MESSAGES_DB_PATH, MODEL_NAME)
        return _matrix


def _drop_matrix() -> None:
    global _matrix
    with _matrix_lock:
        _matrix = None


# ---------- background indexer ----------


def _index_pending() -> None:
    """Walk through unembedded messages in batches and embed them."""
    conn = _connect()
    _ensure_table(conn)
    _indexer_state["status"] = "running"
    try:
        while True:
            rows = conn.execute(
                """
                SELECT m.id, m.chat_jid, m.content
                FROM messages m
                LEFT JOIN message_embeddings e
                    ON e.message_id = m.id AND e.chat_jid = m.chat_jid
                   AND e.model = ?
                WHERE m.content IS NOT NULL
                  AND TRIM(m.content) != ''
                  AND e.message_id IS NULL
                LIMIT ?
                """,
                (MODEL_NAME, INDEX_BATCH_SIZE),
            ).fetchall()
            if not rows:
                break

            import numpy as np

            texts = [r[2] for r in rows]
            try:
                embeddings = _embed(texts)
            except Exception as exc:
                logger.exception("[recall] embedding batch failed: %s", exc)
                _indexer_state["status"] = "error"
                _indexer_state["last_error"] = str(exc)
                return

            now = int(time.time())
            params = [
                (r[0], r[1], emb.astype(np.float32).tobytes(), MODEL_NAME, now)
                for r, emb in zip(rows, embeddings, strict=False)
            ]
            conn.executemany(
                """
                INSERT OR REPLACE INTO message_embeddings
                    (message_id, chat_jid, embedding, model, embedded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                params,
            )
            _indexer_state["indexed"] = (_indexer_state.get("indexed", 0) or 0) + len(rows)
        _indexer_state["status"] = "done"
        logger.info("[recall] indexer finished, %d messages indexed total", _indexer_state["indexed"])
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _ensure_indexer_running() -> None:
    """Start the daemon indexer thread if not already running."""
    global _indexer_thread
    with _indexer_lock:
        if _indexer_thread is not None and _indexer_thread.is_alive():
            return
        # Refresh pending count so callers can see progress
        try:
            conn = _connect()
            _ensure_table(conn)
            row = conn.execute(
                """
                SELECT COUNT(*) FROM messages m
                LEFT JOIN message_embeddings e
                    ON e.message_id = m.id AND e.chat_jid = m.chat_jid
                   AND e.model = ?
                WHERE m.content IS NOT NULL AND TRIM(m.content) != ''
                  AND e.message_id IS NULL
                """,
                (MODEL_NAME,),
            ).fetchone()
            _indexer_state["pending"] = row[0] if row else 0
            conn.close()
        except Exception:
            pass

        _indexer_thread = threading.Thread(target=_index_pending, daemon=True, name="recall-indexer")
        _indexer_thread.start()


# ---------- public API ----------


def index_status() -> dict[str, Any]:
    """Return how far the background indexer has gotten."""
    _ensure_indexer_running()
    try:
        conn = _connect()
        _ensure_table(conn)
        embedded = conn.execute("SELECT COUNT(*) FROM message_embeddings WHERE model = ?", (MODEL_NAME,)).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content IS NOT NULL AND TRIM(content) != ''"
        ).fetchone()[0]
        conn.close()
    except Exception as exc:
        return {"error": str(exc)}
    return {
        "status": _indexer_state.get("status", "idle"),
        "embedded": embedded,
        "total": total,
        "remaining": max(total - embedded, 0),
        "model": MODEL_NAME,
    }


def recall(
    query: str,
    chat_jid: str | None = None,
    sender: str | None = None,
    after: str | None = None,
    before: str | None = None,
    is_from_me: bool | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Semantic search over WhatsApp message history.

    Args:
        query: Natural-language query, in any supported language.
        chat_jid: Optional, restrict to one chat.
        sender: Optional sender JID or LID, restrict to one author.
        after: Optional ISO-8601 datetime; only messages with timestamp >= this.
        before: Optional ISO-8601 datetime; only messages with timestamp <= this.
        is_from_me: Optional, True for outgoing only, False for incoming only.
        limit: Max results to return (default 10).

    Returns:
        Dict with `results` (list of matches with similarity score) and
        `index_status` (so the caller can see if indexing is still in flight).
    """

    # np.argsort(...)[:limit] silently does the wrong thing for non-positive
    # limits: -1 returns everything except the last row, 0 returns nothing.
    # Reject rather than surprise the caller.
    if limit is None or limit < 1:
        return {"error": "limit must be a positive integer"}
    limit = min(limit, MAX_RECALL_LIMIT)

    _ensure_indexer_running()
    # recall is an opt-in extra. Without sentence-transformers installed this
    # raises ImportError, which would surface through MCP as a traceback rather
    # than something a caller can act on.
    try:
        qvec = _embed([query])[0]
    except ImportError as exc:
        return {"error": f"recall needs the optional extra: {exc}. Install with: uv sync --extra pro"}
    except Exception as exc:  # noqa: BLE001 - an embedding failure is a result, not a crash
        return {"error": f"could not embed the query: {exc}"}

    conn = _connect()
    try:
        return _search(conn, qvec, chat_jid, sender, after, before, is_from_me, limit)
    finally:
        conn.close()


def _search(
    conn: sqlite3.Connection,
    qvec: np.ndarray,
    chat_jid: str | None,
    sender: str | None,
    after: str | None,
    before: str | None,
    is_from_me: bool | None,
    limit: int,
) -> dict[str, Any]:
    import numpy as np

    _ensure_table(conn)

    # Keys only: the vectors come from the in-memory matrix, the text is fetched
    # for the top hits alone.
    sql_parts = [
        """
        SELECT e.rowid, e.message_id, e.embedded_at
        FROM message_embeddings e
        JOIN messages m
            ON m.id = e.message_id AND m.chat_jid = e.chat_jid
        WHERE e.model = ?
        """
    ]
    # Seeded with the model: embeddings from a different model have a different
    # dimensionality, and a matrix over a mixed set cannot exist.
    params: list[Any] = [MODEL_NAME]
    if chat_jid:
        # A merged LID's messages and embeddings were moved to its twin.
        jids = sorted({chat_jid, canonical_chat_jid(conn, chat_jid)})
        sql_parts.append(f" AND m.chat_jid IN ({','.join('?' * len(jids))})")
        params.extend(jids)
    if sender:
        # Senders are stored as full JIDs now, older rows as bare digits, and one
        # person can be a phone JID in one chat and a LID in another.
        forms = sender_forms(conn, sender)
        sql_parts.append(f" AND m.sender IN ({','.join('?' * len(forms))})")
        params.extend(forms)
    # The bridge stores "2026-10-21 20:54:59+02:00" (space, local offset), so a
    # raw string compare against an ISO "2026-10-21T00:00:00" is wrong: " " sorts
    # before "T" and the whole day falls out of `after`. Compare in UTC instead.
    try:
        after_utc = to_sql_utc(after)
        before_utc = to_sql_utc(before)
    except ValueError as exc:
        return {"error": f"after/before must be ISO-8601 datetimes: {exc}"}
    if after_utc:
        sql_parts.append(" AND datetime(m.timestamp) >= ?")
        params.append(after_utc)
    if before_utc:
        sql_parts.append(" AND datetime(m.timestamp) <= ?")
        params.append(before_utc)
    if is_from_me is not None:
        sql_parts.append(" AND m.is_from_me = ?")
        params.append(1 if is_from_me else 0)

    # One read transaction for candidates, matrix catch-up and the final fetch:
    # the backfill and the bridge's chat merge write concurrently, and without a
    # shared snapshot a candidate could be missing from the matrix, or a top hit
    # gone by the time its text is read.
    conn.execute("BEGIN")
    try:
        rows = conn.execute("".join(sql_parts), params).fetchall()
        if not rows:
            conn.execute("COMMIT")
            return {
                "results": [],
                "index_status": index_status(),
                "warning": (
                    "No embedded messages match your filters yet. The indexer may still"
                    " be warming up — call `recall_index_status` to check progress."
                ),
            }
        # fromiter over itemgetter: a third of the cost of zip(*rows) + np.array,
        # which mattered at 160k candidates.
        n = len(rows)
        cand_rowids = np.fromiter(map(itemgetter(0), rows), np.int64, n)
        cand_mids = np.fromiter(map(itemgetter(1), rows), object, n)
        cand_stamps = np.fromiter(map(itemgetter(2), rows), np.int64, n)
        vecs, pos = _get_matrix().lookup(conn, cand_rowids, cand_mids, cand_stamps)

        # Embeddings are stored normalized and the query is encoded normalized,
        # so dot == cosine. Past a quarter of the matrix one matvec over all of it
        # beats gathering the rows first (a gather of every row is a full copy).
        if len(pos) * 4 >= vecs.shape[0]:
            sims = (vecs @ qvec)[pos]
        else:
            sims = vecs[pos] @ qvec
        k = min(limit, len(sims))
        top = np.argpartition(-sims, k - 1)[:k] if k < len(sims) else np.arange(len(sims))
        top = top[np.argsort(-sims[top], kind="stable")]
        top_rowids = [int(cand_rowids[i]) for i in top]

        details = {
            r[0]: r[1:]
            for r in conn.execute(
                f"""
                SELECT e.rowid, m.id, m.chat_jid, m.sender, m.content, m.timestamp,
                       m.is_from_me, c.name
                FROM message_embeddings e
                JOIN messages m
                    ON m.id = e.message_id AND m.chat_jid = e.chat_jid
                LEFT JOIN chats c ON c.jid = m.chat_jid
                WHERE e.rowid IN ({",".join("?" * len(top_rowids))})
                """,
                top_rowids,
            )
        }
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    results = []
    for i, rowid in zip(top, top_rowids, strict=True):
        mid, cjid, snd, content, ts, from_me, chat_name = details[rowid]
        results.append(
            {
                "message_id": mid,
                "chat_jid": cjid,
                "chat_name": chat_name,
                "sender": snd,
                "is_from_me": bool(from_me),
                "timestamp": ts,
                "content": content,
                "similarity": round(float(sims[int(i)]), 4),
            }
        )

    return {"results": results, "index_status": index_status()}


# Deliberately NO import-time indexer kickoff: recall / recall_index_status
# start it on first call. See "Design choices" in the module docstring.

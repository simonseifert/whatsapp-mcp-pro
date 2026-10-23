"""Benchmark recall's search: the old fetch-and-stack path vs the cached matrix.

Run it against a COPY of a store, never the live one: the first recall call
creates an index, and --scale writes duplicate rows.

    sqlite3 <live>/messages.db ".backup DIR/messages.db"
    python scripts/bench_recall.py --store DIR                 # both impls
    python scripts/bench_recall.py --store DIR --scale 5       # builds DIR/x5/ first

Each implementation runs in its own subprocess so their resident memory does not
mix. The query vector is embedded once with the real model and both sides are
timed from there on, so the numbers are the search alone; add the model's encode
(~10-20 ms on CPU) for an end-to-end recall. Prints numbers only, never message
content or JIDs.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

QUERIES = ["dinner plans for the weekend", "invoice payment reminder", "flight delayed again"]


def _kib(field: str) -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(field + ":"):
            return int(line.split()[1])
    raise RuntimeError(f"{field} not in /proc/self/status")


def _reset_peak() -> None:
    # Linux >= 4.0: "5" resets VmHWM to the current RSS, so each call's peak is its own.
    Path("/proc/self/clear_refs").write_text("5")


def _run(impl: str, store: Path, calls: int) -> dict[str, Any]:
    os.environ["WA_STORE_PATH"] = str(store)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lib import recall as recall_mod
    from tests.recall_reference import reference_search

    recall_mod._ensure_indexer_running = lambda: None  # a bench must not write embeddings
    recall_mod.index_status = lambda: {}  # the old path never counted inside the search
    model = recall_mod._get_model()
    qvecs = {q: model.encode([q], normalize_embeddings=True, convert_to_numpy=True)[0] for q in QUERIES}

    conn = sqlite3.connect(store / "messages.db")
    recall_mod._ensure_table(conn)  # same index for both sides
    # A real chat for the chat filter, never printed: the one with the most embeddings.
    chat = conn.execute(
        "SELECT chat_jid FROM message_embeddings GROUP BY chat_jid ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()[0]
    rows = conn.execute("SELECT COUNT(*) FROM message_embeddings WHERE model = ?", (recall_mod.MODEL_NAME,)).fetchone()
    filters: dict[str, dict[str, Any]] = {
        "none": {},
        "after_30d": {"after": "2026-09-23T00:00:00Z"},
        "is_from_me": {"is_from_me": True},
        "chat": {"chat_jid": chat},
    }

    def search(q: str, kw: dict[str, Any]) -> list[Any]:
        if impl == "old":
            return reference_search(conn, recall_mod.MODEL_NAME, qvecs[q], limit=10, **kw)
        c = recall_mod._connect()
        try:
            return recall_mod._search(c, qvecs[q], limit=10, **{**dict.fromkeys(_ARGS), **kw})["results"]
        finally:
            c.close()

    out: dict[str, Any] = {"impl": impl, "embeddings": rows[0], "rss_model_loaded_mb": _kib("VmRSS") / 1024}
    _reset_peak()
    rss0 = _kib("VmRSS")
    t0 = time.perf_counter()
    search(QUERIES[0], {})
    out["first_call_ms"] = (time.perf_counter() - t0) * 1000
    out["first_call_peak_delta_mb"] = (_kib("VmHWM") - rss0) / 1024
    for name, kw in filters.items():
        times, peaks = [], []
        for i in range(calls):
            _reset_peak()
            before = _kib("VmRSS")
            t = time.perf_counter()
            search(QUERIES[i % len(QUERIES)], kw)
            times.append((time.perf_counter() - t) * 1000)
            peaks.append((_kib("VmHWM") - before) / 1024)
        times.sort()
        out[name] = {
            "median_ms": round(times[len(times) // 2], 1),
            "p90_ms": round(times[int(len(times) * 0.9)], 1),
            "max_peak_delta_mb": round(max(peaks), 1),
        }
    out["rss_after_mb"] = _kib("VmRSS") / 1024
    return out


_ARGS = ("chat_jid", "sender", "after", "before", "is_from_me")


def _scale(src: Path, factor: int) -> Path:
    dst = src / f"x{factor}"
    db = dst / "messages.db"
    if db.exists():
        return dst
    dst.mkdir(exist_ok=True)
    s = sqlite3.connect(src / "messages.db")
    d = sqlite3.connect(db)
    s.backup(d)
    s.close()
    # Copies under new ids with the same vectors: realistic for size and
    # timing, which is all a benchmark needs.
    for k in range(1, factor):
        d.execute(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) "
            "SELECT id || ?, chat_jid, sender, content, timestamp, is_from_me FROM messages WHERE id NOT LIKE '%~%'",
            (f"~{k}",),
        )
        d.execute(
            "INSERT INTO message_embeddings SELECT message_id || ?, chat_jid, embedding, model, embedded_at "
            "FROM message_embeddings WHERE message_id NOT LIKE '%~%'",
            (f"~{k}",),
        )
        d.commit()
    d.close()
    return dst


def main() -> None:
    """Parse arguments and run each implementation in a fresh interpreter."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", type=Path, required=True, help="directory holding a COPY of messages.db")
    ap.add_argument("--scale", type=int, default=1, help="duplicate the history N times first (in STORE/xN/)")
    ap.add_argument("--calls", type=int, default=20)
    ap.add_argument("--impl", choices=["old", "new"], help=argparse.SUPPRESS)
    args = ap.parse_args()

    store = _scale(args.store, args.scale) if args.scale > 1 else args.store
    if args.impl:
        print(json.dumps(_run(args.impl, store, args.calls)))
        return
    for impl in ("old", "new"):
        res = subprocess.run(
            [sys.executable, __file__, "--store", str(store), "--calls", str(args.calls), "--impl", impl],
            check=True,
            capture_output=True,
            text=True,
        )
        print(json.dumps(json.loads(res.stdout.strip().splitlines()[-1]), indent=1))


if __name__ == "__main__":
    main()

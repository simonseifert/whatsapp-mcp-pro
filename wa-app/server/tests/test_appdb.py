import sqlite3

from wa_app import appdb
from wa_app.config import Settings


def test_schema_idempotent(settings: Settings) -> None:
    first = appdb.connect(settings)
    first.close()
    second = appdb.connect(settings)
    tables = {r[0] for r in second.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"meta", "sessions", "login_failures", "read_state", "transcripts"} <= tables
    assert second.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    second.close()


def test_connect_creates_data_dir(settings: Settings) -> None:
    assert not settings.data_dir.exists()
    conn = appdb.connect(settings)
    assert isinstance(conn, sqlite3.Connection)
    assert (settings.data_dir / "app.db").exists()

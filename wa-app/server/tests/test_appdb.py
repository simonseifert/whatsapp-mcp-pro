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


def test_drafts_source_column_is_added_to_an_older_db(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True)
    old = sqlite3.connect(settings.data_dir / "app.db")
    old.execute(
        "CREATE TABLE drafts (id INTEGER PRIMARY KEY, chat_jid TEXT NOT NULL, trigger_id TEXT NOT NULL,"
        " status TEXT NOT NULL, alternatives TEXT, error TEXT, backend_id INTEGER, created_at INTEGER NOT NULL,"
        " ready_at INTEGER)"
    )
    old.execute("INSERT INTO drafts (chat_jid, trigger_id, status, created_at) VALUES ('a', 'b', 'ready', 1)")
    old.commit()
    old.close()
    for _ in range(2):  # the second connect must not try to add it again
        conn = appdb.connect(settings)
        assert conn.execute("SELECT source FROM drafts").fetchall() == [("ai",)]
        conn.close()


def test_wa_app_data_expands_a_tilde(monkeypatch, tmp_path):
    from wa_app import config

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # expanduser reads this on Windows
    monkeypatch.setenv("WA_APP_DATA", "~/wa-data")
    monkeypatch.setenv("WA_STORE_PATH", str(tmp_path / "store"))
    assert config.load().data_dir == tmp_path / "wa-data"

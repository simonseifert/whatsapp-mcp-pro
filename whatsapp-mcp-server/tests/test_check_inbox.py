"""check_inbox's cursor file under concurrent tool calls.

The shared HTTP server runs tools on worker threads. The cursor file was
written with open(..., "w"), which truncates first, so a concurrent reader saw
an empty file, fell into `except: state = {}`, and wrote back a state with
every other chat's cursor gone. It also loaded the whole unseen backlog to
show the newest `limit` rows.
"""

import importlib
import json
import logging
import shutil
import sqlite3
import threading

import pytest

CHAT = "123456789@s.whatsapp.net"


@pytest.fixture
def inbox(monkeypatch, tmp_path, temp_messages_db):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    db = str(store_dir / "messages.db")
    shutil.copy(temp_messages_db, db)

    import lib.utils as utils

    monkeypatch.setattr(utils, "MESSAGES_DB_PATH", db)
    monkeypatch.setenv("WHATSAPP_MCP_TOOLSETS", "all")
    import main

    importlib.reload(main)
    return main, db, store_dir / ".inbox-cursor"


def _add(db: str, n: int, chat: str = CHAT, start: int = 0) -> None:
    conn = sqlite3.connect(db)
    if chat != CHAT:
        conn.execute("INSERT OR IGNORE INTO chats (jid, name) VALUES (?, ?)", (chat, chat))
    conn.executemany(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, ?, ?, ?, 0)",
        [(f"{chat}-{start + i}", chat, chat, f"m{start + i}", "2026-10-21 20:00:00+02:00") for i in range(n)],
    )
    conn.commit()
    conn.close()


def test_returns_the_newest_and_reports_the_backlog(inbox):
    main, db, _ = inbox
    assert main.check_inbox()["messages"] == []  # first call: cursor to now
    _add(db, 5)

    got = main.check_inbox(limit=2)

    assert [m["content"] for m in got["messages"]] == ["m3", "m4"]  # newest, oldest first
    assert got["more"] is True
    assert got["backlog"] == 5
    assert main.check_inbox()["messages"] == []


def test_peek_does_not_advance(inbox):
    main, db, _ = inbox
    main.check_inbox()
    _add(db, 2)

    assert main.check_inbox(mark_seen=False)["count"] == 2
    assert main.check_inbox()["count"] == 2


def test_rejects_a_non_positive_limit(inbox):
    main, _, _ = inbox
    with pytest.raises(ValueError):
        main.check_inbox(limit=0)


def test_a_corrupt_cursor_file_is_kept_and_warned_about(inbox, caplog):
    main, _, cursor_file = inbox
    cursor_file.write_text('{"*": 12, "other@s.whatsapp.net": ')  # torn write

    with caplog.at_level(logging.WARNING):
        main.check_inbox()

    assert "unreadable" in caplog.text
    asides = list(cursor_file.parent.glob(".inbox-cursor.corrupt-*"))
    assert len(asides) == 1
    assert asides[0].read_text().startswith('{"*": 12')


def test_concurrent_checks_keep_every_chats_cursor(inbox):
    main, db, cursor_file = inbox
    chats = [f"10000000{i}@s.whatsapp.net" for i in range(8)]
    for chat in chats:
        _add(db, 1, chat=chat)
        main.check_inbox(chat_jid=chat)  # establish each cursor
    for chat in chats:
        _add(db, 3, chat=chat, start=1)

    torn: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        # The file must always parse: a truncate-then-write exposes "" here.
        while not stop.is_set():
            try:
                text = cursor_file.read_text()
            except PermissionError:
                continue  # Windows: the file is mid-replace; the writer retries, so must this reader
            try:
                json.loads(text)
            except ValueError:
                torn.append(text)

    def worker(chat: str) -> None:
        for _ in range(3):
            main.check_inbox(chat_jid=chat, limit=1)

    r = threading.Thread(target=reader)
    r.start()
    workers = [threading.Thread(target=worker, args=(c,)) for c in chats]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    stop.set()
    r.join()

    assert torn == []
    state = json.loads(cursor_file.read_text())
    assert set(chats) <= set(state)
    # Every chat consumed all three of its new messages, one per call.
    for chat in chats:
        assert main.check_inbox(chat_jid=chat)["messages"] == []


def test_replace_retries_a_windows_sharing_violation(inbox, monkeypatch):
    main, _, _ = inbox
    calls = []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) < 3:
            raise PermissionError(13, "in use")

    monkeypatch.setattr(main.os, "replace", flaky)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    main._replace_with_retry("a", "b", platform="win32")
    assert len(calls) == 3


def test_replace_does_not_retry_off_windows_and_gives_up_on_windows(inbox, monkeypatch):
    main, _, _ = inbox
    calls = []

    def locked(src, dst):
        calls.append(src)
        raise PermissionError(13, "denied")

    monkeypatch.setattr(main.os, "replace", locked)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        main._replace_with_retry("a", "b", platform="linux")
    assert len(calls) == 1
    calls.clear()
    with pytest.raises(PermissionError):
        main._replace_with_retry("a", "b", platform="win32", attempts=4)
    assert len(calls) == 4

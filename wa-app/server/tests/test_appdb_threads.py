"""The shared app.db connection is used from worker threads: every use must be under appdb.db_lock.

A sqlite3 connection shared across threads without a lock can hand one thread another thread's rows (seen in CI as
a ValueError unpacking read_state rows). The readers inside store/unread take the lock themselves, so a caller
that holds it (or does not) is safe either way.
"""

import threading

from wa_app import appdb, store, unread

ITERATIONS = 1500


def _hammer(workers: list) -> list[BaseException]:
    errors: list[BaseException] = []

    def run(fn) -> None:
        try:
            for _ in range(ITERATIONS):
                fn()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(fn,)) for fn in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_concurrent_app_db_reads_never_cross(settings) -> None:
    appconn = appdb.connect(settings)
    with appdb.db_lock:
        for i in range(5):
            appconn.execute("INSERT INTO read_state (chat_jid, read_at, opened_at) VALUES (?,?,?)", (f"c{i}", i, i))
            appconn.execute("INSERT INTO list_chats (list_id, chat_jid) VALUES (1, ?)", (f"c{i}",))
            appconn.execute(
                "INSERT INTO drafts (chat_jid, trigger_id, status, created_at) VALUES (?,?,?,?)",
                (f"c{i}", "t", "ready", i),
            )

    def counter() -> None:
        ro = store.open_ro(settings)  # bridge connections are per thread; only appconn is shared
        try:
            assert len(unread.UnreadCounter(ro, appconn).app_read) == 5
        finally:
            ro.close()

    def members() -> None:
        assert store._list_members(appconn, 1) == {f"c{i}" for i in range(5)}

    def other_locked_user() -> None:
        with appdb.db_lock:
            rows = appconn.execute("SELECT chat_jid, trigger_id, status, created_at FROM drafts").fetchall()
        assert len(rows) == 5

    errors = _hammer([counter, counter, members, other_locked_user])
    assert errors == []

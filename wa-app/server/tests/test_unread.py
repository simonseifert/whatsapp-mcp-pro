"""Read state that follows the phone: the marker, the counts, and the read receipts."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests import conftest as fx
from wa_app import appdb, mcp_lib, store, unread
from wa_app.config import Settings

Conns = tuple[sqlite3.Connection, sqlite3.Connection]


@pytest.fixture
def conns(settings: Settings) -> Iterator[Conns]:
    ro = store.open_ro(settings)
    app = appdb.connect(settings)
    yield ro, app
    ro.close()
    app.close()


def bridge_write(settings: Settings, sql: str, *args: object) -> None:
    conn = sqlite3.connect(settings.store_path / "messages.db")
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def stamp(minutes_ago: float) -> str:
    return (datetime.now().astimezone() - timedelta(minutes=minutes_ago)).replace(microsecond=0).isoformat(sep=" ")


def app_read(settings: Settings, jid: str, minutes_ago: float) -> None:
    app = appdb.connect(settings)
    at = int((datetime.now(UTC) - timedelta(minutes=minutes_ago)).timestamp())
    app.execute("INSERT OR REPLACE INTO read_state (chat_jid, read_at) VALUES (?, ?)", (jid, at))
    app.close()


@pytest.fixture
def receipts(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, list[str], str | None]] = []
    answer: dict = {"success": True}

    def fake(chat_jid: str, message_ids: list[str], sender_jid: str | None = None) -> dict:
        calls.append((chat_jid, list(message_ids), sender_jid))
        return dict(answer)

    monkeypatch.setattr(mcp_lib, "mark_messages_read", fake)
    return calls, answer


def test_phone_read_clears_unread(settings: Settings, conns: Conns) -> None:
    assert unread.count(*conns, fx.ANA) == 2
    bridge_write(settings, "UPDATE chat_state SET last_read_at=? WHERE chat_jid=?", stamp(1), fx.ANA)
    assert unread.count(*conns, fx.ANA) == 0
    assert next(c for c in store.list_chats(*conns) if c.jid == fx.ANA).unread == 0


def test_app_read_clears_unread(settings: Settings, conns: Conns) -> None:
    assert unread.count(*conns, fx.ANA) == 2
    app_read(settings, fx.ANA, 1)
    assert unread.count(*conns, fx.ANA) == 0
    assert next(c for c in store.list_chats(*conns) if c.jid == fx.ANA).unread == 0
    # The marker is the later of the two: an older app read does not undo a newer phone read.
    marker = unread.read_marker(*conns, fx.ANA)
    assert marker is not None and (datetime.now(UTC) - marker) < timedelta(minutes=2)


def test_marker_is_the_later_of_phone_and_app(settings: Settings, conns: Conns) -> None:
    app_read(settings, fx.ANA, 500)  # older than the phone's marker (100 minutes ago)
    phone = store.parse_ts(stamp(100))
    marker = unread.read_marker(*conns, fx.ANA)
    assert marker is not None and phone is not None and abs((marker - phone).total_seconds()) < 5
    assert unread.count(*conns, fx.ANA) == 2


def test_marked_unread_shows_dot(conns: Conns) -> None:
    nikola = next(c for c in store.list_chats(*conns) if c.jid == fx.NIKOLA)
    assert nikola.marked_unread is True and nikola.unread == 0


def test_no_marker_uses_history_count(conns: Conns) -> None:
    assert unread.read_marker(*conns, fx.FAMILY) is None
    assert unread.count(*conns, fx.FAMILY) == 5
    assert unread.count(*conns, fx.IVANA) == 0  # no marker, no history


def test_receipts_grouped_by_sender_in_groups(logged_in: TestClient, settings: Settings, receipts) -> None:
    calls, _ = receipts
    bridge_write(settings, "INSERT INTO chat_state (chat_jid, last_read_at) VALUES (?, ?)", fx.EKIPA, stamp(5000))
    response = logged_in.post(f"/api/chats/{fx.EKIPA}/read")
    assert response.status_code == 200
    assert response.json() == {"unread": 0, "receipts_sent": 11}
    by_sender = {sender: ids for _, ids, sender in calls}
    nikola, petra, ana = fx.NIKOLA, "385910000026@s.whatsapp.net", fx.ANA
    assert {k: sorted(v) for k, v in by_sender.items()} == {
        nikola: ["E01", "E06", "E09", "E10", "E13"],
        petra: ["E03", "E07", "E12"],
        ana: ["E05", "E08", "E11"],
    }
    assert all(chat == fx.EKIPA for chat, _, _ in calls)
    sent = {i for _, ids, _ in calls for i in ids}
    assert not sent & {"E02", "E04", "E14", "E15"}  # mine, deleted, system


def test_private_chat_receipts_have_no_sender(logged_in: TestClient, receipts) -> None:
    calls, _ = receipts
    response = logged_in.post(f"/api/chats/{fx.ANA}/read")
    assert response.json() == {"unread": 0, "receipts_sent": 2}
    assert len(calls) == 1 and calls[0][0] == fx.ANA and calls[0][2] is None and len(calls[0][1]) == 2


def test_import_ids_never_get_receipts(logged_in: TestClient, settings: Settings, receipts) -> None:
    calls, _ = receipts
    bridge_write(
        settings,
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?,?,?,?,?,0)",
        "import-77", fx.ANA, "import:Ana", "old export line", stamp(2),
    )  # fmt: skip
    response = logged_in.post(f"/api/chats/{fx.ANA}/read")
    assert response.json()["unread"] == 0
    sent = [i for _, ids, _ in calls for i in ids]
    assert "import-77" not in sent and len(sent) == 2


def test_read_is_idempotent(logged_in: TestClient, receipts) -> None:
    calls, _ = receipts
    assert logged_in.post(f"/api/chats/{fx.ANA}/read").json() == {"unread": 0, "receipts_sent": 2}
    assert len(calls) == 1
    assert logged_in.post(f"/api/chats/{fx.ANA}/read").json() == {"unread": 0, "receipts_sent": 0}
    assert len(calls) == 1
    chats = logged_in.get("/api/chats").json()["chats"]
    assert next(c for c in chats if c["jid"] == fx.ANA)["unread"] == 0


def test_split_chat_uses_each_messages_own_form(logged_in: TestClient, settings: Settings, receipts) -> None:
    calls, _ = receipts
    bridge_write(settings, "INSERT INTO chat_state (chat_jid, last_read_at) VALUES (?, ?)", fx.TOMO_PHONE, stamp(1000))
    assert logged_in.post(f"/api/chats/{fx.TOMO_PHONE}/read").json() == {"unread": 0, "receipts_sent": 2}
    assert sorted((chat, ids) for chat, ids, _ in calls) == sorted(
        [(fx.TOMO_PHONE, [calls_id(settings, fx.TOMO_PHONE)]), (fx.TOMO_LID, [calls_id(settings, fx.TOMO_LID)])]
    )


def calls_id(settings: Settings, chat: str) -> str:
    conn = sqlite3.connect(settings.store_path / "messages.db")
    try:
        return conn.execute("SELECT id FROM messages WHERE chat_jid=?", (chat,)).fetchone()[0]
    finally:
        conn.close()


def test_at_most_50_ids_per_call(logged_in: TestClient, settings: Settings, receipts) -> None:
    calls, _ = receipts
    conn = sqlite3.connect(settings.store_path / "messages.db")
    for n in range(120):
        conn.execute(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?,?,?,?,?,0)",
            (f"B{n:03d}", fx.GYM, fx.GYM, "x", stamp(30 - n * 0.1)),
        )
    conn.execute("INSERT INTO chat_state (chat_jid, last_read_at) VALUES (?, ?)", (fx.GYM, stamp(9000)))
    conn.commit()
    conn.close()
    response = logged_in.post(f"/api/chats/{fx.GYM}/read")
    assert response.json()["receipts_sent"] == 121  # the 120 plus the old sticker
    assert [len(ids) for _, ids, _ in calls] == [50, 50, 21]


def test_bridge_failure_still_marks_read_locally(logged_in: TestClient, receipts, caplog) -> None:
    calls, answer = receipts
    answer.update({"success": False, "error": "bridge down"})
    response = logged_in.post(f"/api/chats/{fx.ANA}/read")
    assert response.status_code == 200
    assert response.json() == {"unread": 0, "receipts_sent": 0}
    assert len(calls) == 1
    chats = logged_in.get("/api/chats").json()["chats"]
    assert next(c for c in chats if c["jid"] == fx.ANA)["unread"] == 0


def test_bridge_exception_is_swallowed(logged_in: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> dict:
        raise ConnectionError("refused")

    monkeypatch.setattr(mcp_lib, "mark_messages_read", boom)
    response = logged_in.post(f"/api/chats/{fx.ANA}/read")
    assert response.status_code == 200 and response.json() == {"unread": 0, "receipts_sent": 0}


def test_read_publishes_a_chat_event(logged_in: TestClient, receipts) -> None:
    from wa_app import live

    hub = live.current_hub()
    client = hub.subscribe()
    logged_in.post(f"/api/chats/{fx.ANA}/read")
    event = client.queue.get_nowait()
    assert event.name == "chat" and event.data["chat"]["jid"] == fx.ANA and event.data["chat"]["unread"] == 0


def test_read_unknown_chat_404(logged_in: TestClient, receipts) -> None:
    calls, _ = receipts
    assert logged_in.post("/api/chats/999@s.whatsapp.net/read").status_code == 404
    assert calls == []


def test_no_receipts_for_a_marked_read_chat_without_unread(logged_in: TestClient, receipts) -> None:
    calls, _ = receipts
    assert logged_in.post(f"/api/chats/{fx.ACCOUNTANT}/read").json() == {"unread": 0, "receipts_sent": 0}
    assert calls == []


def test_concurrent_reads_send_receipts_once(settings: Settings, receipts) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from wa_app import sending

    calls, _ = receipts
    app = appdb.connect(settings)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: sending.mark_read(settings, app, fx.ANA), range(4)))
    assert sorted(r.receipts_sent for r in results) == [0, 0, 0, 2]
    assert len(calls) == 1


# --- review fixes -----------------------------------------------------------------------------------------------


def dot(logged_in: TestClient, jid: str) -> bool:
    chats = logged_in.get("/api/chats").json()["chats"]
    return next(c for c in chats if c["jid"] == jid)["marked_unread"]


def test_marked_unread_dot_clears_after_app_read_and_returns_when_marked_again(
    logged_in: TestClient, settings: Settings, receipts
) -> None:
    bridge_write(settings, "UPDATE chat_state SET marked_unread_changed_at=? WHERE chat_jid=?", stamp(60), fx.NIKOLA)
    assert dot(logged_in, fx.NIKOLA) is True
    assert logged_in.post(f"/api/chats/{fx.NIKOLA}/read").json()["unread"] == 0
    assert dot(logged_in, fx.NIKOLA) is False
    # The phone marks it unread again, later than the app's read.
    bridge_write(settings, "UPDATE chat_state SET marked_unread_changed_at=? WHERE chat_jid=?", stamp(-5), fx.NIKOLA)
    assert dot(logged_in, fx.NIKOLA) is True


def test_read_does_not_claim_future_messages(logged_in: TestClient, settings: Settings, receipts) -> None:
    """The read marker is the newest message time, never 'now': a delayed message stays unread."""
    logged_in.post(f"/api/chats/{fx.ANA}/read")
    bridge_write(
        settings,
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?,?,?,?,?,0)",
        "LATE1", fx.ANA, fx.ANA, "delayed", stamp(3),
    )  # fmt: skip
    chats = logged_in.get("/api/chats").json()["chats"]
    assert next(c for c in chats if c["jid"] == fx.ANA)["unread"] == 1


def test_app_db_gains_opened_at_column(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE read_state (chat_jid TEXT PRIMARY KEY, read_at INTEGER)")
    appdb.migrate(old)
    assert "opened_at" in {r[1] for r in old.execute("PRAGMA table_info(read_state)")}
    old.close()


def test_receipts_capped_to_newest_200(logged_in: TestClient, settings: Settings, receipts) -> None:
    calls, _ = receipts
    conn = sqlite3.connect(settings.store_path / "messages.db")
    for n in range(260):
        conn.execute(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?,?,?,?,?,0)",
            (f"C{n:03d}", fx.GYM, fx.GYM, "x", stamp(300 - n)),
        )
    conn.execute("INSERT INTO chat_state (chat_jid, last_read_at) VALUES (?, ?)", (fx.GYM, stamp(9000)))
    conn.commit()
    conn.close()
    response = logged_in.post(f"/api/chats/{fx.GYM}/read")
    assert response.json()["receipts_sent"] == 200
    sent = [i for _, ids, _ in calls for i in ids]
    assert len(sent) == 200 and "C259" in sent and "C060" in sent and "C059" not in sent


def test_chat_vanishing_after_claim_still_answers_200(
    logged_in: TestClient, receipts, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "get_chat", lambda *a, **k: None)
    response = logged_in.post(f"/api/chats/{fx.ANA}/read")
    assert response.status_code == 200 and response.json() == {"unread": 0, "receipts_sent": 2}


def test_counter_raises_on_other_db_errors(conns: Conns) -> None:
    ro, _ = conns
    broken = sqlite3.connect(":memory:")
    broken.execute("CREATE TABLE read_state (chat_jid TEXT PRIMARY KEY)")  # no read_at column
    with pytest.raises(sqlite3.OperationalError):
        unread.UnreadCounter(ro, broken)

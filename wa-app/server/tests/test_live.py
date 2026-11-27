"""Live updates: the change poller, the event hub and the SSE stream."""

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

import pytest
from starlette.testclient import TestClient

from tests import conftest as fx
from wa_app import live, store
from wa_app.config import Settings


@contextmanager
def bridge(settings: Settings) -> Iterator[sqlite3.Connection]:
    """Stand-in for the bridge: the one writer of messages.db."""
    conn = sqlite3.connect(settings.store_path / "messages.db")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat(sep=" ")


def add(conn: sqlite3.Connection, chat: str, mid: str, text: str, ts: str | None = None, **kw: object) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?,?,?,?,?,?)",
        (mid, chat, kw.get("sender", chat), text, ts or now(), 1 if kw.get("from_me") else 0),
    )


@pytest.fixture
def poller(settings: Settings) -> live.Poller:
    p = live.Poller(settings)
    assert p.poll_once() == []  # the baseline: what is already in the store is not news
    return p


def names(events: list[live.Event]) -> list[str]:
    return [e.name for e in events]


def test_new_message_emitted_once(settings: Settings, poller: live.Poller) -> None:
    with bridge(settings) as w:
        add(w, fx.ANA, "NEW1", "Stižem za 5 minuta")
    events = poller.poll_once()
    assert names(events) == ["message", "chat"]
    message, chat = events[0].data, events[1].data
    assert message["chat_jid"] == fx.ANA
    assert message["message"]["id"] == "NEW1"
    assert message["message"]["text"] == "Stižem za 5 minuta"
    assert chat["chat"]["jid"] == fx.ANA
    assert chat["chat"]["last_message"]["preview"] == "Stižem za 5 minuta"
    assert poller.poll_once() == []


def test_message_event_uses_the_chat_jid_the_list_uses(settings: Settings, poller: live.Poller) -> None:
    with bridge(settings) as w:
        add(w, fx.TOMO_LID, "NEW2", "Evo me")
    message = poller.poll_once()[0].data
    assert message["chat_jid"] == fx.TOMO_PHONE  # the list shows him under the phone form
    assert message["message"]["chat_jid"] == fx.TOMO_LID  # the row keeps its own, for dedupe by (chat_jid, id)


def test_history_message_is_still_emitted_with_its_old_timestamp(settings: Settings, poller: live.Poller) -> None:
    with bridge(settings) as w:
        add(w, fx.ANA, "OLD1", "Stara poruka iz povijesti", ts="2020-01-01 10:00:00+00:00")
    message = poller.poll_once()[0].data["message"]
    assert message["timestamp_iso"].startswith("2020-01-01")  # the client places it by time, not at the end


def test_replaced_message_is_emitted_again(settings: Settings, poller: live.Poller) -> None:
    with bridge(settings) as w:
        add(w, fx.ANA, "REP1", "first")
    poller.poll_once()
    with bridge(settings) as w:
        add(w, fx.ANA, "REP1", "first, but replaced")  # INSERT OR REPLACE gives the row a new rowid
    events = poller.poll_once()
    assert names(events) == ["message", "chat"]
    assert events[0].data["message"]["text"] == "first, but replaced"


def test_edit_and_delete_emitted(settings: Settings, poller: live.Poller) -> None:
    with bridge(settings) as w:
        w.execute(
            "INSERT INTO message_edits (message_id, chat_jid, edit_id, new_content, edited_at, status)"
            " VALUES ('E02', ?, 'x1', 'edited', ?, 'applied')",
            (fx.EKIPA, now()),
        )
        w.execute(
            "INSERT INTO message_edits (message_id, chat_jid, edit_id, new_content, edited_at, status)"
            " VALUES ('E09', ?, 'x2', 'nope', ?, 'rejected')",
            (fx.EKIPA, now()),
        )
        w.execute(
            "INSERT INTO message_edits (message_id, chat_jid, edit_id, new_content, edited_at, status)"
            " VALUES ('E13', ?, 'x3', 'later', ?, 'pending')",
            (fx.EKIPA, now()),
        )
    events = poller.poll_once()
    changed = [e.data for e in events if e.name == "message_changed"]
    assert changed == [{"chat_jid": fx.EKIPA, "id": "E02"}]  # applied only
    assert poller.poll_once() == []

    with bridge(settings) as w:
        w.execute("UPDATE messages SET deleted_at=? WHERE id='E11' AND chat_jid=?", (now(), fx.EKIPA))
    events = poller.poll_once()
    assert [e.data for e in events if e.name == "message_changed"] == [{"chat_jid": fx.EKIPA, "id": "E11"}]
    assert poller.poll_once() == []


def test_reaction_emitted(settings: Settings, poller: live.Poller) -> None:
    with bridge(settings) as w:
        w.execute("INSERT INTO message_reactions VALUES (?,?,?,?,?)", (fx.EKIPA, "E09", fx.ANA, "😂", now()))
    events = poller.poll_once()
    assert [e.data for e in events if e.name == "reaction"] == [{"chat_jid": fx.EKIPA, "message_id": "E09"}]
    assert poller.poll_once() == []


def test_chat_state_change_emitted(settings: Settings, poller: live.Poller) -> None:
    with bridge(settings) as w:
        w.execute("UPDATE chat_state SET pinned=0, pinned_changed_at=? WHERE chat_jid=?", (now(), fx.ANA))
    events = poller.poll_once()
    assert names(events) == ["chat"]
    assert events[0].data["chat"]["jid"] == fx.ANA
    assert events[0].data["chat"]["pinned"] is False
    assert poller.poll_once() == []
    with bridge(settings) as w:
        w.execute("UPDATE chat_state SET last_read_at=? WHERE chat_jid=?", (now(), fx.ANA))
    events = poller.poll_once()
    assert events[0].data["chat"]["unread"] == 0


def test_poll_survives_store_locked(
    settings: Settings, poller: live.Poller, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with bridge(settings) as w:
        add(w, fx.ANA, "LOCK1", "while locked")
    real = store.open_ro
    monkeypatch.setattr(
        store, "open_ro", lambda s: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
    )
    assert poller.poll_once() == []  # logs and gives up for this tick
    assert "locked" in caplog.text
    monkeypatch.setattr(store, "open_ro", real)
    assert names(poller.poll_once()) == ["message", "chat"]  # nothing was lost


def test_history_sync_flood_collapses_to_a_resync(settings: Settings, poller: live.Poller) -> None:
    with bridge(settings) as w:
        for n in range(live.MAX_MESSAGES_PER_TICK + 20):
            add(w, fx.LONG_CHAT, f"FLOOD{n}", "history", ts="2019-01-01 10:00:00+00:00")
    events = poller.poll_once()
    assert "resync" in names(events)
    assert sum(1 for e in events if e.name == "message") <= live.MAX_MESSAGES_PER_TICK


def test_rows_removed_rebases_the_cursor_and_resyncs(settings: Settings, poller: live.Poller) -> None:
    with bridge(settings) as w:
        add(w, fx.ANA, "GONE1", "soon moved")
    poller.poll_once()
    with bridge(settings) as w:
        w.execute("DELETE FROM messages WHERE id='GONE1'")  # a chat merge moves messages: the newest rowid is gone
    assert names(poller.poll_once()) == ["resync"]
    with bridge(settings) as w:
        add(w, fx.ANA, "AFTER1", "still seen")  # reuses the freed rowid, which is past the rebased cursor
    assert names(poller.poll_once()) == ["message", "chat"]


# --- hub ---------------------------------------------------------------------------------------------------------


def test_resume_with_last_event_id_replays_missed() -> None:
    hub = live.Hub()
    first = hub.publish("chat", {"n": 1})
    hub.publish("chat", {"n": 2})
    hub.publish("chat", {"n": 3})
    client = hub.subscribe(last_event_id=first.cursor)
    replay = [client.queue.get_nowait() for _ in range(client.queue.qsize())]
    assert [e.data["n"] for e in replay] == [2, 3]
    assert first.cursor < replay[0].cursor < replay[1].cursor  # a monotonic cursor


def test_resume_keeps_only_the_last_500_and_asks_for_a_resync_beyond() -> None:
    hub = live.Hub(queue_size=1000)
    events = [hub.publish("chat", {"n": n}) for n in range(600)]
    recent = hub.subscribe(last_event_id=events[300].cursor)  # inside the 500 kept
    assert recent.queue.qsize() == 299
    old = hub.subscribe(last_event_id=events[10].cursor)  # fell out of the buffer
    assert old.queue.get_nowait().name == "resync"
    assert old.queue.qsize() == 0
    stale = hub.subscribe(last_event_id=1)  # from before this process started
    assert stale.queue.get_nowait().name == "resync"


def test_slow_client_is_dropped_not_buffered() -> None:
    hub = live.Hub(queue_size=2)
    slow = hub.subscribe()
    fast = hub.subscribe()
    for n in range(3):
        hub.publish("chat", {"n": n})
        while not fast.queue.empty():
            fast.queue.get_nowait()
    assert slow.dropped
    assert not fast.dropped
    assert hub.client_count() == 1
    assert slow.queue.qsize() <= 2


def test_visible_clients_counts_reported_visibility() -> None:
    hub = live.Hub()
    a, b = hub.subscribe(), hub.subscribe()
    assert live.visible_clients() == 0
    hub.set_visible(a.id, True)
    hub.set_visible(b.id, True)
    assert live.visible_clients() == 2
    hub.set_visible(a.id, False)
    assert live.visible_clients() == 1
    hub.unsubscribe(b)
    assert live.visible_clients() == 0
    assert hub.set_visible("nobody", True) is False


def test_stream_frames_and_heartbeat() -> None:
    async def run() -> list[str]:
        hub = live.Hub()
        stream = live.sse_stream(hub, None, heartbeat=0.05)
        out = [await anext(stream)]  # the preamble
        out.append(await anext(stream))  # the client id event
        out.append(await anext(stream))  # nothing published: a heartbeat comment
        event = hub.publish("reaction", {"chat_jid": "c", "message_id": "m"})
        out.append(await anext(stream))
        out.append(str(event.cursor))
        await stream.aclose()
        assert hub.client_count() == 0
        return out

    preamble, hello, beat, frame, cursor = asyncio.run(run())
    assert preamble.startswith("retry: 3000\n")
    assert hello.startswith("id: ") and "event: hello\n" in hello and '"client"' in hello
    assert beat.startswith(": ")
    assert frame == f"id: {cursor}\nevent: reaction\ndata: {json.dumps({'chat_jid': 'c', 'message_id': 'm'})}\n\n"


# --- routes ------------------------------------------------------------------------------------------------------


def test_live_requires_login(client: TestClient) -> None:
    assert client.get("/api/live").status_code == 401
    assert client.post("/api/live/visibility", json={"client": "x", "visible": True}).status_code == 401


def test_visibility_post(logged_in: TestClient) -> None:
    assert logged_in.post("/api/live/visibility", json={"client": "nobody", "visible": True}).status_code == 404
    assert logged_in.post("/api/live/visibility", json={"visible": "yes"}).status_code == 400
    hub = live.current_hub()
    client = hub.subscribe()
    ok = logged_in.post("/api/live/visibility", json={"client": client.id, "visible": True})
    assert ok.status_code == 200
    assert live.visible_clients() == 1
    bad = logged_in.post(
        "/api/live/visibility", json={"client": client.id, "visible": False}, headers={"Origin": "https://evil.example"}
    )
    assert bad.status_code == 403  # goes through the Origin check
    assert live.visible_clients() == 1


def test_single_message_route(logged_in: TestClient) -> None:
    body = logged_in.get(f"/api/chats/{fx.EKIPA}/messages/E09").json()
    assert body["message"]["id"] == "E09"
    assert logged_in.get(f"/api/chats/{fx.EKIPA}/messages/NOPE").status_code == 404


def test_hello_has_an_id_and_resuming_from_it_replays_later_events() -> None:
    async def run() -> tuple[int, list[str]]:
        hub = live.Hub()
        stream = live.sse_stream(hub, None, heartbeat=5)
        await anext(stream)
        hello = await anext(stream)
        cursor = int(hello.split("\n")[0].removeprefix("id: "))
        await stream.aclose()  # the connection drops before any event
        event = hub.publish("chat", {"n": 1})  # ...and something happens in the gap
        assert event.cursor > cursor
        again = live.sse_stream(hub, cursor, heartbeat=5)
        await anext(again)
        await anext(again)
        frame = await anext(again)
        await again.aclose()
        return event.cursor, [frame]

    cursor, [frame] = asyncio.run(run())
    assert frame.startswith(f"id: {cursor}\nevent: chat\n")


def test_stream_ends_when_its_session_ends() -> None:
    async def run() -> list[str]:
        hub = live.Hub()
        alive = {"abc": True}
        stream = live.sse_stream(hub, None, heartbeat=0.05, session="abc", still_valid=lambda h: alive[h])
        out = [await anext(stream), await anext(stream)]
        alive["abc"] = False  # logged out elsewhere, nothing told the hub
        async for chunk in stream:
            out.append(chunk)
        assert hub.client_count() == 0
        return out

    out = asyncio.run(asyncio.wait_for(run(), 3))
    assert all(c.startswith((": heartbeat", "retry", "id:")) for c in out)


def test_drop_session_ends_that_sessions_streams_at_once() -> None:
    async def run() -> None:
        hub = live.Hub()
        mine = live.sse_stream(hub, None, heartbeat=30, session="s1")
        other = live.sse_stream(hub, None, heartbeat=30, session="s2")
        for s in (mine, other):
            await anext(s)
            await anext(s)
        hub.drop_session("s1")
        assert [c async for c in mine] == []  # ends without waiting for a heartbeat
        assert hub.client_count() == 1
        hub.close()
        assert [c async for c in other] == []
        assert hub.client_count() == 0

    asyncio.run(asyncio.wait_for(run(), 3))


def test_logout_drops_the_open_stream(logged_in: TestClient) -> None:
    from wa_app import auth

    hub = live.current_hub()
    token = logged_in.cookies.get(auth.COOKIE)
    assert token
    client = hub.subscribe(session=auth.token_hash(token))
    assert logged_in.post("/api/auth/logout").status_code == 200
    assert client.dropped
    assert hub.client_count() == 0


def test_schema_is_looked_up_once_per_baseline(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    with bridge(settings) as w:  # an absent table is deliberately looked up again each tick
        w.execute(
            "CREATE TABLE message_receipts (message_id TEXT, chat_jid TEXT, participant TEXT, status INTEGER, at TIMESTAMP)"
        )
    p = live.Poller(settings)
    p.poll_once()
    seen: list[str] = []
    real = sqlite3.Connection.execute

    class Spy(sqlite3.Connection):
        def execute(self, sql, *a):  # noqa: ANN001, ANN002, ANN202
            seen.append(sql)
            return real(self, sql, *a)

    real_open = store.open_ro
    monkeypatch.setattr(
        store,
        "open_ro",
        lambda s: sqlite3.connect(f"{(s.store_path / 'messages.db').as_uri()}?mode=ro", uri=True, factory=Spy),
    )
    p.poll_once()
    monkeypatch.setattr(store, "open_ro", real_open)
    assert not [q for q in seen if "table_info" in q]


def test_hello_id_is_the_resume_point_not_the_latest_cursor() -> None:
    async def run() -> str:
        hub = live.Hub()
        events = [hub.publish("chat", {"n": n}) for n in range(5)]
        stream = live.sse_stream(hub, events[0].cursor)  # resuming: events 1-4 are queued behind the hello
        await anext(stream)
        hello = await anext(stream)
        assert hello.startswith(f"id: {events[0].cursor}\n")
        assert hello.split("\n")[0] != f"id: {hub.cursor}"
        replay = await anext(stream)
        await stream.aclose()
        assert replay.startswith(f"id: {events[1].cursor}\nevent: chat\n")  # the first one after the resume point
        return replay

    assert '"n": 1' in asyncio.run(run())


def test_absent_table_is_looked_up_again(settings: Settings) -> None:
    p = live.Poller(settings)
    with bridge(settings) as w:
        w.execute("DROP TABLE message_edits")
    p.poll_once()
    with bridge(settings) as w:
        w.execute(
            "CREATE TABLE message_edits (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT, chat_jid TEXT,"
            " edit_id TEXT, new_content TEXT, edited_at TEXT, status TEXT DEFAULT 'applied')"
        )
        w.execute(
            "INSERT INTO message_edits (message_id, chat_jid, edit_id, new_content, edited_at)"
            " VALUES ('E02', ?, 'z', 'x', ?)",
            (fx.EKIPA, now()),
        )
    assert [e.name for e in p.poll_once()] == ["message_changed", "chat"]


def test_hello_id_on_a_gap_reproduces_the_gap() -> None:
    async def run() -> None:
        hub = live.Hub(history=3)
        events = [hub.publish("chat", {"n": n}) for n in range(6)]
        stale = events[0].cursor  # aged out of the 3 kept
        stream = live.sse_stream(hub, stale)
        await anext(stream)
        hello = await anext(stream)
        assert hello.startswith(f"id: {stale}\n")  # not the cursor: a drop now must still look like a gap
        assert (await anext(stream)).startswith(f"id: {hub.cursor}\nevent: resync\n")
        await stream.aclose()
        again = hub.subscribe(last_event_id=stale)
        assert again.queue.get_nowait().name == "resync"

    asyncio.run(run())


def test_logout_all_drops_every_open_stream(logged_in: TestClient) -> None:
    from wa_app import auth

    hub = live.current_hub()
    token = logged_in.cookies.get(auth.COOKIE)
    assert token
    mine = hub.subscribe(session=auth.token_hash(token))
    elsewhere = hub.subscribe(session="another-device")
    assert logged_in.post("/api/auth/logout-all").status_code == 200
    assert mine.dropped
    assert elsewhere.dropped
    assert hub.client_count() == 0


def test_poll_ticks_do_not_wait_behind_a_saturated_default_pool(settings: Settings) -> None:
    """Sends, avatars, thumbnails and transcription share AnyIO's default pool: a burst must not stall live ticks."""
    import threading

    import anyio.to_thread

    async def run() -> int:
        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = 1
        release = threading.Event()
        blocker = asyncio.create_task(anyio.to_thread.run_sync(release.wait))  # takes the only token
        await asyncio.sleep(0.05)
        poller = live.Poller(settings)
        ticks: list[int] = []
        real = poller.poll_once

        def counting() -> list[live.Event]:
            ticks.append(1)
            return real()

        poller.poll_once = counting  # type: ignore[method-assign]
        task = asyncio.create_task(poller.run(live.Hub(), interval=0.01))
        await asyncio.sleep(0.4)
        task.cancel()
        release.set()
        await blocker
        return len(ticks)

    assert asyncio.run(asyncio.wait_for(run(), 5)) >= 3


def test_resume_further_behind_than_the_queue_gets_one_resync() -> None:
    hub = live.Hub(history=50, queue_size=5)
    events = [hub.publish("chat", {"n": n}) for n in range(30)]
    client = hub.subscribe(last_event_id=events[10].cursor)  # in the buffer, but 19 events behind a queue of 5
    assert hub.client_count() == 1
    assert client.resume_point == events[10].cursor
    assert client.queue.get_nowait().name == "resync"
    assert client.queue.empty()


def test_visibility_records_open_chat(logged_in: TestClient) -> None:
    hub = live.current_hub()
    client = hub.subscribe()
    other = hub.subscribe()
    jid = "385911110001@s.whatsapp.net"
    assert not hub.chat_open(jid)
    ok = logged_in.post("/api/live/visibility", json={"client": client.id, "visible": True, "chat": jid})
    assert ok.status_code == 200
    assert hub.chat_open(jid) and not hub.chat_open("other@s.whatsapp.net")
    # Hidden: the chat is still on screen in a locked phone, but it no longer counts as open.
    logged_in.post("/api/live/visibility", json={"client": client.id, "visible": False, "chat": jid})
    assert not hub.chat_open(jid)
    # Visible again with no chat (a list screen), then another client opens it.
    logged_in.post("/api/live/visibility", json={"client": client.id, "visible": True, "chat": None})
    assert not hub.chat_open(jid)
    logged_in.post("/api/live/visibility", json={"client": other.id, "visible": True, "chat": jid})
    assert hub.chat_open(jid)
    hub.unsubscribe(other)
    assert not hub.chat_open(jid)
    bad = logged_in.post("/api/live/visibility", json={"client": client.id, "visible": True, "chat": 5})
    assert bad.status_code == 400

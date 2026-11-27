"""The one-at-a-time history backfill queue and its routes. No real sleeping: poll_s=0 and a fake clock."""

import asyncio
from datetime import date

import pytest
from starlette.testclient import TestClient

from tests.conftest import ANA, CHANNEL, FAMILY
from wa_app import history

A, B, C = "385910000601@s.whatsapp.net", "385910000602@s.whatsapp.net", "385910000603@s.whatsapp.net"
TODAY = date(2026, 11, 27)
BEFORE = "2025-05-05T00:00:00+00:00"  # the chat's oldest stored message before the start


class Bridge:
    """A fake bridge: each started job reports `running` for `polls` status calls, then its final state."""

    def __init__(self, final: dict | None = None, polls: int = 1) -> None:
        self.final = final or {"stopped_by": "phone has nothing older", "batches": 3, "oldest": "2024-01-01"}
        self.finals: dict[str, dict] = {}
        self.polls = polls
        self.events: list[str] = []
        self.calls: list[tuple[str, str | None, int]] = []
        self.left: dict[str, int] = {}
        self.start_reply: dict | None = None
        self.status_reply: dict | None = None
        self.grow = False  # running jobs report a rising batch count
        self.ticks = 0

    def start(self, jid: str, until: str | None, max_batches: int) -> dict:
        self.events.append(f"start:{jid}")
        self.calls.append((jid, until, max_batches))
        if self.start_reply is not None:
            return self.start_reply
        self.left[jid] = self.polls
        return {"success": True, "job": {"chat_jid": jid, "running": True}}

    def status(self) -> dict:
        self.events.append("status")
        if self.status_reply is not None:
            return self.status_reply
        jobs = []
        for jid, left in self.left.items():
            if left > 0:
                self.left[jid] = left - 1
                self.ticks += 1
                batches = self.ticks if self.grow else 1
                jobs.append({"chat_jid": jid, "running": True, "batches": batches, "oldest": "2025-05-05"})
            else:
                jobs.append({"chat_jid": jid, "running": False, **self.finals.get(jid, self.final)})
        return {"success": True, "jobs": jobs}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 100.0  # every look at the clock is 100 s later
        return self.now


def make(bridge: Bridge, **kw) -> history.BackfillQueue:
    return history.BackfillQueue(bridge.start, bridge.status, poll_s=0, today=lambda: TODAY, **kw)


async def _drain(queue: history.BackfillQueue) -> dict:
    task = asyncio.create_task(queue.run())
    for _ in range(10_000):
        snap = queue.snapshot()
        if not snap["queued"] and not snap["running"] and snap["done"]:
            break
        await asyncio.sleep(0)
    queue.stop()
    await asyncio.wait_for(task, 5)
    return queue.snapshot()


def drain(queue: history.BackfillQueue) -> dict:
    return asyncio.run(_drain(queue))


def test_one_at_a_time() -> None:
    bridge = Bridge(polls=2)
    queue = make(bridge)
    assert queue.enqueue([(A, A), (B, B), (C, C)], "year") == 3
    drain(queue)
    events = bridge.events
    starts = [i for i, e in enumerate(events) if e.startswith("start:")]
    assert [events[i] for i in starts] == [f"start:{A}", f"start:{B}", f"start:{C}"]
    assert all("status" in events[a:b] for a, b in zip(starts, starts[1:], strict=False))
    # the second job starts only after the first job's status said it stopped: 2 running polls + 1 final poll
    assert starts[1] - starts[0] == 5  # the idle check before the start adds one status call


@pytest.mark.parametrize(("depth", "until"), [("year", "2025-11-27"), ("3y", "2023-11-28"), ("all", None)])
def test_depth_mapping(depth, until) -> None:
    bridge = Bridge()
    queue = make(bridge)
    queue.enqueue([(A, A)], depth)
    drain(queue)
    assert bridge.calls == [(A, until, 200)]


@pytest.mark.parametrize(
    ("final", "result", "detail"),
    [
        ({"stopped_by": "phone has nothing older", "oldest": "2024-01-01"}, "done", "phone has nothing older"),
        ({"stopped_by": "reached until", "oldest": "2025-01-01"}, "done", "reached until"),
        ({"stopped_by": "max batches", "oldest": "2025-06-01"}, "more", "max batches"),
        ({"stopped_by": "error", "error": "not connected"}, "failed", "not connected"),
    ],
)
def test_result_mapping(final, result, detail) -> None:
    queue = make(Bridge(final=final))
    queue.enqueue([(A, A)], "all")
    done = (drain(queue))["done"]
    assert done[0]["jid"] == A and done[0]["result"] == result and done[0]["detail"] == detail
    if "oldest" in final:
        assert done[0]["oldest"] == final["oldest"]


def test_start_failure_moves_on() -> None:
    bridge = Bridge()
    real = bridge.start

    def start(jid, until, max_batches):
        if jid == A:
            return {"success": False, "error": "Request error: connection refused"}
        return real(jid, until, max_batches)

    queue = history.BackfillQueue(start, bridge.status, poll_s=0, today=lambda: TODAY)
    queue.enqueue([(A, A), (B, B)], "year")
    done = (drain(queue))["done"]
    assert [(d["jid"], d["result"]) for d in done] == [(A, "failed"), (B, "done")]
    assert "Request error" in done[0]["detail"]


def test_conflict_waits() -> None:
    bridge = Bridge(final={"stopped_by": "reached until", "oldest": "2025-01-01"}, polls=2)
    bridge.left[A] = 2  # the bridge is already running A (app restarted mid-fill)
    bridge.start_reply = {
        "success": False,
        "error": f"a backfill is already running for {A}",
        "job": {"chat_jid": A, "running": True},
    }
    queue = make(bridge)
    queue.enqueue([(A, A)], "year")
    done = (drain(queue))["done"]
    assert done[0]["result"] == "done" and bridge.events.count("status") == 4


def test_stalled_job_fails_only_after_the_bridge_says_it_is_over() -> None:
    bridge = Bridge(polls=20)  # A's batch count never moves, and A ends on its own after 20 polls
    bridge.finals[B] = {"stopped_by": "reached until", "oldest": "2025-01-01"}
    real = bridge.start

    def start(jid, until, max_batches):
        reply = real(jid, until, max_batches)
        if jid == B:
            bridge.left[B] = 0  # B finishes at once
        return reply

    queue = history.BackfillQueue(start, bridge.status, poll_s=0, today=lambda: TODAY, stall_s=300, clock=Clock())
    queue.enqueue([(A, A), (B, B)], "year")
    done = (drain(queue))["done"]
    assert [(d["jid"], d["result"]) for d in done] == [(A, "failed"), (B, "done")]
    assert done[0]["detail"] == "Stopped making progress"
    events = bridge.events
    # B starts only after A's job reported not running: every status before B's start is A's, and A ran out first
    assert bridge.left[A] == 0 and events.index(f"start:{B}") > events.index(f"start:{A}") + 20


def test_slow_but_progressing_job_is_not_failed() -> None:
    bridge = Bridge(polls=100)  # 100 polls x 2 clock looks x 100 s = far beyond 30 minutes in total
    bridge.grow = True
    clock = Clock()
    queue = make(bridge, stall_s=300, clock=clock)
    queue.enqueue([(A, A)], "year")
    done = (drain(queue))["done"]
    assert clock.now > 1800
    assert done[0]["result"] == "done" and done[0]["detail"] == "phone has nothing older"


def test_nothing_came_back_fails_and_two_in_a_row_pause_the_queue() -> None:
    # the job ends on the very anchor it started with: the phone did not answer
    bridge = Bridge(final={"stopped_by": "phone has nothing older", "batches": 3, "oldest": "2025-05-05"})
    queue = make(bridge)
    queue.enqueue([(A, A, BEFORE), (B, B, BEFORE), (C, C, BEFORE)], "year")
    done = (drain(queue))["done"]
    assert [(d["jid"], d["result"]) for d in done] == [(A, "failed"), (B, "failed"), (C, "skipped")]
    assert done[0]["detail"] == "No older messages came back. Is your phone online?"
    assert done[2]["detail"] == "Paused: phone not answering"
    assert [c[0] for c in bridge.calls] == [A, B]


def test_oldest_that_moved_before_the_first_poll_is_still_progress() -> None:
    # the bridge already got a batch (oldest moved past BEFORE) before the first poll, then 3 misses ended the job
    bridge = Bridge(polls=0)
    bridge.finals[A] = {"stopped_by": "phone has nothing older", "batches": 4, "oldest": "2025-01-01T00:00:00Z"}
    bridge.finals[B] = dict(bridge.finals[A])
    bridge.finals[C] = dict(bridge.finals[A])
    queue = make(bridge)
    queue.enqueue([(A, A, BEFORE), (B, B, BEFORE), (C, C, BEFORE)], "year")
    done = (drain(queue))["done"]
    assert [d["result"] for d in done] == ["done", "done", "done"]
    assert queue._no_answer == 0


def test_cancel_resets_the_no_answer_streak() -> None:
    bridge = Bridge(final={"stopped_by": "phone has nothing older", "batches": 3, "oldest": "2025-05-05"})
    queue = make(bridge)

    async def scenario() -> list[dict]:
        task = asyncio.create_task(queue.run())

        async def until(n: int) -> None:
            for _ in range(10_000):
                if len(queue.snapshot()["done"]) >= n and not queue.snapshot()["running"]:
                    return
                await asyncio.sleep(0)

        queue.enqueue([(A, A, BEFORE)], "year")
        await until(1)
        assert queue._no_answer == 1
        queue.cancel()
        assert queue._no_answer == 0
        queue.enqueue([(B, B, BEFORE), (C, C, BEFORE)], "year")
        await until(3)
        queue.stop()
        await asyncio.wait_for(task, 5)
        return queue.snapshot()["done"]

    done = asyncio.run(scenario())
    # without the reset B would be the second no-answer in a row and C would be paused
    assert [d["result"] for d in done] == ["failed", "failed", "failed"]


def test_one_failure_without_an_answer_does_not_pause() -> None:
    bridge = Bridge()
    bridge.finals[A] = {"stopped_by": "phone has nothing older", "batches": 3, "oldest": "2025-05-05"}
    queue = make(bridge)
    queue.enqueue([(A, A, BEFORE), (B, B, BEFORE), (C, C, BEFORE)], "year")
    done = (drain(queue))["done"]
    assert [d["result"] for d in done] == ["failed", "done", "done"]


def test_waits_for_any_running_bridge_job_before_starting() -> None:
    bridge = Bridge()
    bridge.left[C] = 3  # a job the app did not start (left over from before a restart)
    queue = make(bridge)
    queue.enqueue([(A, A)], "year")
    done = (drain(queue))["done"]
    first_start = bridge.events.index(f"start:{A}")
    assert bridge.events[:first_start] == ["status"] * 4  # 3 running polls, then idle
    assert done[0]["result"] == "done"


def test_bridge_down_while_waiting() -> None:
    bridge = Bridge()
    bridge.status_reply = {"success": False, "error": "Request error: connection refused"}
    queue = make(bridge)
    queue.enqueue([(A, A)], "year")
    done = (drain(queue))["done"]
    assert done[0]["result"] == "failed" and "connection refused" in done[0]["detail"]


def test_dedupe_and_cancel() -> None:
    bridge = Bridge(polls=10**9)  # A stays running
    queue = make(bridge)
    assert queue.enqueue([(A, A), (B, B)], "year") == 2
    assert queue.enqueue([(B, B), (C, C)], "year") == 1

    async def scenario() -> dict:
        task = asyncio.create_task(queue.run())
        while not queue.snapshot()["running"]:
            await asyncio.sleep(0)
        assert queue.enqueue([(A, A)], "year") == 0  # running counts as known
        queue.cancel()
        snap = queue.snapshot()
        queue.stop()
        task.cancel()
        return snap

    snap = asyncio.run(scenario())
    assert snap["queued"] == [] and snap["running"]["jid"] == A


def test_no_anchor_skipped() -> None:
    bridge = Bridge()
    queue = make(bridge)
    queue.enqueue([(A, None)], "year")
    done = (drain(queue))["done"]
    assert done[0]["result"] == "skipped" and done[0]["detail"] == "nothing to anchor on"
    assert bridge.calls == []


def test_done_keeps_last_100() -> None:
    queue = make(Bridge())
    queue.enqueue([(f"38591000{n:04d}@s.whatsapp.net", None) for n in range(120)], "year")
    done = (drain(queue))["done"]
    assert len(done) == 100 and done[-1]["jid"].startswith("38591000" + "0119")


# --- routes ---------------------------------------------------------------------------------------------------


@pytest.fixture
def bridge_app(logged_in: TestClient):
    bridge = Bridge()
    queue = make(bridge)
    logged_in.app.state.backfill = queue  # type: ignore[attr-defined]
    return logged_in, queue


def test_fill_thin_enqueues_only_thin_chats(bridge_app) -> None:
    client, queue = bridge_app
    res = client.post("/api/history/fill", json={"scope": "thin", "depth": "year"})
    assert res.status_code == 200
    body = res.json()
    thin = {c["jid"] for c in client.get("/api/history/coverage").json()["chats"] if c["thin"]}
    assert thin and body["added"] == len(thin) and set(body["status"]["queued"]) == thin
    assert CHANNEL not in thin and ANA in thin
    assert client.post("/api/history/fill", json={"scope": "thin", "depth": "year"}).json()["added"] == 0


def test_fill_all_enqueues_every_covered_chat(bridge_app) -> None:
    client, _ = bridge_app
    chats = client.get("/api/history/coverage").json()["chats"]
    assert client.post("/api/history/fill", json={"scope": "all", "depth": "all"}).json()["added"] == len(chats)


@pytest.mark.parametrize(
    "body",
    [{"scope": "thin", "depth": "week"}, {"scope": "some", "depth": "year"}, {"depth": "year"}, [], {}],
)
def test_fill_bad_input(bridge_app, body) -> None:
    client, queue = bridge_app
    assert client.post("/api/history/fill", json=body).status_code == 422
    assert queue.snapshot()["queued"] == []


def test_chat_history(bridge_app) -> None:
    client, queue = bridge_app
    res = client.post(f"/api/chats/{FAMILY}/history", json={"depth": "3y"})
    assert res.status_code == 200 and res.json()["added"] == 1 and queue.snapshot()["queued"] == [FAMILY]
    assert client.post(f"/api/chats/{FAMILY}/history", json={"depth": "x"}).status_code == 422
    assert client.post("/api/chats/999999@s.whatsapp.net/history", json={"depth": "year"}).status_code == 404


def test_status_and_cancel(bridge_app) -> None:
    client, queue = bridge_app
    client.post("/api/history/fill", json={"scope": "all", "depth": "year"})
    assert client.get("/api/history/status").json()["queued"]
    res = client.post("/api/history/cancel")
    assert res.status_code == 200 and res.json()["queued"] == [] and queue.snapshot()["queued"] == []


def test_origin_and_login_required(bridge_app, settings) -> None:
    client, queue = bridge_app
    evil = {"Origin": "https://evil.example"}
    for path, body in [
        ("/api/history/fill", {"scope": "all", "depth": "year"}),
        ("/api/history/cancel", {}),
        (f"/api/chats/{ANA}/history", {"depth": "year"}),
    ]:
        assert client.post(path, json=body, headers=evil).status_code == 403
    assert queue.snapshot()["queued"] == []
    from wa_app.main import create_app

    with TestClient(create_app(settings), base_url="https://testserver") as anon:
        assert anon.get("/api/history/status").status_code == 401


def test_route_enqueue_wakes_a_live_queue(settings) -> None:
    """run() is live on the app loop (started by the lifespan); a route must start the job with no other wake-up,
    and enqueue must run on the loop thread, not in the threadpool."""
    from wa_app.main import create_app

    bridge = Bridge()
    queue = make(bridge)
    on_loop: list[bool] = []
    orig = queue.enqueue

    def enqueue(items, depth):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return orig(items, depth)

    queue.enqueue = enqueue  # type: ignore[method-assign]
    app = create_app(settings)
    app.state.backfill = queue
    with TestClient(app, base_url="https://testserver") as client:
        client.post("/api/auth/setup", json={"passphrase": "correct horse battery"})
        client.post("/api/auth/login", json={"passphrase": "correct horse battery"})
        assert client.post(f"/api/chats/{FAMILY}/history", json={"depth": "year"}).status_code == 200
        for _ in range(100_000):  # the loop thread works the queue on its own; no sleeping, just look again
            if client.get("/api/history/status").json()["done"]:
                break
        assert queue.snapshot()["done"][0]["jid"] == FAMILY
    assert on_loop == [True] and bridge.calls


def test_chat_history_newsletter_and_broadcast_never_reach_the_bridge(bridge_app) -> None:
    client, queue = bridge_app
    assert client.post(f"/api/chats/{CHANNEL}/history", json={"depth": "year"}).status_code == 404
    assert client.post("/api/chats/status@broadcast/history", json={"depth": "year"}).status_code == 404
    assert queue.snapshot()["queued"] == []

"""Draft worker: triggers, debounce, cancel, concurrency, failure, live events. Fictional data, fake backend."""

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Any

import pytest

from tests.conftest import ANA, EKIPA, NEIGHBOURHOOD
from wa_app import appdb, live, store
from wa_app.ai import config as ai_config
from wa_app.ai import drafts
from wa_app.ai.backends import BackendError
from wa_app.ai.drafts import DraftWorker

LOCAL = {"kind": "openai", "label": "Local", "base_url": "http://127.0.0.1:11434/v1", "model": "m"}
CLOUD = {"kind": "anthropic", "label": "Cloud", "model": "m", "api_key": "sk-FICTIONAL"}
REPLY = json.dumps({"alternatives": ["Može!", "Dogovoreno"]})


def _ts(minutes_ago: float = 0) -> str:
    return (datetime.now().astimezone() - timedelta(minutes=minutes_ago)).replace(microsecond=0).isoformat(sep=" ")


def _insert(settings, chat, mid, text="Dolaziš večeras?", minutes_ago=0.0, **kw) -> None:
    conn = sqlite3.connect(settings.store_path / "messages.db")
    conn.execute(
        "INSERT OR REPLACE INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me,"
        " is_system_message, media_type) VALUES (?,?,?,?,?,?,?,?,?)",
        (mid, chat, kw.get("sender", chat), kw.get("sender_name", "Ana"), text, _ts(minutes_ago),
         int(kw.get("from_me", False)), int(kw.get("system", False)), kw.get("media_type")),
    )  # fmt: skip
    conn.commit()
    conn.close()


def _event(chat, mid, **kw) -> live.Event:
    return live.Event("message", {"chat_jid": chat, "message": {"id": mid, "from_me": kw.get("from_me", False)}})


class Fake:
    """Backend stand-in: records calls, can block until released or fail."""

    def __init__(self, reply=REPLY, error=None) -> None:
        self.reply, self.error = reply, error
        self.calls: list = []
        self.active = 0
        self.peak = 0
        self.gate: asyncio.Event | None = None

    async def __call__(self, cfg, secret, prompt, settings, **kw) -> str:
        self.calls.append((cfg.kind, prompt))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.error:
                raise self.error
            return self.reply
        finally:
            self.active -= 1


class Rig:
    def __init__(self, settings, fake, visible) -> None:
        self.settings, self.fake = settings, fake
        self.appconn = appdb.connect(settings)
        self.hub = live.Hub()
        self.sleeps: list[float] = []
        self.sleep_gate: asyncio.Event | None = None
        self.visible_now = visible

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            if self.sleep_gate is not None:
                await self.sleep_gate.wait()

        self.worker = DraftWorker(
            settings, self.hub, appconn=self.appconn, complete=fake, sleep=sleep, visible=lambda: self.visible_now
        )
        self.client = self.hub.subscribe()

    def setup(self, chat=ANA, backend=LOCAL, **chat_ai) -> int:
        b = ai_config.save_backend(self.appconn, self.settings, backend)
        ai_config.set_default_backend(self.appconn, b.id)
        ai_config.save_chat_ai(self.appconn, chat, {"enabled": True, **chat_ai})
        return b.id

    def events(self) -> list[dict]:
        out = []
        while not self.client.queue.empty():
            e = self.client.queue.get_nowait()
            if e.name == "draft":
                out.append(e.data)
        return out

    def rows(self, chat=ANA) -> list[tuple]:
        return self.appconn.execute("SELECT status, error FROM drafts WHERE chat_jid=? ORDER BY id", (chat,)).fetchall()


def run(settings, scenario, *, fake=None, visible=1):
    async def main():
        rig = Rig(settings, fake or Fake(), visible)
        task = asyncio.create_task(rig.worker.run())
        await asyncio.sleep(0)
        try:
            await scenario(rig)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(main())


async def feed(rig: Rig, *events: live.Event) -> None:
    rig.worker.on_events(list(events))
    await asyncio.sleep(0.01)
    await rig.worker.idle()


def test_open_app_drafts_immediately(settings) -> None:
    async def scenario(rig):
        rig.setup()
        _insert(settings, ANA, "N1")
        await feed(rig, _event(ANA, "N1"))
        assert rig.sleeps == []
        assert rig.rows() == [("ready", None)]
        assert len(rig.fake.calls) == 1

    run(settings, scenario)


def test_closed_app_waits_20s_quiet(settings) -> None:
    async def scenario(rig):
        rig.setup()
        rig.sleep_gate = asyncio.Event()
        _insert(settings, ANA, "N1")
        rig.worker.on_events([_event(ANA, "N1")])
        await asyncio.sleep(0.05)
        assert rig.sleeps == [20.0]
        assert rig.fake.calls == [] and rig.rows() == []
        rig.sleep_gate.set()
        await rig.worker.idle()
        assert len(rig.fake.calls) == 1 and rig.rows() == [("ready", None)]

    run(settings, scenario, visible=0)


def test_burst_of_8_one_call_for_last(settings) -> None:
    async def scenario(rig):
        rig.setup()
        rig.sleep_gate = asyncio.Event()
        for i in range(8):
            _insert(settings, ANA, f"B{i}", f"msg {i}")
            rig.worker.on_events([_event(ANA, f"B{i}")])
            await asyncio.sleep(0.02)
        rig.sleep_gate.set()
        await rig.worker.idle()
        assert len(rig.fake.calls) == 1
        assert "msg 7" in rig.fake.calls[0][1].user
        assert rig.appconn.execute("SELECT trigger_id FROM drafts").fetchall() == [("B7",)]

    run(settings, scenario, visible=0)


def test_new_message_cancels_running_draft(settings) -> None:
    async def scenario(rig):
        rig.setup()
        rig.fake.gate = asyncio.Event()
        _insert(settings, ANA, "N1")
        rig.worker.on_events([_event(ANA, "N1")])
        await asyncio.sleep(0.1)
        assert rig.fake.active == 1
        _insert(settings, ANA, "N2", "Još sam tu")
        rig.worker.on_events([_event(ANA, "N2")])
        await asyncio.sleep(0.1)
        assert rig.fake.active == 1  # the first call was cancelled, the second runs
        rig.fake.gate.set()
        await rig.worker.idle()
        assert [r[0] for r in rig.rows()] == ["superseded", "ready"]

    run(settings, scenario)


def test_disabled_chat_never_drafts(settings) -> None:
    async def scenario(rig):
        rig.setup(enabled=False)
        _insert(settings, ANA, "N1")
        await feed(rig, _event(ANA, "N1"))
        assert rig.fake.calls == [] and rig.rows() == []

    run(settings, scenario)


def test_old_or_imported_or_own_or_system_never_triggers(settings) -> None:
    async def scenario(rig):
        rig.setup()
        own: list = []
        rig.worker.own_message_listeners.append(lambda chat, m: own.append(m["id"]))
        _insert(settings, ANA, "OLD", minutes_ago=11)
        _insert(settings, ANA, "import-abc")
        _insert(settings, ANA, "MINE", from_me=True)
        _insert(settings, ANA, "SYS", system=True, text="Ana joined")
        _insert(settings, ANA, "EMPTY", text="")
        await feed(rig, *(_event(ANA, m, from_me=m == "MINE") for m in ("OLD", "import-abc", "MINE", "SYS", "EMPTY")))
        assert rig.fake.calls == [] and rig.rows() == []
        assert own == ["MINE"]  # the seam for outcome learning
        _insert(settings, ANA, "FRESH", minutes_ago=9)
        await feed(rig, _event(ANA, "FRESH"))
        assert len(rig.fake.calls) == 1

    run(settings, scenario)


def test_only_text_voice_and_captioned_media_trigger(settings) -> None:
    async def scenario(rig):
        rig.setup()
        _insert(settings, ANA, "STK", text="", media_type="sticker")
        _insert(settings, ANA, "PIC", text="", media_type="image")
        _insert(settings, ANA, "DOC", text="  ", media_type="document")
        _insert(settings, ANA, "CARD", text="[Contact: Marko]\n📞 +385910000009")
        _insert(settings, ANA, "POLL", text="[Poll: Pizza?]")
        conn = sqlite3.connect(settings.store_path / "messages.db")
        conn.execute("INSERT INTO poll_options VALUES ('POLL', ?, 0, 'Da')", (ANA,))
        conn.commit()
        conn.close()
        await feed(rig, *(_event(ANA, m) for m in ("STK", "PIC", "DOC", "CARD", "POLL")))
        assert rig.fake.calls == [] and rig.rows() == []
        for n, (mid, text, kind) in enumerate(
            [("TXT", "Dolaziš?", None), ("CAP", "Evo slika", "image"), ("VN", "", "ptt")]
        ):
            _insert(settings, ANA, mid, text=text, media_type=kind)
            await feed(rig, _event(ANA, mid))
            assert len(rig.fake.calls) == n + 1, mid

    run(settings, scenario)


def test_chat_switched_to_local_only_while_waiting_for_a_slot(settings) -> None:
    async def scenario(rig):
        rig.setup(backend=CLOUD)
        _insert(settings, ANA, "N1")
        for _ in range(drafts.CLOUD_SLOTS):
            await rig.worker._cloud.acquire()  # the cloud is busy
        rig.worker.on_events([_event(ANA, "N1")])
        await asyncio.sleep(0.1)
        assert rig.rows() == [("pending", None)]
        ai_config.save_chat_ai(rig.appconn, ANA, {"local_only": True})
        for _ in range(drafts.CLOUD_SLOTS):
            rig.worker._cloud.release()
        await rig.worker.idle()
        assert rig.fake.calls == []
        assert rig.rows() == [("failed", "This chat is local only")]

    run(settings, scenario)


def test_chat_switched_off_while_waiting_for_a_slot_makes_no_call(settings) -> None:
    async def scenario(rig):
        rig.setup(backend=CLOUD)
        _insert(settings, ANA, "N1")
        for _ in range(drafts.CLOUD_SLOTS):
            await rig.worker._cloud.acquire()  # the cloud is busy
        rig.worker.on_events([_event(ANA, "N1")])
        await asyncio.sleep(0.1)
        assert rig.rows() == [("pending", None)]
        ai_config.save_chat_ai(rig.appconn, ANA, {"enabled": False})
        for _ in range(drafts.CLOUD_SLOTS):
            rig.worker._cloud.release()
        await rig.worker.idle()
        assert rig.fake.calls == []
        assert rig.rows() == [("superseded", None)]  # nothing left pending
        assert rig.events()[-1]["draft"] is None  # the pending card is withdrawn

    run(settings, scenario)


def test_switched_off_during_the_quiet_wait_makes_no_draft(settings) -> None:
    async def scenario(rig):
        rig.setup()
        _insert(settings, ANA, "N1")
        rig.sleep_gate = asyncio.Event()
        rig.worker.on_events([_event(ANA, "N1")])
        await asyncio.sleep(0.1)
        ai_config.save_chat_ai(rig.appconn, ANA, {"enabled": False})
        rig.sleep_gate.set()
        await rig.worker.idle()
        assert rig.fake.calls == [] and rig.rows() == []

    run(settings, scenario, visible=0)


def test_announcement_group_needs_admin(settings) -> None:
    async def scenario(rig):
        rig.setup(chat=NEIGHBOURHOOD)
        _insert(settings, NEIGHBOURHOOD, "A1", "Sastanak u petak")
        await feed(rig, _event(NEIGHBOURHOOD, "A1"))
        assert rig.fake.calls == []
        conn = sqlite3.connect(settings.store_path / "messages.db")
        conn.execute("UPDATE identities SET i_am_admin=1 WHERE jid=?", (NEIGHBOURHOOD,))
        conn.commit()
        conn.close()
        _insert(settings, NEIGHBOURHOOD, "A2", "Sastanak u petak u 18")
        await feed(rig, _event(NEIGHBOURHOOD, "A2"))
        assert len(rig.fake.calls) == 1

    run(settings, scenario)


@pytest.mark.parametrize(("backend", "cap"), [(LOCAL, 1), (CLOUD, 3)])
def test_local_cap_1_cloud_cap_3(settings, backend, cap) -> None:
    chats = [ANA, EKIPA, "385910000003@s.whatsapp.net", "385910000006@s.whatsapp.net"]

    async def scenario(rig):
        for chat in chats:
            rig.setup(chat=chat, backend=backend)
            _insert(settings, chat, f"X-{chat[:6]}")
        rig.fake.gate = asyncio.Event()
        for chat in chats:
            rig.worker.on_events([_event(chat, f"X-{chat[:6]}")])
        await asyncio.sleep(0.2)
        assert rig.fake.active == cap
        rig.fake.gate.set()
        await rig.worker.idle()
        assert len(rig.fake.calls) == 4 and rig.fake.peak == cap

    run(settings, scenario)


def test_backend_error_sets_failed_no_retry(settings) -> None:
    async def scenario(rig):
        rig.setup()
        _insert(settings, ANA, "N1")
        await feed(rig, _event(ANA, "N1"))
        assert rig.rows() == [("failed", "AI is down")]
        await asyncio.sleep(0.05)
        assert len(rig.fake.calls) == 1
        _insert(settings, ANA, "N1")  # re-inserted by the bridge: not a new trigger
        await feed(rig, _event(ANA, "N1"))
        assert len(rig.fake.calls) == 1

    run(settings, scenario, fake=Fake(error=BackendError("AI is down")))


def test_unexpected_exception_is_generic(settings) -> None:
    async def scenario(rig):
        rig.setup()
        _insert(settings, ANA, "N1", "tajni sadržaj")
        await feed(rig, _event(ANA, "N1"))
        assert rig.rows() == [("failed", "Couldn't draft")]

    run(settings, scenario, fake=Fake(error=RuntimeError("tajni sadržaj")))


def test_policy_refusal_reaches_card(settings) -> None:
    async def scenario(rig):
        rig.setup(backend=CLOUD, local_only=True)
        _insert(settings, ANA, "N1")
        await feed(rig, _event(ANA, "N1"))
        assert rig.fake.calls == []
        assert rig.rows() == [("failed", "This chat is local only")]
        assert rig.events()[-1]["draft"]["error"] == "This chat is local only"

    run(settings, scenario)


def test_expired_after_24h(settings) -> None:
    conn = appdb.connect(settings)
    old = int(time.time()) - 25 * 3600
    conn.execute("INSERT INTO drafts (chat_jid, trigger_id, status, alternatives, created_at) VALUES (?,?,?,?,?)",
                 (ANA, "O1", "ready", '["x"]', old))  # fmt: skip
    conn.execute("INSERT INTO drafts (chat_jid, trigger_id, status, alternatives, created_at) VALUES (?,?,?,?,?)",
                 (ANA, "O2", "ready", '["y"]', int(time.time()) - 3600))  # fmt: skip
    view = drafts.current(conn, ANA)
    assert view and view.trigger_id == "O2" and view.alternatives == ["y"]
    assert conn.execute("SELECT status FROM drafts WHERE trigger_id='O1'").fetchone() == ("expired",)
    conn.execute("DELETE FROM drafts WHERE trigger_id='O2'")
    assert drafts.current(conn, ANA) is None


def test_one_off_request_on_disabled_chat(settings) -> None:
    async def scenario(rig):
        rig.setup(enabled=False)
        view = rig.worker.request(ANA)  # last incoming message of the fixture chat
        assert view.status == "pending" and view.trigger_id
        await rig.worker.idle()
        assert rig.rows() == [("ready", None)] and len(rig.fake.calls) == 1
        with pytest.raises(LookupError):
            rig.worker.request(ANA, "NOPE")

    run(settings, scenario, visible=0)


def test_one_off_request_still_passes_policy(settings) -> None:
    async def scenario(rig):
        rig.setup(enabled=False, backend=CLOUD, local_only=True)
        rig.worker.request(ANA)
        await rig.worker.idle()
        assert rig.fake.calls == [] and rig.rows() == [("failed", "This chat is local only")]

    run(settings, scenario)


def test_group_draft_quotes_trigger(settings) -> None:
    async def scenario(rig):
        rig.setup(chat=EKIPA)
        _insert(settings, EKIPA, "G1", "Tko nosi roštilj?", sender_name="Nikola")
        await feed(rig, _event(EKIPA, "G1"))
        ro = store.open_ro(settings)
        view = drafts.current(rig.appconn, EKIPA, ro)
        assert view and view.quote and view.quote.id == "G1" and view.quote.preview == "Tko nosi roštilj?"

    run(settings, scenario)


def test_dismiss_publishes_null(settings) -> None:
    async def scenario(rig):
        rig.setup()
        _insert(settings, ANA, "N1")
        await feed(rig, _event(ANA, "N1"))
        draft = drafts.current(rig.appconn, ANA)
        rig.events()
        assert draft is not None
        rig.worker.dismiss(draft.id)
        assert rig.events() == [{"chat_jid": ANA, "draft": None}]
        assert drafts.current(rig.appconn, ANA) is None

    run(settings, scenario)


def test_sse_draft_events_sequence(settings) -> None:
    async def scenario(rig):
        rig.setup()
        _insert(settings, ANA, "N1")
        await feed(rig, _event(ANA, "N1"))
        seen = rig.events()
        assert [e["draft"]["status"] for e in seen] == ["pending", "ready"]
        assert seen[1]["draft"]["alternatives"] == ["Može!", "Dogovoreno"]
        assert seen[0]["chat_jid"] == ANA and seen[0]["draft"]["trigger_id"] == "N1"

    run(settings, scenario)


def test_poller_hands_events_to_listeners(settings) -> None:
    poller = live.Poller(settings)
    got: list = []
    poller.on_events.append(got.extend)

    def failing(events: list[Any]) -> None:
        1 / 0

    poller.on_events.append(failing)  # a failing listener never stops the poll
    assert poller._tick() == []
    _insert(settings, ANA, "P1")
    events = poller._tick()
    assert [e.name for e in events][0] == "message" and got == events


# --- no send path --------------------------------------------------------------------------------------------------


def test_drafts_module_has_no_send_path() -> None:
    code = "import sys, wa_app.ai.drafts\nimport wa_app.ai.learning\nimport wa_app.agent_api\nprint('wa_app.sending' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_no_draft_route_sends(logged_in) -> None:
    from wa_app import sending

    calls: list = []
    orig = sending.send_text
    sending.send_text = lambda *a, **k: calls.append(a)  # type: ignore[assignment]
    try:
        assert logged_in.get(f"/api/chats/{ANA}/draft").json() == {"draft": None, "chat_jid": ANA}
        logged_in.post(f"/api/chats/{ANA}/draft", json={})
        logged_in.post(f"/api/chats/{ANA}/draft", json={"trigger_id": "x"})
        logged_in.delete("/api/drafts/1")
    finally:
        sending.send_text = orig
    assert calls == []


def test_draft_routes(logged_in) -> None:
    r = logged_in.post(f"/api/chats/{ANA}/draft", json={})
    assert r.status_code == 202 and r.json()["draft"]["status"] in ("pending", "failed", "ready")
    got = logged_in.get(f"/api/chats/{ANA}/draft").json()["draft"]
    assert got["id"] == r.json()["draft"]["id"]
    assert logged_in.delete(f"/api/drafts/{got['id']}").status_code == 204
    assert logged_in.get(f"/api/chats/{ANA}/draft").json() == {"draft": None, "chat_jid": ANA}
    assert logged_in.delete("/api/drafts/9999").status_code == 404
    assert logged_in.get("/api/chats/nobody@s.whatsapp.net/draft").status_code == 404


def test_create_failure_does_not_kill_the_job(settings, caplog) -> None:
    async def scenario(rig):
        rig.setup()
        _insert(settings, ANA, "N1", "tajni sadržaj")
        orig = drafts._create
        drafts._create = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("tajni sadržaj"))
        try:
            await feed(rig, _event(ANA, "N1"))
        finally:
            drafts._create = orig
        assert rig.rows() == [] and rig.fake.calls == [] and rig.events() == []
        assert "tajni" not in caplog.text
        _insert(settings, ANA, "N2")  # the worker still works
        await feed(rig, _event(ANA, "N2"))
        assert rig.rows() == [("ready", None)]

    run(settings, scenario)


@pytest.mark.parametrize(
    ("fake", "error"),
    [
        (Fake(reply="not json"), "No usable draft"),
        (Fake(reply='{"alternatives": []}'), "No usable draft"),
        (Fake(error=BackendError("AI didn't answer in time")), "AI didn't answer in time"),
    ],
)
def test_bad_backend_replies_fail_once_and_free_the_slot(settings, fake, error) -> None:
    other = "385910000003@s.whatsapp.net"

    async def scenario(rig):
        rig.setup()
        rig.setup(chat=other)
        _insert(settings, ANA, "N1")
        await feed(rig, _event(ANA, "N1"))
        assert rig.rows() == [("failed", error)]
        await asyncio.sleep(0.02)
        assert len(rig.fake.calls) == 1  # no retry
        fake.error, fake.reply = None, REPLY  # the backend recovers; the slot must be free
        _insert(settings, other, "O1")
        await feed(rig, _event(other, "O1"))
        assert rig.rows(other) == [("ready", None)]

    run(settings, scenario, fake=fake)


def test_hanging_backend_times_out_and_frees_slot(settings) -> None:
    async def hang(*args, **kw):
        await asyncio.sleep(10)

    async def scenario(rig):
        rig.setup()
        rig.worker._complete = lambda *a, **k: backends.complete(*a, timeout=0.05, **k)
        _insert(settings, ANA, "N1")
        await feed(rig, _event(ANA, "N1"))
        assert rig.rows() == [("failed", "AI didn't answer in time")]
        assert rig.worker._local._value == 1

    from wa_app.ai import backends

    real = backends._openai
    backends._openai = hang
    try:
        run(settings, scenario)
    finally:
        backends._openai = real


def test_request_without_incoming_message_is_nothing_to_reply_to(settings) -> None:
    async def scenario(rig):
        rig.setup()
        conn = sqlite3.connect(settings.store_path / "messages.db")
        conn.execute("DELETE FROM messages WHERE chat_jid=? AND COALESCE(is_from_me,0)=0", (ANA,))
        conn.commit()
        conn.close()
        with pytest.raises(LookupError, match="Nothing to reply to"):
            rig.worker.request(ANA, None)

    run(settings, scenario)


def test_request_rejects_own_and_system_triggers(settings) -> None:
    async def scenario(rig):
        rig.setup()
        _insert(settings, ANA, "MINE", from_me=True)
        _insert(settings, ANA, "SYS", system=True, text="Ana joined")
        for mid in ("MINE", "SYS"):
            with pytest.raises(LookupError):
                rig.worker.request(ANA, mid)
        assert rig.rows() == []

    run(settings, scenario)


def test_request_view_carries_the_group_quote(settings) -> None:
    async def scenario(rig):
        rig.setup(chat=EKIPA)
        _insert(settings, EKIPA, "G1", "Tko nosi roštilj?", sender_name="Nikola")
        view = rig.worker.request(EKIPA, "G1")
        assert view.quote and view.quote.id == "G1"
        await rig.worker.idle()

    run(settings, scenario)


def test_worker_task_crash_is_logged_without_message(caplog) -> None:
    from wa_app.main import _log_worker_end

    async def boom() -> None:
        raise RuntimeError("Dolaziš večeras? secret chat text")

    async def main() -> None:
        task = asyncio.create_task(boom())
        task.add_done_callback(_log_worker_end)
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    with caplog.at_level("ERROR", logger="wa_app"):
        asyncio.run(main())
    assert "RuntimeError" in caplog.text and "secret chat text" not in caplog.text

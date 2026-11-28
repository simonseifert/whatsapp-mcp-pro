"""Draft outcomes, per-chat rule distillation, feedback and the send hint. Fictional data, fake backend."""

import asyncio
import json
import sqlite3
import time
from datetime import datetime

import pytest

from tests.conftest import ANA, EKIPA, until
from tests.test_ai_drafts import CLOUD, LOCAL, Fake, Rig, _event, _insert, _ts, feed
from wa_app import appdb, live, mcp_lib
from wa_app.ai import config as ai_config
from wa_app.ai import learning
from wa_app.ai.drafts import DraftWorker

NOW = 1_800_000_000.0


def _appconn(settings) -> sqlite3.Connection:
    return appdb.connect(settings)


def make_draft(conn, chat=ANA, alts=("Može!", "Dogovoreno"), status="ready", ready_ago=60.0, now=None) -> int:
    now = now if now is not None else time.time()
    ready_at = int(now - ready_ago) if status != "pending" else None
    cur = conn.execute(
        "INSERT INTO drafts (chat_jid, trigger_id, status, alternatives, created_at, ready_at) VALUES (?,?,?,?,?,?)",
        (chat, "T1", status, json.dumps(list(alts)), int(now - ready_ago - 5), ready_at),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def outcome(conn, draft_id):
    return conn.execute(
        "SELECT kind, ratio, best_alternative, sent_id FROM draft_outcomes WHERE draft_id=?", (draft_id,)
    ).fetchone()


def status_of(conn, draft_id) -> str:
    return conn.execute("SELECT status FROM drafts WHERE id=?", (draft_id,)).fetchone()[0]


def counter(conn, chat=ANA) -> int:
    row = conn.execute("SELECT outcomes_since_distill FROM chat_ai WHERE chat_jid=?", (chat,)).fetchone()
    return row[0] if row else 0


def test_used_edited_ignored_thresholds(settings) -> None:
    conn = _appconn(settings)
    high = "a" * 19 + "b"  # 20 chars: ratio = 2 * matches / 40
    low = "a" * 7 + "b" * 13
    assert learning.similarity(high, "a" * 19 + "c") == 0.95
    assert learning.similarity(low, "a" * 7 + "c" * 13) == 0.35
    assert learning.similarity(low, "a" * 6 + "c" * 14) == pytest.approx(0.3)
    for n, (alt, sent, kind) in enumerate(
        [
            (high, "a" * 19 + "c", "used"),  # 0.95
            (high, "a" * 18 + "cc", "edited"),  # 0.90
            (low, "a" * 7 + "c" * 13, "edited"),  # 0.35
            (low, "a" * 6 + "c" * 14, "ignored"),  # 0.30
        ]
    ):
        make_draft(conn, alts=(alt,))
        assert learning.record_own_message(conn, ANA, f"S{n}", sent, time.time()) == kind
    # the closest of the alternatives is the one compared
    d = make_draft(conn, alts=("Zdravo svima", "Dolazim oko osam"))
    assert learning.record_own_message(conn, ANA, "S9", "Dolazim oko osam", time.time()) == "used"
    assert outcome(conn, d)[2] == "Dolazim oko osam"
    assert counter(conn) == 4  # used, edited, edited, used; the ignored one does not count


def test_reply_from_phone_supersedes_and_removes_card(settings) -> None:
    async def scenario(rig):
        rig.setup()
        _insert(settings, ANA, "N1")
        await feed(rig, _event(ANA, "N1"))
        assert rig.rows() == [("ready", None)]
        rig.events()
        own = live.Event("message", {"chat_jid": ANA, "message": {
            "id": "M1", "from_me": True, "text": "Može!", "timestamp_iso": _ts(0)}})  # fmt: skip
        await feed(rig, own)
        assert rig.rows() == [("done", None)]
        assert rig.events()[-1] == {"chat_jid": ANA, "draft": None}
        assert outcome(rig.appconn, 1)[:1] == ("used",)

    _run(settings, scenario)


def test_reply_while_pending_cancels_the_job(settings) -> None:
    async def scenario(rig):
        rig.setup()
        rig.fake.gate = asyncio.Event()
        _insert(settings, ANA, "N1")
        rig.worker.on_events([_event(ANA, "N1")])
        await until(lambda: rig.rows() == [("pending", None)])
        assert rig.rows() == [("pending", None)]
        rig.events()
        own = live.Event("message", {"chat_jid": ANA, "message": {
            "id": "M1", "from_me": True, "text": "Evo me", "timestamp_iso": _ts(0)}})  # fmt: skip
        await feed(rig, own)
        assert rig.rows() == [("superseded", None)]
        assert rig.events()[-1] == {"chat_jid": ANA, "draft": None}
        assert outcome(rig.appconn, 1)[0] == "ignored"
        assert counter(rig.appconn) == 0

    _run(settings, scenario)


def test_outside_15_minutes_not_linked(settings) -> None:
    conn = _appconn(settings)
    d = make_draft(conn, ready_ago=60)
    assert learning.record_own_message(conn, ANA, "S1", "Može!", time.time() + 15 * 60) is None
    assert outcome(conn, d) is None and status_of(conn, d) == "ready" and counter(conn) == 0
    assert learning.record_own_message(conn, ANA, "S2", "Može!", time.time() + 10 * 60) == "used"


def test_one_message_one_draft_one_outcome(settings) -> None:
    conn = _appconn(settings)
    d = make_draft(conn)
    assert learning.record_own_message(conn, ANA, "S1", "Može!", time.time()) == "used"
    assert learning.record_own_message(conn, ANA, "S1", "Može!", time.time()) is None  # same message again
    assert learning.record_own_message(conn, ANA, "S2", "Može!", time.time()) is None  # no draft left
    assert conn.execute("SELECT COUNT(*) FROM draft_outcomes").fetchone()[0] == 1
    assert status_of(conn, d) == "done"


def test_send_hint_prefers_that_draft_within_window(settings) -> None:
    conn = _appconn(settings)
    older = make_draft(conn, status="superseded", ready_ago=300, alts=("Vidimo se sutra",))
    newer = make_draft(conn, ready_ago=30, alts=("Može!",))
    other_chat = make_draft(conn, chat=EKIPA, ready_ago=300, alts=("Vidimo se sutra",))
    learning.set_hint(ANA, other_chat)  # another chat's draft is never linked
    assert learning.record_own_message(conn, ANA, "S1", "Vidimo se sutra", time.time()) == "ignored"
    assert outcome(conn, other_chat) is None and outcome(conn, newer)[0] == "ignored"
    d = make_draft(conn, status="superseded", ready_ago=300, alts=("Vidimo se sutra",))
    live_one = make_draft(conn, ready_ago=30, alts=("Može!",))
    learning.set_hint(ANA, d)
    assert learning.record_own_message(conn, ANA, "S2", "Vidimo se sutra", time.time()) == "used"
    assert outcome(conn, d)[0] == "used" and outcome(conn, live_one) is None
    assert status_of(conn, older) == "superseded" and status_of(conn, live_one) == "superseded"
    old = make_draft(conn, status="dismissed", ready_ago=20 * 60, alts=("Vidimo se sutra",))
    learning.set_hint(ANA, old)  # hinted but outside the 15 minute window
    assert learning.record_own_message(conn, ANA, "S3", "Vidimo se sutra", time.time()) is None


def test_ignored_does_not_count_toward_distill(settings) -> None:
    async def scenario(rig):
        rig.setup()
        for i in range(12):
            make_draft(rig.appconn)
            await feed(rig, _own(f"M{i}", "Potpuno nešto drugo u pet"))
        assert counter(rig.appconn) == 0
        assert rig.rows() and all(r[0] == "done" for r in rig.rows())
        assert rig.fake.calls == []
        kinds = {r[0] for r in rig.appconn.execute("SELECT kind FROM draft_outcomes")}
        assert kinds == {"ignored"}

    _run(settings, scenario)


def _own(mid, text, chat=ANA) -> live.Event:
    return live.Event("message", {"chat_jid": chat, "message": {
        "id": mid, "from_me": True, "text": text, "timestamp_iso": _ts(0)}})  # fmt: skip


DISTILLED = "- Kratke rečenice\n- Bez emojija"


def test_tenth_outcome_triggers_distill_once(settings) -> None:
    async def scenario(rig):
        rig.setup()
        rig.fake.reply = DISTILLED
        for i in range(9):
            make_draft(rig.appconn, alts=("Može!",))
            await feed(rig, _own(f"M{i}", "Može!"))
        assert rig.fake.calls == [] and counter(rig.appconn) == 9
        make_draft(rig.appconn, alts=("Može!",))
        await feed(rig, _own("M9", "Može!"))
        await rig.worker.idle()
        assert len(rig.fake.calls) == 1
        assert "writing rules" in rig.fake.calls[0][1].system
        assert ai_config.chat_ai(rig.appconn, ANA).rules == DISTILLED
        assert counter(rig.appconn) == 0
        make_draft(rig.appconn, alts=("Može!",))
        await feed(rig, _own("M10", "Može!"))
        assert len(rig.fake.calls) == 1  # the next crossing is ten outcomes away

    _run(settings, scenario)


def test_distill_respects_local_only(settings) -> None:
    conn = _appconn(settings)
    b = ai_config.save_backend(conn, settings, CLOUD)
    ai_config.set_default_backend(conn, b.id)
    ai_config.save_chat_ai(conn, ANA, {"enabled": True, "local_only": True, "rules": "- Jedan"})
    seed_outcomes(conn, 3)
    fake = Fake(reply=DISTILLED)
    assert asyncio.run(learning.distill(settings, conn, ANA, complete=fake)) is None
    assert fake.calls == []
    assert ai_config.chat_ai(conn, ANA).rules == "- Jedan" and counter(conn) == 3


def seed_outcomes(conn, n, chat=ANA) -> None:
    conn.execute("INSERT OR IGNORE INTO chat_ai (chat_jid) VALUES (?)", (chat,))
    for i in range(n):
        conn.execute(
            "INSERT INTO draft_outcomes (draft_id, chat_jid, sent_id, sent_text, best_alternative, ratio, kind, at)"
            " VALUES (?,?,?,?,?,0.6,'edited',?)",
            (1000 + i, chat, f"S{i}", f"Stiže u {i}", f"Dolazim u {i}", int(time.time()) + i),
        )
    conn.execute("UPDATE chat_ai SET outcomes_since_distill=? WHERE chat_jid=?", (n, chat))


def test_distill_keeps_user_edit_as_base(settings) -> None:
    conn = _appconn(settings)
    b = ai_config.save_backend(conn, settings, LOCAL)
    ai_config.set_default_backend(conn, b.id)
    ai_config.save_chat_ai(conn, ANA, {"rules": "- Nikad ne koristi emoji"})
    edited_at = conn.execute("SELECT rules_edited_at FROM chat_ai WHERE chat_jid=?", (ANA,)).fetchone()[0]
    assert edited_at is not None
    seed_outcomes(conn, 4)
    fake = Fake(reply="- Nikad ne koristi emoji\n- Piši kratko")
    out = asyncio.run(learning.distill(settings, conn, ANA, complete=fake))
    assert out == "- Nikad ne koristi emoji\n- Piši kratko"
    assert "Nikad ne koristi emoji" in fake.calls[0][1].user and "Dolazim u 0 → Stiže u 0" in fake.calls[0][1].user
    row = conn.execute("SELECT rules, rules_edited_at, outcomes_since_distill FROM chat_ai WHERE chat_jid=?", (ANA,))
    assert row.fetchone() == (out, edited_at, 0)


def test_distill_failure_keeps_old_rules(settings) -> None:
    from wa_app.ai.backends import BackendError

    conn = _appconn(settings)
    b = ai_config.save_backend(conn, settings, LOCAL)
    ai_config.set_default_backend(conn, b.id)
    ai_config.save_chat_ai(conn, ANA, {"rules": "- Staro"})
    seed_outcomes(conn, 10)
    assert asyncio.run(learning.distill(settings, conn, ANA, complete=Fake(error=BackendError("down")))) is None
    assert asyncio.run(learning.distill(settings, conn, ANA, complete=Fake(reply="nema bulleta"))) is None
    assert ai_config.chat_ai(conn, ANA).rules == "- Staro" and counter(conn) == 10


def test_rules_capped_12_lines(settings) -> None:
    conn = _appconn(settings)
    b = ai_config.save_backend(conn, settings, LOCAL)
    ai_config.set_default_backend(conn, b.id)
    seed_outcomes(conn, 2)
    lines = ["Evo pravila:", "```", *(f"- pravilo {i} ⟦you⟧ `x` \x07" + "z" * 300 for i in range(20)), "```"]
    out = asyncio.run(learning.distill(settings, conn, ANA, complete=Fake(reply="\n".join(lines))))
    assert out is not None
    got = out.splitlines()
    assert len(got) <= 12 and all(line.startswith("- ") for line in got)
    assert len(out) <= 2000
    assert not any(c in out for c in ("`", "⟦", "⟧", "\x07"))
    assert ai_config.chat_ai(conn, ANA).rules == out


def test_nightly_sweep_distills_chats_with_new_outcomes(settings) -> None:
    async def main():
        clock = [datetime(2026, 11, 24, 3, 29).timestamp()]
        delays: list[float] = []
        hold = asyncio.Event()

        async def nightly_sleep(seconds: float) -> None:
            delays.append(seconds)
            clock[0] += seconds + 1  # the sleep ends a second after 03:30
            if len(delays) > 1:
                await hold.wait()

        rig = Rig(settings, Fake(reply=DISTILLED), 1)
        rig.worker = DraftWorker(
            settings, rig.hub, appconn=rig.appconn, complete=rig.fake, now=lambda: clock[0], nightly_sleep=nightly_sleep
        )
        rig.setup()
        rig.setup(chat=EKIPA)
        seed_outcomes(rig.appconn, 3, ANA)
        task = asyncio.create_task(rig.worker.run())
        await until(lambda: len(delays) >= 2)
        await rig.worker.idle()
        assert delays[0] == pytest.approx(60.0) and delays[1] == pytest.approx(24 * 3600 - 1)
        assert len(rig.fake.calls) == 1
        assert ai_config.chat_ai(rig.appconn, ANA).rules == DISTILLED
        assert ai_config.chat_ai(rig.appconn, EKIPA).rules is None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


def _run(settings, scenario) -> None:
    from tests.test_ai_drafts import run

    run(settings, scenario)


# --- feedback and the send hint over the API ---------------------------------------------------------------------


def test_feedback_validation(logged_in, settings) -> None:
    conn = appdb.connect(settings)
    d = make_draft(conn)
    url = f"/api/drafts/{d}/feedback"
    assert logged_in.post(url, json={"thumb": 1, "note": "Točno moj stil"}).status_code == 204
    assert conn.execute("SELECT value FROM meta WHERE key=?", (f"draft_feedback:{d}",)).fetchone() is not None
    for body in ({}, {"thumb": 2}, {"thumb": True}, {"thumb": "up"}, {"thumb": 1, "note": 5}, {"thumb": 1, "note": "x" * 301},
                 [1], "x"):  # fmt: skip
        assert logged_in.post(url, json=body).status_code == 422, body
    assert logged_in.post(url, content=b"{nope").status_code == 422
    assert logged_in.post("/api/drafts/99999/feedback", json={"thumb": 1}).status_code == 404
    assert logged_in.post("/api/drafts/abc/feedback", json={"thumb": 1}).status_code == 404
    assert logged_in.post(url, json={"thumb": None, "note": "x" * 300}).status_code == 204
    # it rides along into the outcome, and a later thumb updates the stored outcome
    assert learning.record_own_message(conn, ANA, "S1", "Može!", time.time()) == "used"
    assert conn.execute("SELECT thumb, feedback FROM draft_outcomes WHERE draft_id=?", (d,)).fetchone() == (
        None,
        "x" * 300,
    )
    assert logged_in.post(url, json={"thumb": -1}).status_code == 204
    assert conn.execute("SELECT thumb, feedback FROM draft_outcomes WHERE draft_id=?", (d,)).fetchone() == (-1, None)


def test_feedback_needs_login_and_origin(client) -> None:
    assert client.post("/api/drafts/1/feedback", json={"thumb": 1}).status_code == 401


def test_feedback_bad_origin(logged_in) -> None:
    r = logged_in.post("/api/drafts/1/feedback", json={"thumb": 1}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_send_route_takes_a_draft_hint_and_never_changes_the_send(logged_in, monkeypatch) -> None:
    sent: list = []

    def send_message(recipient, message, quoted_message_id=None, idempotency_key=None):  # noqa: ANN001, ANN202
        sent.append((recipient, message))
        return {"success": True, "message_id": "OUT1"}

    monkeypatch.setattr(mcp_lib, "send_message", send_message)
    learning._hints.clear()
    key = "11111111-1111-4111-8111-111111111111"
    r = logged_in.post(f"/api/chats/{ANA}/send", json={"text": "Može!", "client_key": key, "draft_id": 7})
    assert r.status_code == 200 and sent == [(ANA, "Može!")]
    assert learning._hints[ANA][0] == 7
    for bad in ("7", True, 1.5, [7]):
        r = logged_in.post(f"/api/chats/{ANA}/send", json={"text": "x", "client_key": key, "draft_id": bad})
        assert r.status_code == 422, bad
    assert len(sent) == 1
    learning._hints.clear()

    def failing(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return {"success": False, "retryable": True}

    monkeypatch.setattr(mcp_lib, "send_message", failing)
    assert (
        logged_in.post(f"/api/chats/{ANA}/send", json={"text": "Može!", "client_key": key, "draft_id": 7}).status_code
        == 502
    )
    assert ANA not in learning._hints  # a failed send leaves no hint behind


# --- any own message makes the live draft stale -----------------------------------------------------------------


def _own_event(mid, **message) -> live.Event:
    return live.Event(
        "message", {"chat_jid": ANA, "message": {"id": mid, "from_me": True, "timestamp_iso": _ts(0), **message}}
    )


def test_own_text_outside_window_clears_card_without_outcome(settings) -> None:
    async def scenario(rig):
        rig.setup()
        d = make_draft(rig.appconn, ready_ago=20 * 60)
        rig.events()
        await feed(rig, _own_event("M1", text="Može!"))
        assert status_of(rig.appconn, d) == "superseded"
        assert outcome(rig.appconn, d) is None and counter(rig.appconn) == 0
        assert rig.events() == [{"chat_jid": ANA, "draft": None}]
        await feed(rig, _own_event("M2", text="Opet"))  # nothing left to change: nothing published
        assert rig.events() == []

    _run(settings, scenario)


def test_media_only_own_message_clears_card(settings) -> None:
    async def scenario(rig):
        rig.setup()
        d = make_draft(rig.appconn)
        rig.events()
        await feed(rig, _own_event("M1"))
        assert status_of(rig.appconn, d) == "superseded" and outcome(rig.appconn, d) is None
        assert rig.events() == [{"chat_jid": ANA, "draft": None}]

    _run(settings, scenario)


def test_failed_draft_cleared_by_own_message(settings) -> None:
    async def scenario(rig):
        rig.setup()
        d = make_draft(rig.appconn, status="failed")
        rig.events()
        await feed(rig, _own_event("M1", text="Evo me"))
        assert status_of(rig.appconn, d) == "superseded"
        assert rig.events() == [{"chat_jid": ANA, "draft": None}]

    _run(settings, scenario)


def test_failed_distill_is_not_retried_by_later_outcomes(settings) -> None:
    from wa_app.ai.backends import BackendError

    async def scenario(rig):
        rig.setup()
        rig.fake.error = BackendError("down")
        for i in range(12):
            make_draft(rig.appconn, alts=("Može!",))
            await feed(rig, _own(f"M{i}", "Može!"))
        assert len(rig.fake.calls) == 1  # only the crossing to 10 asked the model
        assert counter(rig.appconn) == 12

    _run(settings, scenario)


def test_rejected_hint_is_kept_and_stale_feedback_pruned(settings) -> None:
    conn = _appconn(settings)
    learning._hints.clear()
    old = make_draft(conn, status="dismissed", ready_ago=20 * 60)
    learning.set_hint(ANA, old)
    assert learning.record_own_message(conn, ANA, "S1", "x", time.time()) is None
    assert learning._hints[ANA][0] == old
    learning._hints.clear()
    learning.set_feedback(conn, old, 1, None)
    fresh = make_draft(conn)
    learning.set_feedback(conn, fresh, 1, None)
    conn.execute("UPDATE drafts SET created_at=? WHERE id=?", (int(time.time()) - 25 * 3600, old))
    learning.prune_feedback(conn, time.time())
    keys = {r[0] for r in conn.execute("SELECT key FROM meta WHERE key LIKE 'draft_feedback:%'")}
    assert keys == {f"draft_feedback:{fresh}"}


def test_old_own_row_does_not_cancel_a_newer_draft(settings) -> None:
    async def scenario(rig):
        rig.setup()
        rig.visible_now = 0  # closed app: the job waits in its debounce
        rig.sleep_gate = asyncio.Event()
        _insert(settings, ANA, "N1")
        rig.worker.on_events([_event(ANA, "N1")])
        await until(lambda: rig.sleeps == [20.0])
        assert rig.sleeps == [20.0] and rig.rows() == []
        rig.events()
        old = live.Event("message", {"chat_jid": ANA, "message": {
            "id": "OLD", "from_me": True, "text": "Stara poruka", "timestamp_iso": _ts(60)}})  # fmt: skip
        await feed_nowait(rig, old)
        assert rig.events() == []
        rig.sleep_gate.set()
        await rig.worker.idle()
        assert rig.rows() == [("ready", None)] and len(rig.fake.calls) == 1
        assert rig.events()[-1]["draft"]["status"] == "ready"

    _run(settings, scenario)


async def feed_nowait(rig, *events) -> None:
    rig.worker.on_events(list(events))
    await asyncio.sleep(0.05)


def test_drafts_older_than_90_days_are_pruned_with_their_outcomes(settings) -> None:
    conn = _appconn(settings)
    now = time.time()
    old = make_draft(conn, now=now - 91 * 86400)
    fresh = make_draft(conn, now=now - 3600)
    for d in (old, fresh):
        conn.execute(
            "INSERT INTO draft_outcomes (draft_id, chat_jid, sent_id, sent_text, best_alternative, ratio, kind, at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (d, ANA, f"s{d}", "x", "y", 0.5, "edited", 1),
        )
    learning.prune_old_drafts(conn, now)
    assert [r[0] for r in conn.execute("SELECT id FROM drafts")] == [fresh]
    assert [r[0] for r in conn.execute("SELECT draft_id FROM draft_outcomes")] == [fresh]


def test_distill_rechecks_local_only_after_the_slot(settings) -> None:
    async def scenario(rig):
        rig.setup(backend=CLOUD, rules="- Jedan")
        seed_outcomes(rig.appconn, 3)
        for _ in range(3):
            await rig.worker._cloud.acquire()
        rig.worker._schedule_distill(ANA)
        await asyncio.sleep(0.1)
        ai_config.save_chat_ai(rig.appconn, ANA, {"local_only": True})
        for _ in range(3):
            rig.worker._cloud.release()
        await rig.worker.idle()
        assert rig.fake.calls == []
        assert ai_config.chat_ai(rig.appconn, ANA).rules == "- Jedan"

    _run(settings, scenario)

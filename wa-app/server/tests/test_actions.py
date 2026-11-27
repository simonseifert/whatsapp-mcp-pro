"""Message actions: edit, delete for everyone, star, forward and the starred list (the bridge is always patched)."""

import sqlite3
import uuid
from datetime import datetime, timedelta

import pytest
from starlette.testclient import TestClient

from tests import conftest as fx
from wa_app import mcp_lib

KEY = "2f6f0c1e-8a53-4d0e-9a53-1b1c9a3a0001"
LID_SENDER = "555@lid"


def db(settings) -> sqlite3.Connection:  # noqa: ANN001
    return sqlite3.connect(settings.store_path / "messages.db")


def add_message(settings, chat: str, seconds_ago: float, content: str | None = "hello", **kw: object) -> str:  # noqa: ANN001
    mid = str(kw.get("mid") or f"A{uuid.uuid4().hex[:8]}")
    stamp = (datetime.now().astimezone().replace(microsecond=0) - timedelta(seconds=seconds_ago)).isoformat(sep=" ")
    conn = db(settings)
    conn.execute(
        "INSERT INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, media_type, filename)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            mid, chat, kw.get("sender"), kw.get("sender_name"), content, stamp, 1 if kw.get("from_me") else 0,
            kw.get("media_type"), kw.get("filename"),
        ),
    )  # fmt: skip
    conn.commit()
    conn.close()
    return mid


def add_star(settings, chat: str, mid: str, starred_at: str) -> None:  # noqa: ANN001
    conn = db(settings)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS starred_messages (chat_jid TEXT NOT NULL, message_id TEXT NOT NULL,"
        " starred_at TIMESTAMP, PRIMARY KEY (chat_jid, message_id))"
    )
    conn.execute("INSERT INTO starred_messages VALUES (?,?,?)", (chat, mid, starred_at))
    conn.commit()
    conn.close()


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict] = []
    answers: dict[str, dict] = {}

    def make(kind: str):  # noqa: ANN202
        def fn(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            calls.append({"kind": kind, "args": args, **kwargs})
            return dict(answers.get(kind, {"success": True, "message_id": "NEW"}))

        return fn

    for name in ("edit_message", "delete_message", "star_message", "send_message", "send_file"):
        monkeypatch.setattr(mcp_lib, name, make(name), raising=False)
    return calls, answers


def edit(client: TestClient, chat: str, mid: str, **body: object):  # noqa: ANN201
    body.setdefault("text", "fixed")
    body.setdefault("client_key", KEY)
    return client.post(f"/api/chats/{chat}/messages/{mid}/edit", json=body)


def revoke(client: TestClient, chat: str, mid: str):  # noqa: ANN201
    return client.post(f"/api/chats/{chat}/messages/{mid}/revoke", json={"client_key": KEY})


# --- edit ---------------------------------------------------------------------------------------------------------


def test_edit_at_14_59_works_and_goes_to_the_bridge(logged_in, settings, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    mid = add_message(settings, fx.ANA, 14 * 60 + 59, from_me=True)
    response = edit(logged_in, fx.ANA, mid, text="  fixed  ")
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert calls == [{"kind": "edit_message", "args": (fx.ANA, mid, "fixed")}]


def test_edit_at_15_01_is_refused_with_409_too_late(logged_in, settings, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    mid = add_message(settings, fx.ANA, 15 * 60 + 1, from_me=True)
    response = edit(logged_in, fx.ANA, mid)
    assert response.status_code == 409 and response.json() == {"error": "too_late"}
    assert calls == []


def test_edit_refuses_what_is_not_an_own_plain_text(logged_in, settings, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    theirs = add_message(settings, fx.ANA, 60, sender=fx.ANA)
    photo = add_message(settings, fx.ANA, 60, "", from_me=True, media_type="image", filename="a.jpg")
    for mid in (theirs, photo, "nope"):
        assert edit(logged_in, fx.ANA, mid).status_code in (404, 422)
    assert edit(logged_in, fx.ANA, add_message(settings, fx.ANA, 60, from_me=True), text="   ").status_code == 422
    assert edit(logged_in, fx.ANA, add_message(settings, fx.ANA, 60, from_me=True), client_key="x").status_code == 422
    assert calls == []


def test_edit_uses_the_form_the_message_is_stored_under(logged_in, settings, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    mid = add_message(settings, fx.TOMO_LID, 30, from_me=True)
    assert edit(logged_in, fx.TOMO_PHONE, mid).status_code == 200  # the route names the phone form
    assert calls[0]["args"][0] == fx.TOMO_LID


def test_edit_bridge_failure_maps_like_a_send(logged_in, settings, bridge) -> None:  # noqa: ANN001
    _, answers = bridge
    answers["edit_message"] = {"success": False, "http_status": 429, "code": "rate_limited", "retryable": True}
    mid = add_message(settings, fx.ANA, 30, from_me=True)
    response = edit(logged_in, fx.ANA, mid)
    assert response.status_code == 502 and response.json()["error"] == "Not sent: limit"


# --- revoke -------------------------------------------------------------------------------------------------------


def test_revoke_at_47_59_works_at_48_01_is_too_late(logged_in, settings, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    early = add_message(settings, fx.ANA, 48 * 3600 - 61, from_me=True)
    late = add_message(settings, fx.ANA, 48 * 3600 + 1, from_me=True)
    ok = revoke(logged_in, fx.ANA, early)
    assert ok.status_code == 200 and ok.json() == {"ok": True}
    refused = revoke(logged_in, fx.ANA, late)
    assert refused.status_code == 409 and refused.json() == {"error": "too_late"}
    assert calls == [{"kind": "delete_message", "args": (fx.ANA, early)}]


def test_revoke_is_for_own_messages_only(logged_in, settings, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    theirs = add_message(settings, fx.EKIPA, 60, sender=LID_SENDER)
    assert revoke(logged_in, fx.EKIPA, theirs).status_code == 422
    assert calls == []


def test_revoking_an_already_deleted_own_message_is_a_quiet_success(logged_in, settings, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    conn = db(settings)
    mid = add_message(settings, fx.ANA, 60, from_me=True)
    conn.execute("UPDATE messages SET deleted_at = datetime('now') WHERE id = ?", (mid,))
    conn.commit()
    res = revoke(logged_in, fx.ANA, mid)
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    assert calls == []  # a retry after a lost answer must not send anything again


def test_revoking_someone_elses_deleted_message_is_still_refused(logged_in, settings, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    conn = db(settings)
    mid = add_message(settings, fx.EKIPA, 60, from_me=False, sender=LID_SENDER)
    conn.execute("UPDATE messages SET deleted_at = datetime('now') WHERE id = ?", (mid,))
    conn.commit()
    assert revoke(logged_in, fx.EKIPA, mid).status_code == 422
    assert calls == []


# --- star ---------------------------------------------------------------------------------------------------------


def star(client: TestClient, chat: str, mid: str, value: object = True):  # noqa: ANN201
    return client.post(f"/api/chats/{chat}/messages/{mid}/star", json={"star": value})


def test_star_a_lid_sender_in_a_group_passes_the_stored_forms(logged_in, settings, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    mid = add_message(settings, fx.EKIPA, 60, sender=LID_SENDER, sender_name="Zed")
    response = star(logged_in, fx.EKIPA, mid)
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert calls == [
        {"kind": "star_message", "args": (fx.EKIPA, mid), "star": True, "sender_jid": LID_SENDER, "from_me": False}
    ]


def test_star_own_message_and_unstar(logged_in, settings, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    mid = add_message(settings, fx.ANA, 60, from_me=True)
    assert star(logged_in, fx.ANA, mid, False).status_code == 200
    assert calls == [
        {"kind": "star_message", "args": (fx.ANA, mid), "star": False, "sender_jid": None, "from_me": True}
    ]


def test_star_resolves_the_chat_form_like_send_does(logged_in, settings, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    mid = add_message(settings, fx.TOMO_LID, 30, sender=fx.TOMO_LID)
    assert star(logged_in, fx.TOMO_PHONE, mid).status_code == 200
    assert calls[0]["args"] == (fx.TOMO_LID, mid)


def test_star_validation(logged_in, settings, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    assert star(logged_in, fx.ANA, "missing").status_code == 404
    assert star(logged_in, "nobody@s.whatsapp.net", "x").status_code == 404
    mid = add_message(settings, fx.ANA, 60, sender=fx.ANA)
    assert star(logged_in, fx.ANA, mid, "yes").status_code == 422
    assert calls == []


def test_starred_flag_shows_in_messages_and_lists(logged_in, settings, bridge) -> None:  # noqa: ANN001
    mid = add_message(settings, fx.ANA, 60, sender=fx.ANA)
    other = add_message(settings, fx.ANA, 50, sender=fx.ANA)
    add_star(settings, fx.ANA, mid, "2026-11-20 10:00:00+00:00")
    by_id = {m["id"]: m for m in logged_in.get(f"/api/chats/{fx.ANA}/messages").json()["messages"]}
    assert by_id[mid]["starred"] is True and by_id[other]["starred"] is False


def test_star_publishes_a_live_change(logged_in, settings, bridge, monkeypatch) -> None:  # noqa: ANN001
    from wa_app import live

    seen: list[tuple[str, dict]] = []
    hub = live.current_hub()
    original = hub.publish

    def spy(name: str, data: dict):  # noqa: ANN202
        seen.append((name, data))
        return original(name, data)

    monkeypatch.setattr(hub, "publish", spy)
    mid = add_message(settings, fx.ANA, 60, sender=fx.ANA)
    star(logged_in, fx.ANA, mid)
    assert ("message_changed", {"chat_jid": fx.ANA, "id": mid}) in seen


# --- starred list -------------------------------------------------------------------------------------------------


def test_starred_list_is_empty_on_an_older_store(logged_in) -> None:  # noqa: ANN001
    response = logged_in.get("/api/starred")
    assert response.status_code == 200 and response.json() == {"items": [], "next": None}


def test_starred_list_newest_star_first_with_chat_names_and_paging(logged_in, settings) -> None:  # noqa: ANN001
    ids = [add_message(settings, fx.ANA, 100 + n, f"msg {n}", sender=fx.ANA) for n in range(3)]
    add_star(settings, fx.ANA, ids[0], "2026-11-20 10:00:00+00:00")
    add_star(settings, fx.ANA, ids[1], "2026-11-22 10:00:00+00:00")
    add_star(settings, fx.ANA, ids[2], "2026-11-21 10:00:00+00:00")
    body = logged_in.get("/api/starred").json()
    assert [i["message"]["id"] for i in body["items"]] == [ids[1], ids[2], ids[0]]
    assert body["items"][0]["chat_name"] == "Ana Kovačević" and body["items"][0]["message"]["starred"] is True
    assert body["next"] is None
    # paging: the cursor of an item continues after it
    first = logged_in.get("/api/starred?limit=2").json()
    assert [i["message"]["id"] for i in first["items"]] == [ids[1], ids[2]] and first["next"]
    rest = logged_in.get(f"/api/starred?limit=2&before={first['next']}").json()
    assert [i["message"]["id"] for i in rest["items"]] == [ids[0]] and rest["next"] is None
    assert logged_in.get("/api/starred?before=junk").status_code == 400


def test_starred_list_skips_deleted_messages(logged_in, settings) -> None:  # noqa: ANN001
    mid = add_message(settings, fx.ANA, 100, "secret", sender=fx.ANA)
    add_star(settings, fx.ANA, mid, "2026-11-20 10:00:00+00:00")
    conn = db(settings)
    conn.execute("UPDATE messages SET deleted_at = datetime('now') WHERE id = ?", (mid,))
    conn.commit()
    assert logged_in.get("/api/starred").json()["items"] == []


# --- forward ------------------------------------------------------------------------------------------------------


def forward(client: TestClient, **body: object):  # noqa: ANN201
    body.setdefault("client_key", KEY)
    return client.post("/api/forward", json=body)


def test_forward_text_goes_out_forwarded_to_every_target(logged_in, settings, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    mid = add_message(settings, fx.EKIPA, 60, "see you", sender=LID_SENDER, sender_name="Zed")
    response = forward(logged_in, from_chat=fx.EKIPA, message_ids=[mid], to=[fx.ANA, fx.LUKAS])
    assert response.status_code == 200
    assert response.json()["results"] == [{"to": fx.ANA, "ok": True}, {"to": fx.LUKAS, "ok": True}]
    sends = [c for c in calls if c["kind"] == "send_message"]
    assert [(c["args"][0], c["args"][1], c["forwarded"]) for c in sends] == [
        (fx.ANA, "see you", True),
        (fx.LUKAS, "see you", True),
    ]
    keys = {c["idempotency_key"] for c in sends}
    assert len(keys) == 2 and KEY not in keys  # one key per (target, message), derived from the client key


def test_forward_retry_reuses_the_same_per_target_keys(logged_in, settings, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    mid = add_message(settings, fx.EKIPA, 60, "see you", sender=fx.ANA)
    forward(logged_in, from_chat=fx.EKIPA, message_ids=[mid], to=[fx.ANA])
    forward(logged_in, from_chat=fx.EKIPA, message_ids=[mid], to=[fx.ANA])
    assert calls[0]["idempotency_key"] == calls[1]["idempotency_key"]


def test_forward_reports_each_target_and_never_hides_a_partial_failure(logged_in, settings, monkeypatch) -> None:  # noqa: ANN001
    def send(recipient, message, **kw):  # noqa: ANN001, ANN003, ANN202
        if recipient == fx.LUKAS:
            return {"success": False, "http_status": 429, "code": "rate_limited", "retryable": True}
        return {"success": True, "message_id": "N"}

    monkeypatch.setattr(mcp_lib, "send_message", send)
    mid = add_message(settings, fx.EKIPA, 60, "see you", sender=fx.ANA)
    body = forward(
        logged_in, from_chat=fx.EKIPA, message_ids=[mid], to=[fx.ANA, fx.LUKAS, "nobody@s.whatsapp.net"]
    ).json()
    assert body["results"] == [
        {"to": fx.ANA, "ok": True},
        {"to": fx.LUKAS, "ok": False, "error": "Not sent: limit"},
        {"to": "nobody@s.whatsapp.net", "ok": False, "error": "Chat not found"},
    ]


def test_forward_stops_a_target_at_its_first_failure_and_says_how_far_it_got(logged_in, settings, monkeypatch) -> None:  # noqa: ANN001
    sent: list[str] = []

    def send(recipient, message, **kw):  # noqa: ANN001, ANN003, ANN202
        if message == "two":
            return {"success": False, "http_status": 403, "code": "forbidden"}
        sent.append(message)
        return {"success": True}

    monkeypatch.setattr(mcp_lib, "send_message", send)
    ids = [
        add_message(settings, fx.EKIPA, 60 - n, text, sender=fx.ANA) for n, text in enumerate(["one", "two", "three"])
    ]
    result = forward(logged_in, from_chat=fx.EKIPA, message_ids=ids, to=[fx.ANA]).json()["results"][0]
    assert sent == ["one"] and result["ok"] is False
    assert result["error"] == "Sent 1 of 3, then: Not sent: this chat isn't allowed"


def test_forward_media_downloads_first_then_sends_the_file_forwarded(logged_in, settings, bridge, monkeypatch) -> None:  # noqa: ANN001
    calls, _ = bridge
    folder = settings.store_path / fx.EKIPA.replace("@", "_")
    folder.mkdir(exist_ok=True)
    (folder / "image_route.jpg").write_bytes(b"jpg")
    asked: list[tuple[str, str, bool]] = []

    def download(message_id, chat_jid, *, no_phone_retry):  # noqa: ANN001, ANN202
        asked.append((message_id, chat_jid, no_phone_retry))
        calls.append({"kind": "download"})
        return str(folder / "image_route.jpg")

    monkeypatch.setattr(mcp_lib, "download_media", download)
    response = forward(logged_in, from_chat=fx.EKIPA, message_ids=["E05"], to=[fx.ANA, fx.LUKAS])
    assert response.json()["results"] == [{"to": fx.ANA, "ok": True}, {"to": fx.LUKAS, "ok": True}]
    assert asked == [("E05", fx.EKIPA, True)]  # downloaded once, never pings the phone
    kinds = [c["kind"] for c in calls]
    assert kinds == ["download", "send_file", "send_file"]
    sent = calls[1]
    assert sent["file_path"] == str(folder / "image_route.jpg") and sent["forwarded"] is True
    assert sent["caption"] == "Ovo je ruta, 11 km, cca 4h s pauzama" and sent["args"] == (fx.ANA,)


def test_forward_media_that_cannot_be_downloaded_fails_every_target_and_sends_nothing(
    logged_in,
    bridge,
    monkeypatch,  # noqa: ANN001
) -> None:
    calls, _ = bridge
    monkeypatch.setattr(mcp_lib, "download_media", lambda *a, **k: None)
    body = forward(logged_in, from_chat=fx.EKIPA, message_ids=["E05"], to=[fx.ANA, fx.LUKAS]).json()
    assert [r["ok"] for r in body["results"]] == [False, False]
    assert all("download" in r["error"].lower() for r in body["results"])
    assert [c for c in calls if c["kind"] in ("send_file", "send_message")] == []


def test_forward_validation(logged_in, settings, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    mid = add_message(settings, fx.EKIPA, 60, "hi", sender=fx.ANA)
    targets = [f"38591000010{n}@s.whatsapp.net" for n in range(6)]
    assert forward(logged_in, from_chat=fx.EKIPA, message_ids=[mid], to=targets).status_code == 422
    assert (
        forward(logged_in, from_chat=fx.EKIPA, message_ids=[f"x{n}" for n in range(11)], to=[fx.ANA]).status_code == 422
    )
    assert forward(logged_in, from_chat=fx.EKIPA, message_ids=[], to=[fx.ANA]).status_code == 422
    assert forward(logged_in, from_chat=fx.EKIPA, message_ids=[mid], to=[]).status_code == 422
    assert forward(logged_in, from_chat=fx.EKIPA, message_ids=[mid], to=[fx.ANA], client_key="x").status_code == 422
    assert forward(logged_in, from_chat="nobody@s.whatsapp.net", message_ids=[mid], to=[fx.ANA]).status_code == 404
    assert calls == []


def test_forward_refuses_polls_deleted_and_imported_rows_per_target(logged_in, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    for mid in ("E06", "E04", "E15"):  # poll, deleted, system
        body = forward(logged_in, from_chat=fx.EKIPA, message_ids=[mid], to=[fx.ANA]).json()
        assert body["results"][0]["ok"] is False
    assert calls == []


def test_forward_refuses_contact_cards(logged_in, settings, bridge) -> None:  # noqa: ANN001
    calls, _ = bridge
    mid = add_message(settings, fx.ANA, 60, "[Contact: Ivo Horvat]\n📞 +385 91 000 0777", from_me=True)
    body = forward(logged_in, from_chat=fx.ANA, message_ids=[mid], to=[fx.EKIPA]).json()
    assert body["results"][0]["ok"] is False
    assert calls == []

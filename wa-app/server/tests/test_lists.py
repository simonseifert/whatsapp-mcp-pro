"""Custom lists and the mentions filter."""

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests import conftest as fx
from wa_app import appdb, lists, store
from wa_app.config import Settings


@pytest.fixture
def conns(settings: Settings) -> Iterator[tuple[sqlite3.Connection, sqlite3.Connection]]:
    ro = store.open_ro(settings)
    app = appdb.connect(settings)
    yield ro, app
    ro.close()
    app.close()


def jids(chats) -> list[str]:
    return [c.jid for c in chats]


# --- CRUD and limits ------------------------------------------------------------------------------------------------


def test_create_rename_reorder_delete(logged_in: TestClient) -> None:
    a = logged_in.post("/api/lists", json={"name": "Work"}).json()
    b = logged_in.post("/api/lists", json={"name": "Family"}).json()
    assert [row["name"] for row in logged_in.get("/api/lists").json()["lists"]] == ["Work", "Family"]
    assert logged_in.put(f"/api/lists/{b['id']}", json={"position": 0}).status_code == 200
    assert [row["name"] for row in logged_in.get("/api/lists").json()["lists"]] == ["Family", "Work"]
    assert logged_in.put(f"/api/lists/{a['id']}", json={"name": "Office"}).status_code == 200
    assert [row["name"] for row in logged_in.get("/api/lists").json()["lists"]] == ["Family", "Office"]
    assert logged_in.delete(f"/api/lists/{b['id']}").status_code == 200
    rows = logged_in.get("/api/lists").json()["lists"]
    assert [(row["name"], row["position"]) for row in rows] == [("Office", 0)]
    assert logged_in.delete(f"/api/lists/{b['id']}").status_code == 404
    assert logged_in.put(f"/api/lists/{b['id']}", json={"name": "x"}).status_code == 404


def test_limits(logged_in: TestClient) -> None:
    assert logged_in.post("/api/lists", json={"name": "x" * 41}).status_code == 422
    assert logged_in.post("/api/lists", json={"name": "   "}).status_code == 422
    assert logged_in.post("/api/lists", json={"name": "x" * 40}).status_code == 200
    assert logged_in.post("/api/lists", json={"name": "X" * 40}).status_code == 409  # duplicate, any case
    for n in range(19):
        assert logged_in.post("/api/lists", json={"name": f"List {n}"}).status_code == 200
    assert logged_in.post("/api/lists", json={"name": "One too many"}).status_code == 409
    first = logged_in.get("/api/lists").json()["lists"][0]
    assert logged_in.put(f"/api/lists/{first['id']}", json={"name": "list 3"}).status_code == 409


def test_lists_need_a_session(client: TestClient) -> None:
    assert client.get("/api/lists").status_code == 401
    assert client.post("/api/lists", json={"name": "x"}).status_code in (401, 403)


# --- membership -----------------------------------------------------------------------------------------------------


def make_list(conn: sqlite3.Connection, name: str = "Work") -> int:
    return int(lists.create(conn, name)["id"])


def test_filter_shows_members_only(conns) -> None:
    ro, app = conns
    lid = make_list(app)
    lists.change_chats(app, ro, lid, [fx.ANA, fx.EKIPA], [])
    assert set(jids(store.list_chats(ro, app, filter=f"list:{lid}"))) == {fx.ANA, fx.EKIPA}
    lists.change_chats(app, ro, lid, [], [fx.ANA])
    assert jids(store.list_chats(ro, app, filter=f"list:{lid}")) == [fx.EKIPA]


def test_membership_survives_form_change(conns) -> None:
    ro, app = conns
    lid = make_list(app)
    lists.change_chats(app, ro, lid, [fx.TOMO_LID], [])  # added through the LID form
    assert app.execute("SELECT chat_jid FROM list_chats").fetchall() == [(fx.TOMO_PHONE,)]  # stored canonical
    # The list row says phone form; the chat is now reached as LID only: a stale row under either form still matches.
    app.execute("UPDATE list_chats SET chat_jid = ?", (fx.TOMO_LID,))
    assert jids(store.list_chats(ro, app, filter=f"list:{lid}")) == [fx.TOMO_PHONE]
    assert lists.lists_of(app, ro, fx.TOMO_PHONE) == {lid}
    lists.change_chats(app, ro, lid, [], [fx.TOMO_PHONE])  # removing by the other form clears it
    assert app.execute("SELECT COUNT(*) FROM list_chats").fetchone()[0] == 0


def test_archived_chat_shows_in_its_list(conns) -> None:
    ro, app = conns
    lid = make_list(app)
    lists.change_chats(app, ro, lid, [fx.OLD_ARCHIVED, fx.ANA], [])
    assert fx.OLD_ARCHIVED not in jids(store.list_chats(ro, app))  # the normal list hides it
    shown = store.list_chats(ro, app, filter=f"list:{lid}")
    assert set(jids(shown)) == {fx.OLD_ARCHIVED, fx.ANA}


def test_delete_list_drops_members(conns) -> None:
    ro, app = conns
    lid = make_list(app)
    lists.change_chats(app, ro, lid, [fx.ANA], [])
    lists.delete(app, lid)
    assert app.execute("SELECT COUNT(*) FROM list_chats").fetchone()[0] == 0


def test_chat_routes(logged_in: TestClient) -> None:
    lid = logged_in.post("/api/lists", json={"name": "Work"}).json()["id"]
    r = logged_in.put(f"/api/lists/{lid}/chats", json={"add": [fx.ANA, fx.OLD_ARCHIVED], "remove": []})
    assert r.status_code == 200
    shown = logged_in.get("/api/chats", params={"filter": f"list:{lid}"}).json()["chats"]
    assert {c["jid"] for c in shown} == {fx.ANA, fx.OLD_ARCHIVED}
    rows = logged_in.get("/api/lists", params={"chat": fx.ANA}).json()["lists"]
    assert [(row["id"], row["member"]) for row in rows] == [(lid, True)]
    assert logged_in.get("/api/lists", params={"chat": fx.GYM}).json()["lists"][0]["member"] is False
    assert logged_in.put(f"/api/lists/{lid}/chats", json={"add": "nope"}).status_code == 422
    assert logged_in.put("/api/lists/999/chats", json={"add": [fx.ANA]}).status_code == 404
    assert logged_in.get("/api/chats", params={"filter": "list:999"}).status_code == 400
    assert logged_in.get("/api/chats", params={"filter": "list:abc"}).status_code == 400


# --- chips ----------------------------------------------------------------------------------------------------------


def test_chip_order(logged_in: TestClient) -> None:
    a = logged_in.post("/api/lists", json={"name": "Work"}).json()["id"]
    b = logged_in.post("/api/lists", json={"name": "Family"}).json()["id"]
    logged_in.put(f"/api/lists/{b}", json={"position": 0})
    chips = logged_in.get("/api/chat-filters").json()["filters"]
    assert [c["id"] for c in chips] == ["all", "unread", "favourites", "groups", "mentions", f"list:{b}", f"list:{a}"]
    assert [c["label"] for c in chips][-2:] == ["Family", "Work"]


# --- mentions and replies -------------------------------------------------------------------------------------------


def _stamp(minutes_ago: float) -> str:
    return (datetime.now().astimezone().replace(microsecond=0) - timedelta(minutes=minutes_ago)).isoformat(sep=" ")


def _group_with(settings: Settings, jid: str, rows: list[dict[str, Any]], read_minutes_ago: float = 60) -> None:
    db = sqlite3.connect(settings.store_path / "messages.db")
    db.execute("INSERT INTO chats (jid, name, last_message_time) VALUES (?,?,?)", (jid, "G " + jid[-4:], _stamp(1)))
    db.execute("INSERT INTO chat_state (chat_jid, last_read_at) VALUES (?,?)", (jid, _stamp(read_minutes_ago)))
    for n, row in enumerate(rows):
        db.execute(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, quoted_message_id,"
            " mentioned_jids) VALUES (?,?,?,?,?,?,?,?)",
            (
                row.get("id", f"{jid[-4:]}{n}"), jid, row.get("sender", "385910000020@s.whatsapp.net"),
                row.get("content", "hi"), _stamp(float(row.get("ago", 10 - n))), 1 if row.get("mine") else 0,
                row.get("quoted"), json.dumps(row["mentions"]) if "mentions" in row else None,
            ),
        )  # fmt: skip
    db.commit()
    db.close()


def mention_chats(settings: Settings) -> set[str]:
    ro, app = store.open_ro(settings), appdb.connect(settings)
    try:
        return set(jids(store.list_chats(ro, app, filter="mentions")))
    finally:
        ro.close()
        app.close()


G = "120363000000000101@g.us"


def test_mention_of_me_phone_form(settings: Settings) -> None:
    _group_with(settings, G, [{"content": "@Me look", "mentions": [fx.ME]}])
    assert mention_chats(settings) == {G}


def test_mention_of_me_lid_form(settings: Settings) -> None:
    _group_with(settings, G, [{"content": "@Me look", "mentions": [fx.ME_LID]}])
    assert mention_chats(settings) == {G}


def test_mention_of_someone_else_is_not_one(settings: Settings) -> None:
    _group_with(settings, G, [{"mentions": ["385910000555@s.whatsapp.net"]}, {"content": "plain"}])
    assert mention_chats(settings) == set()


def test_reply_to_my_message(settings: Settings) -> None:
    _group_with(settings, G, [{"id": "mine1", "mine": True, "ago": 30, "sender": fx.ME}, {"quoted": "mine1"}])
    assert mention_chats(settings) == {G}


def test_reply_to_someone_elses_message_is_not_one(settings: Settings) -> None:
    _group_with(settings, G, [{"id": "theirs", "ago": 30}, {"quoted": "theirs"}])
    assert mention_chats(settings) == set()


@pytest.mark.parametrize("text", ["@all dinner at 8", "hey @Everyone", "@ALL"])
def test_at_all(settings: Settings, text: str) -> None:
    _group_with(settings, G, [{"content": text}])
    assert mention_chats(settings) == {G}


def test_at_all_inside_a_word_is_not_one(settings: Settings) -> None:
    _group_with(settings, G, [{"content": "mail@allstate.com and @allan"}])
    assert mention_chats(settings) == set()


def test_read_mention_does_not_count(settings: Settings) -> None:
    _group_with(settings, G, [{"mentions": [fx.ME], "ago": 90}], read_minutes_ago=60)
    assert mention_chats(settings) == set()


def test_my_own_message_and_private_chats_never_match(settings: Settings) -> None:
    _group_with(settings, G, [{"mine": True, "sender": fx.ME, "content": "@all", "mentions": [fx.ME]}])
    assert mention_chats(settings) == set()


def test_without_the_mentions_column(settings: Settings) -> None:
    db = sqlite3.connect(settings.store_path / "messages.db")
    db.execute("ALTER TABLE messages DROP COLUMN mentioned_jids")
    db.commit()
    db.close()
    _group_with_no_column(settings)
    assert mention_chats(settings) == {G}  # the reply still counts; no crash


def _group_with_no_column(settings: Settings) -> None:
    db = sqlite3.connect(settings.store_path / "messages.db")
    db.execute("INSERT INTO chats (jid, name, last_message_time) VALUES (?,?,?)", (G, "G", _stamp(1)))
    db.execute("INSERT INTO chat_state (chat_jid, last_read_at) VALUES (?,?)", (G, _stamp(60)))
    db.execute(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES ('m1',?,?,?,?,1)",
        (G, fx.ME, "mine", _stamp(30)),
    )
    db.execute(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, quoted_message_id)"
        " VALUES ('m2',?,?,?,?,0,'m1')",
        (G, "385910000020@s.whatsapp.net", "re", _stamp(5)),
    )
    db.commit()
    db.close()


def test_mentions_api(logged_in: TestClient, settings: Settings) -> None:
    _group_with(settings, G, [{"mentions": [fx.ME]}])
    chats = logged_in.get("/api/chats", params={"filter": "mentions"}).json()["chats"]
    assert [c["jid"] for c in chats] == [G]

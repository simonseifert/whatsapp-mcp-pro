"""History coverage: how much of the user's own writing is stored per chat."""

import sqlite3
from datetime import datetime, timedelta

import pytest
from starlette.testclient import TestClient

from tests.conftest import ANA, CHANNEL, TOMO_LID, TOMO_PHONE
from wa_app import appdb, history, store
from wa_app.main import create_app

NEW_CHAT = "385910000501@s.whatsapp.net"
EMPTY_CHAT = "385910000502@s.whatsapp.net"
_NOW = datetime.now().astimezone().replace(microsecond=0)


def _ts(minutes_ago: float) -> str:
    return (_NOW - timedelta(minutes=minutes_ago)).isoformat(sep=" ")


@pytest.fixture
def db(settings):
    conn = sqlite3.connect(settings.store_path / "messages.db")
    yield conn
    conn.close()


@pytest.fixture
def conns(settings, db):
    """Read-only store and app db; open lazily so a test's inserts (committed first) are visible."""
    db.commit()
    ro = store.open_ro(settings)
    yield ro, appdb.connect(settings)
    ro.close()


def _chat(db, jid: str, last: float | None = 1) -> None:
    db.execute(
        "INSERT INTO chats (jid, name, last_message_time) VALUES (?,?,?)",
        (jid, jid.split("@")[0], _ts(last) if last else None),
    )


def _msgs(db, jid: str, n: int, *, own: bool, tag: str = "x", content: str = "hello", **kw: object) -> None:
    for i in range(n):
        db.execute(
            "INSERT INTO messages (id, chat_jid, content, timestamp, is_from_me, media_type, deleted_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (f"{jid}-{tag}-{i}", jid, content, _ts(1000 - i), int(own), kw.get("media"), kw.get("deleted")),
        )


def _row(rows, jid):
    return next(r for r in rows if r.jid == jid)


def test_counts_and_oldest(conns, db) -> None:
    _chat(db, NEW_CHAT)
    _msgs(db, NEW_CHAT, 5, own=True)  # own texts
    _msgs(db, NEW_CHAT, 2, own=True, tag="m", media="image")  # media: not a text
    _msgs(db, NEW_CHAT, 1, own=True, tag="e", content="  ")  # empty
    _msgs(db, NEW_CHAT, 1, own=True, tag="p", content="[Poll: lunch?]")
    _msgs(db, NEW_CHAT, 1, own=True, tag="c", content="[Contact: Someone]")
    _msgs(db, NEW_CHAT, 1, own=True, tag="d", deleted=_ts(1))
    _msgs(db, NEW_CHAT, 4, own=False, tag="in")
    db.commit()
    row = _row(history.coverage(*conns), NEW_CHAT)
    assert (row.messages, row.own_texts) == (15, 5)
    assert row.thin and row.kind == "private" and row.backfill_jid == NEW_CHAT
    assert row.oldest is not None and store.parse_ts(row.oldest) == store.parse_ts(_ts(1000))


def test_forms_merged(conns, db) -> None:
    _msgs(db, TOMO_LID, 3, own=True)
    db.commit()
    rows = history.coverage(*conns)
    matching = [r for r in rows if r.jid in (TOMO_LID, TOMO_PHONE)]
    assert len(matching) == 1
    assert matching[0].messages == 5  # the two fixture rows plus three
    assert matching[0].own_texts == 3
    assert matching[0].backfill_jid == TOMO_LID


def test_thin_flag(conns, db) -> None:
    _chat(db, NEW_CHAT)
    _msgs(db, NEW_CHAT, history.THIN_OWN - 1, own=True)
    db.commit()
    assert _row(history.coverage(*conns), NEW_CHAT).thin is True
    _msgs(db, NEW_CHAT, 1, own=True, tag="more")
    db.commit()
    assert _row(history.coverage(*conns), NEW_CHAT).thin is False


def test_skips_broadcast(conns, db) -> None:
    extra = "120363000000000555@newsletter"
    for jid in ("status@broadcast", extra):
        _chat(db, jid)
        _msgs(db, jid, 1, own=False)
    db.commit()
    jids = {r.jid for r in history.coverage(*conns, limit=500)}
    assert not jids & {"status@broadcast", extra, CHANNEL}
    assert ANA in jids


def test_empty_chat(conns, db) -> None:
    _chat(db, EMPTY_CHAT, last=2)
    db.commit()
    row = _row(history.coverage(*conns), EMPTY_CHAT)
    assert row.messages == 0 and row.own_texts == 0
    assert row.backfill_jid is None and row.oldest is None and row.thin


def test_limit_and_order(conns) -> None:
    rows = history.coverage(*conns, limit=3)
    assert len(rows) == 3
    stamps = [store._sort_key(r.last_active or "") for r in rows]
    assert stamps == sorted(stamps, reverse=True)


def test_totals(conns) -> None:
    t = history.totals(conns[0])
    assert t["messages"] > 150 and t["chats"] > 5 and t["oldest"]


def test_route(logged_in: TestClient) -> None:
    res = logged_in.get("/api/history/coverage")
    assert res.status_code == 200
    body = res.json()
    assert body["thin_threshold"] == 30
    assert {"messages", "chats", "oldest"} <= body["totals"].keys()
    assert body["chats"] and {"jid", "own_texts", "thin", "backfill_jid"} <= body["chats"][0].keys()


def test_route_needs_login(settings) -> None:
    with TestClient(create_app(settings), base_url="https://testserver") as anon:
        assert anon.get("/api/history/coverage").status_code == 401


def test_archived_chat_is_included(conns, db) -> None:
    _chat(db, NEW_CHAT)
    _msgs(db, NEW_CHAT, 3, own=True)
    db.execute("INSERT INTO chat_state (chat_jid, archived) VALUES (?, 1)", (NEW_CHAT,))
    db.commit()
    row = _row(history.coverage(*conns), NEW_CHAT)
    assert row.own_texts == 3


def test_totals_chats_match_coverage(conns, db) -> None:
    _chat(db, "status@broadcast")
    _msgs(db, "status@broadcast", 1, own=False)
    db.commit()
    ro, app = conns
    assert history.totals(ro)["chats"] == len(history.coverage(ro, app, limit=10_000))


def test_chat_coverage_matches_the_list(conns) -> None:
    listed = {c.jid: c for c in history.coverage(*conns)}
    assert history.chat_coverage(conns[0], conns[1], ANA) == listed[ANA]
    by_phone = history.chat_coverage(conns[0], conns[1], TOMO_PHONE)
    by_lid = history.chat_coverage(conns[0], conns[1], TOMO_LID)
    assert by_phone is not None and by_phone == by_lid == listed[by_phone.jid]


def test_chat_coverage_unknown_and_skipped(conns) -> None:
    assert history.chat_coverage(conns[0], conns[1], "385910000999@s.whatsapp.net") is None
    assert history.chat_coverage(conns[0], conns[1], CHANNEL) is None
    assert history.chat_coverage(conns[0], conns[1], "status@broadcast") is None


def test_chat_coverage_empty_chat(conns, db) -> None:
    db.execute("INSERT INTO chats (jid, name, last_message_time) VALUES (?,?,?)", (EMPTY_CHAT, "Empty", _ts(5)))
    db.commit()
    found = history.chat_coverage(conns[0], conns[1], EMPTY_CHAT)
    assert found and found.messages == 0 and found.backfill_jid is None

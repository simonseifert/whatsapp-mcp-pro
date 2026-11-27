"""Message search: the FTS5 index in app.db, kept in step with the bridge's store."""

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from tests import conftest as fx
from tests.test_live import add, bridge, now
from wa_app import appdb, live, search, store
from wa_app.config import Settings


@pytest.fixture
def appconn(settings: Settings) -> Iterator[sqlite3.Connection]:
    conn = appdb.connect(settings)
    yield conn
    conn.close()


@contextmanager
def ro_of(settings: Settings) -> Iterator[sqlite3.Connection]:
    ro = store.open_ro(settings)
    try:
        yield ro
    finally:
        ro.close()


@pytest.fixture
def indexed(settings: Settings, appconn: sqlite3.Connection) -> sqlite3.Connection:
    with ro_of(settings) as ro:
        search.index_messages(appconn, ro)
    return appconn


def find(settings: Settings, appconn: sqlite3.Connection, query: str, **kw) -> list[search.Hit]:
    with ro_of(settings) as ro:
        return search.search(appconn, ro, query, **kw)[0]


def ids(hits: list[search.Hit]) -> list[str]:
    return [h.message_id for h in hits]


def test_diacritics_are_folded(settings, indexed) -> None:
    hits = find(settings, indexed, "racun")  # "račun" in the accountant chat
    assert [h.chat_jid for h in hits] == [fx.ACCOUNTANT]
    assert hits[0].chat_name == fx.ACCOUNTANT_NAME
    assert find(settings, indexed, "RAČUN")


def test_snippet_marks_the_match(settings, indexed) -> None:
    (hit,) = find(settings, indexed, "listopad")
    assert "[listopad]" in hit.snippet


def test_last_word_matches_as_prefix(settings, indexed) -> None:
    assert find(settings, indexed, "slobod")  # "slobodna"
    assert not find(settings, indexed, "slobodnaa")
    assert find(settings, indexed, "sam slobod")  # earlier words are whole words
    assert not find(settings, indexed, "sa slobodna")


@pytest.mark.parametrize(
    "raw", ['"', "*", "NEAR(", 'a" OR "b', "body:x", "(", "-", "^kruh", "kruh AND", '"unterminated']
)
def test_user_text_never_reaches_fts_syntax(settings, indexed, raw) -> None:
    find(settings, indexed, raw)  # must not raise


def test_operators_are_plain_words(settings, indexed) -> None:
    assert not find(settings, indexed, "slobodna OR zzzz")  # OR is a word here, so nothing has all three
    assert search.fts_query('"') is None and search.fts_query("*") is None


def test_one_character_query_finds_nothing(settings, indexed) -> None:
    assert find(settings, indexed, "a") == []
    assert find(settings, indexed, "  ") == []


def test_polls_deleted_and_system_rows_are_not_indexed(settings, indexed) -> None:
    assert not find(settings, indexed, "kolokvij")  # a poll question
    assert not find(settings, indexed, "secret")  # a deleted message


def test_newest_first_and_pagination(settings, indexed) -> None:
    with bridge(settings) as b:
        for i in range(70):
            add(b, fx.ANA, f"P{i:03d}", f"pagetest riječ {i}", ts=f"2026-01-01 10:{i // 60:02d}:{i % 60:02d}+00:00")
    with ro_of(settings) as ro:
        search.index_messages(indexed, ro)
        first, nxt = search.search(indexed, ro, "pagetest")
        assert len(first) == 30 and nxt
        assert ids(first)[0] == "P069" and ids(first)[-1] == "P040"
        second, nxt2 = search.search(indexed, ro, "pagetest", before=nxt)
        assert ids(second)[0] == "P039" and len(second) == 30 and nxt2
        third, end = search.search(indexed, ro, "pagetest", before=nxt2)
        assert len(third) == 10 and end is None
        assert len({*ids(first), *ids(second), *ids(third)}) == 70


def test_pagination_does_not_skip_same_second_messages(settings, indexed) -> None:
    with bridge(settings) as b:
        for i in range(5):
            add(b, fx.ANA, f"S{i}", "samesecond", ts="2026-02-02 08:00:00+00:00")
    with ro_of(settings) as ro:
        search.index_messages(indexed, ro)
        seen: list[str] = []
        cursor = None
        while True:
            hits, cursor = search.search(indexed, ro, "samesecond", before=cursor, limit=2)
            seen += ids(hits)
            if cursor is None:
                break
    assert sorted(seen) == [f"S{i}" for i in range(5)]


def test_chat_filter(settings, indexed) -> None:
    with bridge(settings) as b:
        add(b, fx.ANA, "F1", "zajednicka rijec")
        add(b, fx.LUKAS, "F2", "zajednicka rijec", sender=fx.LUKAS)
    with ro_of(settings) as ro:
        search.index_messages(indexed, ro)
        both, _ = search.search(indexed, ro, "zajednicka")
        only, _ = search.search(indexed, ro, "zajednicka", chat_jid=fx.LUKAS)
    assert {h.chat_jid for h in both} == {fx.ANA, fx.LUKAS}
    assert ids(only) == ["F2"]


def test_chat_filter_accepts_any_form_of_the_chat(settings, indexed) -> None:
    with bridge(settings) as b:
        add(b, fx.TOMO_LID, "T1", "lidform poruka", sender=fx.TOMO_LID)
    with ro_of(settings) as ro:
        search.index_messages(indexed, ro)
        by_lid, _ = search.search(indexed, ro, "lidform", chat_jid=fx.TOMO_LID)
        by_phone, _ = search.search(indexed, ro, "lidform", chat_jid=fx.TOMO_PHONE)
    assert ids(by_lid) == ids(by_phone) == ["T1"]
    assert by_lid[0].chat_jid == by_phone[0].chat_jid


def test_sender_names(settings, indexed) -> None:
    (mine,) = find(settings, indexed, "Može kad")
    assert mine.sender_name == "You"
    (theirs,) = find(settings, indexed, "Tko donosi")
    assert theirs.sender_name == "Tata"


def test_backfill_resumes_after_a_restart(settings, appconn, monkeypatch) -> None:
    monkeypatch.setattr(search, "BATCH", 3)
    with ro_of(settings) as ro:
        total = ro.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        first = search.index_messages(appconn, ro, max_batches=1)
        assert 0 < first <= 3
        saved = appconn.execute("SELECT value FROM meta WHERE key='search_rowid'").fetchone()
        assert saved and int(saved[0]) >= 1
    appconn.close()
    again = appdb.connect(settings)  # "restart": a new connection, the position comes from app.db
    try:
        with ro_of(settings) as ro:
            rest = search.index_messages(again, ro)
            assert search.index_messages(again, ro) == 0  # nothing left
            assert first + rest <= total
        assert find(settings, again, "racun") and find(settings, again, "slobodna")
        rows = again.execute("SELECT COUNT(*), COUNT(DISTINCT chat_jid || message_id) FROM msg_fts_keys").fetchone()
        assert rows[0] == rows[1] == again.execute("SELECT COUNT(*) FROM msg_fts").fetchone()[0]
    finally:
        again.close()


def test_backfill_is_idempotent(settings, indexed) -> None:
    before = indexed.execute("SELECT COUNT(*) FROM msg_fts").fetchone()[0]
    with ro_of(settings) as ro:
        search.index_messages(indexed, ro, since_rowid=0)
    assert indexed.execute("SELECT COUNT(*) FROM msg_fts").fetchone()[0] == before


def test_voice_transcripts_are_indexed(settings, indexed) -> None:
    with ro_of(settings) as ro:
        voice = next(m for m in store.list_messages(ro, fx.FAMILY) if m.media and m.media.type == "audio")
    assert not find(settings, indexed, "kruh i mlijeko")
    indexed.execute(
        "INSERT INTO transcripts (message_id, chat_jid, text, language, created_at) VALUES (?,?,?,?,?)",
        (voice.id, fx.FAMILY, "Kupi kruh i mlijeko.", "hr", int(time.time())),
    )
    with ro_of(settings) as ro:
        search.reindex(indexed, ro, [(fx.FAMILY, voice.id)])
    (hit,) = find(settings, indexed, "mlijeko")
    assert hit.message_id == voice.id and hit.chat_jid == fx.FAMILY


def test_existing_transcripts_are_picked_up_by_the_backfill(settings, appconn) -> None:
    with ro_of(settings) as ro:
        voice = next(m for m in store.list_messages(ro, fx.FAMILY) if m.media and m.media.type == "audio")
    appconn.execute(
        "INSERT INTO transcripts (message_id, chat_jid, text, language, created_at) VALUES (?,?,?,?,?)",
        (voice.id, fx.FAMILY, "Dolazim sutra", "hr", 1),
    )
    with ro_of(settings) as ro:
        search.index_messages(appconn, ro)
    assert ids(find(settings, appconn, "dolazim")) == [voice.id]


class Feed:
    """The real poller feeding the indexer, as main.py wires them."""

    def __init__(self, settings: Settings, appconn: sqlite3.Connection) -> None:
        self.poller = live.Poller(settings)
        self.indexer = search.Indexer(settings, appconn)
        self.poller.on_events.append(self.indexer.on_events)
        assert self.poller.poll_once() == []

    def tick(self) -> None:
        self.poller._tick()
        self.indexer.flush()

    def close(self) -> None:
        self.poller.close()
        self.indexer.close()


@pytest.fixture
def feed(settings, indexed) -> Iterator[Feed]:
    f = Feed(settings, indexed)
    yield f
    f.close()


def test_new_message_is_indexed_from_the_event(settings, indexed, feed) -> None:
    assert not find(settings, indexed, "novasifra")
    with bridge(settings) as b:
        add(b, fx.ANA, "N1", "Evo novasifra za wifi", sender=fx.ANA)
    feed.tick()
    assert ids(find(settings, indexed, "novasifra")) == ["N1"]


def test_edit_replaces_the_indexed_text(settings, indexed, feed) -> None:
    with bridge(settings) as b:
        add(b, fx.ANA, "E1", "stara verzija teksta", sender=fx.ANA)
    feed.tick()
    assert find(settings, indexed, "stara")
    with bridge(settings) as b:
        b.execute(
            "UPDATE messages SET content='nova verzija teksta', is_edited=1, edit_count=1 WHERE id='E1' AND chat_jid=?",
            (fx.ANA,),
        )
        b.execute(
            "INSERT INTO message_edits (message_id, chat_jid, edit_id, old_content, new_content, edited_at)"
            " VALUES ('E1', ?, 'x1', 'stara verzija teksta', 'nova verzija teksta', ?)",
            (fx.ANA, now()),
        )
    feed.tick()
    assert not find(settings, indexed, "stara")
    (hit,) = find(settings, indexed, "nova verzija")
    assert hit.message_id == "E1"
    assert indexed.execute("SELECT COUNT(*) FROM msg_fts WHERE message_id='E1'").fetchone()[0] == 1


def test_revoke_removes_the_row(settings, indexed, feed) -> None:
    with bridge(settings) as b:
        add(b, fx.ANA, "R1", "tajna lozinka stoji ovdje", sender=fx.ANA)
    feed.tick()
    assert find(settings, indexed, "lozinka")
    with bridge(settings) as b:
        b.execute("UPDATE messages SET deleted_at=? WHERE id='R1'", (now(),))
    feed.tick()
    assert not find(settings, indexed, "lozinka")
    assert indexed.execute("SELECT COUNT(*) FROM msg_fts WHERE message_id='R1'").fetchone()[0] == 0
    assert indexed.execute("SELECT COUNT(*) FROM msg_fts_keys WHERE message_id='R1'").fetchone()[0] == 0


def test_a_revoked_message_is_never_shown_even_if_the_index_missed_it(settings, indexed) -> None:
    with bridge(settings) as b:
        add(b, fx.ANA, "R2", "povjerljivo tekst", sender=fx.ANA)
    with ro_of(settings) as ro:
        search.index_messages(indexed, ro)
    assert find(settings, indexed, "povjerljivo")
    with bridge(settings) as b:
        b.execute("UPDATE messages SET deleted_at=? WHERE id='R2'", (now(),))  # nobody told the index
    assert find(settings, indexed, "povjerljivo") == []


def test_edits_and_revokes_made_while_the_app_was_down(settings, indexed) -> None:
    with bridge(settings) as b:
        add(b, fx.ANA, "D1", "prije promjene", sender=fx.ANA)
        add(b, fx.ANA, "D2", "bit ce obrisano", sender=fx.ANA)
    with ro_of(settings) as ro:
        search.index_messages(indexed, ro)
        search.reconcile(indexed, ro)  # takes the edit baseline
    with bridge(settings) as b:
        b.execute("UPDATE messages SET content='poslije promjene' WHERE id='D1'")
        b.execute(
            "INSERT INTO message_edits (message_id, chat_jid, edit_id, new_content, edited_at)"
            " VALUES ('D1', ?, 'e', 'poslije promjene', ?)",
            (fx.ANA, now()),
        )
        b.execute("UPDATE messages SET deleted_at=? WHERE id='D2'", (now(),))
    with ro_of(settings) as ro:
        search.reconcile(indexed, ro)
    assert not find(settings, indexed, "prije promjene")
    assert find(settings, indexed, "poslije promjene")
    assert indexed.execute("SELECT COUNT(*) FROM msg_fts WHERE message_id='D2'").fetchone()[0] == 0


def test_chat_moving_to_its_phone_form_keeps_one_row(settings, indexed) -> None:
    with bridge(settings) as b:
        add(b, fx.TOMO_LID, "M1", "dvaputa ista", sender=fx.TOMO_LID)
    with ro_of(settings) as ro:
        search.reindex(indexed, ro, [(fx.TOMO_LID, "M1")])
        search.reindex(indexed, ro, [(fx.TOMO_PHONE, "M1")])
    assert indexed.execute("SELECT COUNT(*) FROM msg_fts WHERE message_id='M1'").fetchone()[0] == 1


# --- routes -------------------------------------------------------------------------------------------------------


def wait_for_hits(client, params: str, timeout: float = 5.0) -> dict:
    """The app indexes in the background after start: poll until the first hits arrive."""
    end = time.time() + timeout
    while True:
        body = client.get(f"/api/search?{params}").json()
        if body.get("hits") or time.time() > end:
            return body
        time.sleep(0.05)


def test_search_route_needs_login(client) -> None:
    assert client.get("/api/search?q=racun").status_code == 401


def test_search_route_finds_and_shapes_hits(logged_in) -> None:
    body = wait_for_hits(logged_in, "q=racun")
    assert body["next"] is None
    (hit,) = body["hits"]
    assert set(hit) == {"chat_jid", "chat_name", "message_id", "ts", "sender_name", "snippet"}
    assert hit["chat_jid"] == fx.ACCOUNTANT and "[račun]" in hit["snippet"]


def test_search_route_odd_input_is_empty_not_an_error(logged_in) -> None:
    for q in ['"', "*", "NEAR(", "a", ""]:
        r = logged_in.get("/api/search", params={"q": q})
        assert r.status_code == 200 and r.json() == {"hits": [], "next": None}


def test_search_route_bad_cursor_is_400(logged_in) -> None:
    assert logged_in.get("/api/search?q=racun&before=zzz").status_code == 400


def test_search_route_chat_filter(logged_in) -> None:
    wait_for_hits(logged_in, "q=racun")
    assert logged_in.get("/api/search", params={"q": "racun", "chat": fx.ANA}).json()["hits"] == []
    assert logged_in.get("/api/search", params={"q": "racun", "chat": fx.ACCOUNTANT}).json()["hits"]


def test_around_returns_the_target_in_a_window(logged_in, settings) -> None:
    with bridge(settings) as b:
        for i in range(80):
            add(b, fx.ANA, f"W{i:03d}", f"window {i}", ts=f"2026-03-01 09:{i // 60:02d}:{i % 60:02d}+00:00")
    body = logged_in.get(f"/api/chats/{fx.ANA}/messages", params={"around": "W040"}).json()
    got = [m["id"] for m in body["messages"]]
    assert "W040" in got and got.index("W040") == 30 and len(got) == 61
    assert got[0] == "W010" and got[-1] == "W070"
    assert body["has_more"] is True and body["has_newer"] is True


def test_around_near_the_end_has_nothing_newer(logged_in, settings) -> None:
    with bridge(settings) as b:
        for i in range(5):
            add(b, fx.ANA, f"X{i}", f"x {i}", ts=f"2999-03-01 09:00:0{i}+00:00")
    body = logged_in.get(f"/api/chats/{fx.ANA}/messages", params={"around": "X4"}).json()
    assert body["messages"][-1]["id"] == "X4" and body["has_newer"] is False


def test_around_unknown_message_is_404(logged_in) -> None:
    assert logged_in.get(f"/api/chats/{fx.ANA}/messages", params={"around": "nope"}).status_code == 404


def test_around_works_through_the_other_form_of_a_chat(logged_in, settings) -> None:
    with bridge(settings) as b:
        add(b, fx.TOMO_LID, "L1", "lid poruka", sender=fx.TOMO_LID)
    body = logged_in.get(f"/api/chats/{fx.TOMO_PHONE}/messages", params={"around": "L1"}).json()
    assert "L1" in [m["id"] for m in body["messages"]]

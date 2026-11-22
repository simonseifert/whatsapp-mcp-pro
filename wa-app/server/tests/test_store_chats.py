import sqlite3
from pathlib import Path

import pytest

from tests import conftest as fx
from tests.conftest import make_store
from wa_app import appdb, store
from wa_app.config import Settings


@pytest.fixture
def conns(settings: Settings):
    ro = store.open_ro(settings)
    app = appdb.connect(settings)
    yield ro, app
    ro.close()
    app.close()


def jids(chats) -> list[str]:
    return [c.jid for c in chats]


def by_jid(chats, jid: str):
    return next(c for c in chats if c.jid == jid)


def test_open_ro_is_read_only(settings: Settings) -> None:
    ro = store.open_ro(settings)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("UPDATE chats SET name = 'x'")
    ro.close()


def test_order_pinned_first_then_recent(conns) -> None:
    chats = store.list_chats(*conns)
    order = jids(chats)
    assert order[:2] == [fx.FAMILY, fx.ANA]  # pinned, the more recent first
    rest = order[2:]
    times = [c.last_message.timestamp_iso for c in chats[2:]]
    assert times == sorted(times, reverse=True)
    assert rest[0] == fx.TOMO_PHONE  # newest unpinned, found through its LID half


def test_archived_hidden_and_counted(conns) -> None:
    visible = store.list_chats(*conns)
    assert fx.OLD_ARCHIVED not in jids(visible)
    assert fx.OLD_ARCHIVED_GROUP not in jids(visible)
    assert store.list_chats_with_count(*conns)[1] == 2
    assert store.list_chats_with_count(*conns, archived=True)[1] == 2
    archived = store.list_chats(*conns, archived=True)
    assert sorted(jids(archived)) == sorted([fx.OLD_ARCHIVED, fx.OLD_ARCHIVED_GROUP])
    assert all(c.archived for c in archived)


def test_merged_rows_skipped(conns) -> None:
    chats = store.list_chats(*conns)
    assert fx.IVANA_LID_SHELL not in jids(chats)
    assert [c for c in chats if c.name == "Ivana"] == [by_jid(chats, fx.IVANA)]


def test_lid_and_phone_are_one_chat(conns) -> None:
    chats = store.list_chats(*conns)
    tomo = [c for c in chats if c.jid in (fx.TOMO_LID, fx.TOMO_PHONE)]
    assert len(tomo) == 1
    assert tomo[0].name == "Tomo (posao)"  # the nickname, not the push name or the bare number
    assert tomo[0].last_message.preview == "Stigao sam"  # the newer of the two halves (under the LID)
    assert tomo[0].jid == fx.TOMO_PHONE  # canonical phone form


def test_filters(conns) -> None:
    unread = store.list_chats(*conns, filter="unread")
    assert set(jids(unread)) == {fx.ANA, fx.FAMILY, fx.NEIGHBOURHOOD, fx.STUDY, fx.NIKOLA}
    groups = store.list_chats(*conns, filter="groups")
    assert set(jids(groups)) == {fx.FAMILY, fx.NEIGHBOURHOOD, fx.STUDY}
    favourites = store.list_chats(*conns, filter="favourites")
    assert jids(favourites) == [fx.FAMILY, fx.ANA]


def test_unread_counts_and_marked_unread(conns) -> None:
    chats = store.list_chats(*conns)
    assert by_jid(chats, fx.ANA).unread == 2  # two incoming after last_read_at, mine not counted
    assert by_jid(chats, fx.FAMILY).unread == 5  # no marker: the history count
    assert by_jid(chats, fx.STUDY).unread == 12
    assert by_jid(chats, fx.ACCOUNTANT).unread == 0  # read after the last message
    nikola = by_jid(chats, fx.NIKOLA)
    assert nikola.unread == 0 and nikola.marked_unread is True


def test_query_matches_name_with_diacritics(conns) -> None:
    for q in ("durdica", "Đurđica", "ĐURĐICA", "simunovic"):
        assert jids(store.list_chats(*conns, query=q)) == [fx.ACCOUNTANT], q
    assert store.list_chats(*conns, query="no such person") == []


def test_preview_kinds(conns) -> None:
    chats = store.list_chats(*conns)
    family = by_jid(chats, fx.FAMILY).last_message
    assert family.preview == "🎤 Voice message (0:42)"
    assert family.media_type == "audio"
    assert family.sender_name == "Mama"  # sender prefix in groups
    deleted = by_jid(chats, fx.UNKNOWN).last_message
    assert deleted.preview == "Message deleted" and deleted.deleted is True
    assert "secret" not in deleted.preview
    assert by_jid(chats, fx.GYM).last_message.preview == "Sticker"
    photo = by_jid(chats, fx.NIKOLA).last_message
    assert photo.preview == "📷 Photo" and photo.media_type == "image"
    assert photo.sender_name is None  # no prefix in a private chat
    assert by_jid(chats, fx.STUDY).last_message.preview == "📊 Poll: Kad idemo na kolokvij pripreme?"
    mine = by_jid(chats, fx.LUKAS).last_message
    assert mine.from_me is True and mine.sender_name is None and mine.ticks is None


def test_voice_without_file_has_no_duration(tmp_path: Path, settings: Settings) -> None:
    for f in (settings.store_path / fx.FAMILY).glob("*.ogg"):
        f.unlink()
    ro = store.open_ro(settings)
    family = by_jid(store.list_chats(ro, appdb.connect(settings)), fx.FAMILY)
    assert family.last_message.preview == "🎤 Voice message"
    ro.close()


def test_muted_when_muted_until_in_future(conns) -> None:
    chats = store.list_chats(*conns)
    assert by_jid(chats, fx.FAMILY).muted is True
    assert by_jid(chats, fx.ANA).muted is False
    ro, _ = conns
    ro_path = ro.execute("PRAGMA database_list").fetchone()[2]
    rw = sqlite3.connect(ro_path)
    rw.execute("UPDATE chat_state SET muted_until = 1000 WHERE chat_jid = ?", (fx.FAMILY,))  # long past
    rw.commit()
    assert by_jid(store.list_chats(*conns), fx.FAMILY).muted is False
    rw.execute("UPDATE chat_state SET muted_until = -1 WHERE chat_jid = ?", (fx.FAMILY,))  # forever
    rw.commit()
    assert by_jid(store.list_chats(*conns), fx.FAMILY).muted is True
    rw.close()


def test_kinds_and_community(conns) -> None:
    chats = store.list_chats(*conns)
    assert by_jid(chats, fx.ANA).kind == "private"
    assert by_jid(chats, fx.FAMILY).kind == "group"
    hood = by_jid(chats, fx.NEIGHBOURHOOD)
    assert hood.kind == "announcement" and hood.community_name == "Kvart Trsat"
    assert by_jid(chats, fx.CHANNEL).kind == "newsletter"
    assert fx.COMMUNITY not in jids(chats)  # the community shell has no messages
    assert by_jid(chats, fx.ANA).avatar_url == f"/api/avatar/{fx.ANA}"


def test_plain_community_subgroup_is_community_group(conns) -> None:
    ro, _ = conns
    path = ro.execute("PRAGMA database_list").fetchone()[2]
    rw = sqlite3.connect(path)
    rw.execute("UPDATE identities SET is_announce = 0, is_default_subgroup = 0 WHERE jid = ?", (fx.NEIGHBOURHOOD,))
    rw.commit()
    rw.close()
    assert by_jid(store.list_chats(*conns), fx.NEIGHBOURHOOD).kind == "community_group"


def test_names_fall_back_to_number(conns) -> None:
    chats = store.list_chats(*conns)
    assert by_jid(chats, fx.UNKNOWN).name == "+385910000008"
    assert by_jid(chats, fx.ACCOUNTANT).name == fx.ACCOUNTANT_NAME


def test_limit(conns) -> None:
    assert len(store.list_chats(*conns, limit=3)) == 3


def test_store_without_optional_tables(tmp_path: Path) -> None:
    """An older store (no chat_state, no identities, no deleted_at) still lists chats."""
    path = tmp_path / "old"
    path.mkdir()
    db = sqlite3.connect(path / "messages.db")
    db.executescript(
        """
        CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP);
        CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, sender_name TEXT, content TEXT,
            timestamp TIMESTAMP, is_from_me BOOLEAN, media_type TEXT, filename TEXT, PRIMARY KEY (id, chat_jid));
        INSERT INTO chats VALUES ('385910000001@s.whatsapp.net', 'Ana', '2026-11-22 10:00:00+01:00');
        INSERT INTO messages VALUES ('1', '385910000001@s.whatsapp.net', '385910000001@s.whatsapp.net', NULL,
            'hello', '2026-11-22 10:00:00+01:00', 0, NULL, NULL);
        """
    )
    db.commit()
    db.close()
    ro = sqlite3.connect(f"file:{path / 'messages.db'}?mode=ro", uri=True)
    chats = store.list_chats(ro, sqlite3.connect(":memory:"))
    assert [(c.name, c.unread) for c in chats] == [("Ana", 0)]
    ro.close()


def test_make_store_is_repeatable(tmp_path: Path) -> None:
    assert make_store(tmp_path / "a") != make_store(tmp_path / "b")


def _big_store(tmp_path: Path, chats: int, identities_per_chat: int = 3) -> sqlite3.Connection:
    path = tmp_path / f"big{chats}"
    path.mkdir()
    db = sqlite3.connect(path / "messages.db")
    db.executescript(fx._SCHEMA)
    for i in range(chats):
        jid = f"3859{i:08d}@s.whatsapp.net"
        db.execute(
            "INSERT INTO chats (jid, name, last_message_time) VALUES (?,?,?)",
            (jid, f"Person {i}", "2026-11-22 10:00:00+01:00"),
        )
        db.execute(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?,?,?,?,?,0)",
            (f"m{i}", jid, jid, "hi", "2026-11-22 10:00:00+01:00"),
        )
        for k in range(identities_per_chat):  # one real identity plus unrelated directory rows
            ident = jid if k == 0 else f"{k}{i:08d}@s.whatsapp.net"
            db.execute(
                "INSERT INTO identities (jid, phone_jid, display_name) VALUES (?,?,?)", (ident, ident, f"Person {i}")
            )
    db.commit()
    db.close()
    return sqlite3.connect(f"file:{path / 'messages.db'}?mode=ro", uri=True)


def test_statement_count_does_not_scan_directory_per_chat(tmp_path: Path) -> None:
    """Grouping must not query identities per chat (it used to scan the table once per chat)."""
    counts = {}
    for n in (100, 1000):
        ro = _big_store(tmp_path, n)
        statements: list[str] = []
        ro.set_trace_callback(statements.append)
        chats = store.list_chats(ro, sqlite3.connect(":memory:"), limit=10_000)
        assert len(chats) == n
        directory = [q for q in statements if "identities" in q]
        # Our grouping reads identities once; the rest is lib.naming's name lookup, one query per 200 keys.
        assert len(directory) <= 12 + n // 50, len(directory)
        assert len(statements) <= 2 * n + 50  # per chat only the indexed last-message query (+ unread when marked)
        counts[n] = len(statements)
        ro.close()
    assert counts[1000] - counts[100] <= 2 * 900 + 10

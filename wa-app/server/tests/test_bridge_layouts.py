"""Media folders as the bridge really writes them, downloadable, unread fallback, ticks and their live events."""

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest

from tests import conftest as fx
from tests.test_live import add, bridge, names, now
from wa_app import appdb, live, store
from wa_app.config import Settings

GROUP = "120363430507332452@g.us"


def db(settings: Settings, sql: str, *args: object) -> None:
    conn = sqlite3.connect(settings.store_path / "messages.db")
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def ensure_group(settings: Settings, members: int | None = None) -> None:
    db(settings, "INSERT OR IGNORE INTO chats (jid, name) VALUES (?, 'G')", GROUP)
    db(
        settings,
        "INSERT OR REPLACE INTO identities (jid, kind, participant_count) VALUES (?, 'group', ?)",
        GROUP,
        members,
    )


@pytest.fixture
def ro(settings: Settings) -> Iterator[sqlite3.Connection]:
    conn = store.open_ro(settings)
    yield conn
    conn.close()


# --- media folders -------------------------------------------------------------------------------------------------


def test_sanitisers_mirror_the_bridge() -> None:
    assert store._safe_part("120363430507332452@g.us") == "120363430507332452_g.us"
    assert store._safe_part("a b/c") == "a_b_c"
    assert [store._safe_part(x) for x in ("", ".", "..", ".hidden")] == ["", "", "", ""]
    assert store._sanitize_path("120363430507332452@g.us") == "120363430507332452@g_us"
    assert store._sanitize_path("") == "_"
    assert store._sanitize_path(".x") == "_x"
    assert store._sanitize_path("a.b") == "a_b"
    assert store._sanitize_path("-x") == "_x"
    assert store._sanitize_path("é") == "__"  # per UTF-8 byte, like the Go loop


def test_has_file_auto_download_layout(settings: Settings) -> None:
    folder = settings.store_path / "120363430507332452_g.us"
    folder.mkdir()
    (folder / "image_20261119_122813.jpg").write_bytes(b"x")
    assert store._has_file(settings.store_path, [GROUP], "M1", "image_20261119_122813.jpg")
    assert not store._has_file(settings.store_path, [GROUP], "M1", "other.jpg")


def test_has_file_auto_download_with_id_prefix_and_odd_names(settings: Settings) -> None:
    folder = settings.store_path / "120363430507332452_g.us"
    folder.mkdir()
    (folder / "3EB0_my_doc_.pdf").write_bytes(b"x")  # id prefix; filename 'my doc!.pdf' sanitised
    assert store._has_file(settings.store_path, [GROUP], "3EB0", "my doc!.pdf")


def test_has_file_on_demand_layout(settings: Settings) -> None:
    folder = settings.store_path / "media" / "120363430507332452@g_us"
    folder.mkdir(parents=True)
    (folder / "3EB0ABC.jpg").write_bytes(b"x")
    assert store._has_file(settings.store_path, [GROUP], "3EB0ABC", None)
    assert not store._has_file(settings.store_path, [GROUP], "3EB0ABD", None)


def test_has_file_tries_every_jid_form_and_ignores_the_old_wrong_folder(settings: Settings) -> None:
    folder = settings.store_path / "media" / "111@lid"
    folder.mkdir(parents=True)
    (folder / "ID1.ogg").write_bytes(b"x")
    assert store._has_file(settings.store_path, [fx.TOMO_PHONE, "111@lid"], "ID1", None)
    wrong = settings.store_path / "media" / GROUP
    wrong.mkdir(parents=True)
    (wrong / "ID2.jpg").write_bytes(b"x")  # the raw JID is not a folder the bridge writes
    assert not store._has_file(settings.store_path, [GROUP], "ID2", None)


def test_voice_seconds_reads_both_layouts(settings: Settings) -> None:
    auto = settings.store_path / "120363430507332452_g.us"
    auto.mkdir()
    (auto / "a.ogg").write_bytes(fx.fake_ogg_opus(7))
    assert store._voice_seconds(settings.store_path, [GROUP], GROUP, "a.ogg") == 7
    demand = settings.store_path / "media" / "120363430507332452@g_us"
    demand.mkdir(parents=True)
    (demand / "MID9.ogg").write_bytes(fx.fake_ogg_opus(9))
    assert store._voice_seconds(settings.store_path, [GROUP], GROUP, None, "MID9") == 9


def test_message_view_sees_the_file_in_the_chat_folder(settings: Settings, ro: sqlite3.Connection) -> None:
    ensure_group(settings)
    folder = settings.store_path / "120363430507332452_g.us"
    folder.mkdir()
    (folder / "image_x.jpg").write_bytes(b"x")
    db(
        settings,
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename)"
        " VALUES ('P1', ?, 's', '', ?, 0, 'image', 'image_x.jpg')",
        GROUP,
        now(),
    )
    view = store.get_message(store.open_ro(settings), GROUP, "P1")
    assert view is not None and view.media is not None and view.media.has_file is True


# --- downloadable --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "path", "url", "expected"),
    [
        (b"k", "/v/t62/x", None, True),
        (b"k", None, "https://mmg.whatsapp.net/x", True),
        (b"k", "", "", False),
        (None, "/v/t62/x", None, False),
        (b"", "/v/t62/x", None, False),
    ],
)
def test_downloadable(settings: Settings, key: bytes | None, path: str | None, url: str | None, expected: bool) -> None:
    ensure_group(settings)
    db(
        settings,
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename,"
        " media_key, direct_path, url) VALUES ('D1', ?, 's', '', ?, 0, 'image', 'f.jpg', ?, ?, ?)",
        GROUP,
        now(),
        key,
        path,
        url,
    )
    conn = store.open_ro(settings)
    view = store.get_message(conn, GROUP, "D1")
    conn.close()
    assert view is not None and view.media is not None and view.media.downloadable is expected


# --- unread fallback -----------------------------------------------------------------------------------------------


def stamp(minutes_ago: float) -> str:
    return (datetime.now().astimezone() - timedelta(minutes=minutes_ago)).replace(microsecond=0).isoformat(sep=" ")


def count(settings: Settings, jid: str) -> int:
    from wa_app import unread

    conn, app = store.open_ro(settings), appdb.connect(settings)
    try:
        return unread.count(conn, app, jid)
    finally:
        conn.close()
        app.close()


def make_bridge_state(settings: Settings, since: str | None) -> None:
    db(settings, "CREATE TABLE bridge_state (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP)")
    if since:
        db(settings, "INSERT INTO bridge_state VALUES ('read_tracking_since', ?, ?)", since, since)


def test_no_marker_no_tracking_key_keeps_history_count(settings: Settings) -> None:
    assert count(settings, fx.FAMILY) == 5  # no bridge_state table at all
    make_bridge_state(settings, None)
    assert count(settings, fx.FAMILY) == 5  # table without the key


def test_no_marker_counts_incoming_after_tracking_start(settings: Settings) -> None:
    make_bridge_state(settings, datetime.fromisoformat(stamp(20)).astimezone().isoformat())  # RFC3339
    # IVANA has one old incoming message and no marker: before the tracking start, so not unread.
    assert count(settings, fx.IVANA) == 0
    db(
        settings,
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES ('U1', ?, ?, 'hi', ?, 0)",
        fx.IVANA,
        fx.IVANA,
        stamp(5),
    )
    db(
        settings,
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES ('U2', ?, ?, 'mine', ?, 1)",
        fx.IVANA,
        fx.IVANA,
        stamp(4),
    )
    assert count(settings, fx.IVANA) == 1
    # The tracking start wins over the stale history count when there is no marker.
    assert count(settings, fx.FAMILY) == 1  # only its voice note (12 min ago) is after the start; history says 5


def test_marker_still_beats_tracking_start(settings: Settings) -> None:
    make_bridge_state(settings, datetime.fromisoformat(stamp(5000)).astimezone().isoformat())
    assert count(settings, fx.ANA) == 2  # Ana's marker (100 minutes ago) decides, not the older tracking start


# --- ticks ---------------------------------------------------------------------------------------------------------


def make_receipts(settings: Settings) -> None:
    db(
        settings,
        "CREATE TABLE message_receipts (message_id TEXT NOT NULL, chat_jid TEXT NOT NULL, participant TEXT NOT NULL,"
        " status INTEGER NOT NULL, at TIMESTAMP NOT NULL, PRIMARY KEY (message_id, chat_jid, participant))",
    )


def receipt(settings: Settings, mid: str, chat: str, who: str, status: int) -> None:
    db(settings, "INSERT OR REPLACE INTO message_receipts VALUES (?,?,?,?,?)", mid, chat, who, status, now())


def mine(settings: Settings, chat: str, mid: str) -> None:
    db(
        settings,
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, 'me', 'x', ?, 1)",
        mid,
        chat,
        now(),
    )


def ticks(settings: Settings, chat: str, mid: str) -> str | None:
    conn = store.open_ro(settings)
    try:
        view = store.get_message(conn, chat, mid)
        assert view is not None
        return view.ticks
    finally:
        conn.close()


def test_no_receipts_table_means_no_ticks(settings: Settings) -> None:
    mine(settings, fx.ANA, "T0")
    assert ticks(settings, fx.ANA, "T0") is None


def test_private_ticks(settings: Settings) -> None:
    make_receipts(settings)
    for mid in ("T1", "T2", "T3", "T4"):
        mine(settings, fx.ANA, mid)
    receipt(settings, "T2", fx.ANA, fx.ANA, 1)
    receipt(settings, "T3", fx.ANA, fx.ANA, 2)
    receipt(settings, "T4", fx.ANA, fx.ANA, 3)
    assert [ticks(settings, fx.ANA, m) for m in ("T1", "T2", "T3", "T4")] == ["sent", "delivered", "read", "read"]


def test_incoming_and_system_messages_get_no_ticks(settings: Settings) -> None:
    make_receipts(settings)
    conn = store.open_ro(settings)
    views = store.list_messages(conn, fx.EKIPA)
    conn.close()
    assert all(v.ticks is None for v in views if not v.from_me or v.system)
    assert next(v for v in views if v.id == "E14").ticks == "sent"


def test_ticks_across_jid_forms(settings: Settings) -> None:
    make_receipts(settings)
    mine(settings, fx.TOMO_PHONE, "T5")
    receipt(settings, "T5", fx.TOMO_LID, fx.TOMO_LID, 2)  # the receipt is filed under the other form
    assert ticks(settings, fx.TOMO_PHONE, "T5") == "read"


def test_group_read_needs_every_other_member(settings: Settings) -> None:
    make_receipts(settings)
    ensure_group(settings, members=4)  # us plus three
    mine(settings, GROUP, "G1")
    receipt(settings, "G1", GROUP, "a@s.whatsapp.net", 2)
    receipt(settings, "G1", GROUP, "b@s.whatsapp.net", 3)
    assert ticks(settings, GROUP, "G1") == "delivered"
    receipt(settings, "G1", GROUP, "c@s.whatsapp.net", 2)
    assert ticks(settings, GROUP, "G1") == "read"


def test_group_without_member_count_is_never_read(settings: Settings) -> None:
    make_receipts(settings)
    ensure_group(settings, members=None)
    mine(settings, GROUP, "G2")
    receipt(settings, "G2", GROUP, "a@s.whatsapp.net", 3)
    assert ticks(settings, GROUP, "G2") == "delivered"


def test_chat_list_last_message_ticks(settings: Settings) -> None:
    make_receipts(settings)
    receipt(settings, "M0002", fx.LUKAS, fx.LUKAS, 0)
    conn, app = store.open_ro(settings), appdb.connect(settings)
    chats = {c.jid: c for c in store.list_chats(conn, app)}
    conn.close()
    assert chats[fx.LUKAS].last_message.from_me and chats[fx.LUKAS].last_message.ticks in ("sent", "delivered", "read")
    assert chats[fx.ANA].last_message.ticks is None  # incoming
    receipt(settings, chats[fx.LUKAS].last_message.id, fx.LUKAS, fx.LUKAS, 2)
    conn = store.open_ro(settings)
    assert store.get_chat(conn, app, fx.LUKAS).last_message.ticks == "read"  # type: ignore[union-attr]
    conn.close()


# --- live ----------------------------------------------------------------------------------------------------------


def test_receipt_emits_message_changed_and_chat_for_the_last_message(settings: Settings) -> None:
    make_receipts(settings)
    with bridge(settings) as w:
        add(w, fx.ANA, "L1", "mine", from_me=True)
    poller = live.Poller(settings)
    poller.poll_once()
    with bridge(settings) as w:
        w.execute("INSERT INTO message_receipts VALUES ('L1', ?, ?, 1, ?)", (fx.ANA, fx.ANA, now()))
    events = poller.poll_once()
    assert names(events) == ["message_changed", "chat"]
    assert events[0].data == {"chat_jid": fx.ANA, "id": "L1"}
    assert events[1].data["chat"]["last_message"]["ticks"] == "delivered"
    assert poller.poll_once() == []
    with bridge(settings) as w:  # raised in place (same key, same second): reported again
        w.execute("UPDATE message_receipts SET status = 2 WHERE message_id = 'L1'")
    assert names(poller.poll_once()) == ["message_changed", "chat"]


def test_receipt_for_an_older_message_has_no_chat_event(settings: Settings) -> None:
    make_receipts(settings)
    with bridge(settings) as w:
        add(w, fx.ANA, "L2", "older mine", ts=now(), from_me=True)
        add(w, fx.ANA, "L3", "newer", ts=(datetime.now().astimezone() + timedelta(minutes=1)).isoformat(sep=" "))
    poller = live.Poller(settings)
    poller.poll_once()
    with bridge(settings) as w:
        w.execute("INSERT INTO message_receipts VALUES ('L2', ?, ?, 2, ?)", (fx.ANA, fx.ANA, now()))
    events = poller.poll_once()
    assert names(events) == ["message_changed"]
    assert events[0].data == {"chat_jid": fx.ANA, "id": "L2"}

import sqlite3
from datetime import datetime, timedelta

import pytest

from tests import conftest as fx
from wa_app import appdb, store
from wa_app.config import Settings


@pytest.fixture
def ro(settings: Settings):
    conn = store.open_ro(settings)
    yield conn
    conn.close()


def by_id(messages, mid: str):
    return next(m for m in messages if m.id == mid)


def add_messages(settings: Settings, chat_jid: str, count: int, *, start_id: int = 0) -> None:
    """`count` extra incoming messages, one minute apart, ending a day before now."""
    conn = sqlite3.connect(settings.store_path / "messages.db")
    end = datetime.now().astimezone().replace(microsecond=0) - timedelta(hours=24)
    for i in range(count):
        stamp = (end - timedelta(minutes=count - i)).isoformat(sep=" ")
        conn.execute(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?,?,?,?,?,0)",
            (f"P{start_id + i:04d}", chat_jid, chat_jid, f"msg {start_id + i}", stamp),
        )
    conn.commit()
    conn.close()


def test_pages_backwards(settings: Settings, ro) -> None:
    add_messages(settings, fx.LUKAS, 130)
    page1, more1 = store.list_messages_page(ro, fx.LUKAS)
    assert len(page1) == 60 and more1
    assert [m.text for m in page1][-1] == "Danke! Ich schick dir die Unterlagen morgen früh."  # newest last
    page2, more2 = store.list_messages_page(ro, fx.LUKAS, before_ts=page1[0].timestamp_iso)
    assert len(page2) == 60 and more2
    page3, more3 = store.list_messages_page(ro, fx.LUKAS, before_ts=page2[0].timestamp_iso)
    assert len(page3) == 12 and not more3
    ids = [m.id for m in page3 + page2 + page1]
    assert len(set(ids)) == 132
    stamps = [m.timestamp_iso for m in page3 + page2 + page1]
    assert stamps == sorted(stamps)
    assert [m.id for m in store.list_messages(ro, fx.LUKAS, limit=5)] == [m.id for m in page1[-5:]]


def test_merges_lid_and_phone_halves_in_time_order(ro) -> None:
    for jid in (fx.TOMO_LID, fx.TOMO_PHONE):
        texts = [m.text for m in store.list_messages(ro, jid)]
        assert texts == ["Pozdrav iz Njemačke", "Stigao sam"]


def test_unknown_chat_is_empty(ro) -> None:
    assert store.list_messages(ro, "999@s.whatsapp.net") == []


def test_reactions_summary_excludes_tombstones(ro) -> None:
    msgs = store.list_messages(ro, fx.EKIPA)
    forwarded = by_id(msgs, "E03")
    assert [(r.emoji, r.count, r.mine) for r in forwarded.reactions] == [("☀️", 3, True)]  # Ivana's "" is gone
    route = by_id(msgs, "E05")
    assert [(r.emoji, r.count, r.mine) for r in route.reactions] == [("🔥", 2, False), ("👍", 1, False)]
    # our own reaction filed under our LID still counts as ours
    assert [(r.emoji, r.mine) for r in by_id(msgs, "E13").reactions] == [("👍", True)]
    assert by_id(msgs, "E01").reactions == []


def test_reply_preview_of_deleted_quoted_message(ro) -> None:
    msgs = store.list_messages(ro, fx.EKIPA)
    quote = by_id(msgs, "E10").reply_to
    assert quote is not None
    assert quote.id == "E04" and quote.preview == "Message deleted"  # not the stored 'old quoted text'
    assert "old quoted text" not in repr(quote)
    voice = by_id(msgs, "E08").reply_to
    assert voice is not None and voice.sender_name == "Petra" and voice.preview == "🎤 Voice message (0:37)"
    assert by_id(msgs, "E01").reply_to is None


def test_edited_and_forwarded_flags(ro) -> None:
    msgs = store.list_messages(ro, fx.EKIPA)
    assert by_id(msgs, "E02").edited and not by_id(msgs, "E02").forwarded
    assert by_id(msgs, "E03").forwarded and not by_id(msgs, "E03").edited
    assert not by_id(msgs, "E01").edited and not by_id(msgs, "E01").forwarded


def test_poll_from_the_store(ro) -> None:
    poll = by_id(store.list_messages(ro, fx.EKIPA), "E06").poll
    assert poll is not None
    assert poll.question == "Ručak poslije?"
    assert [(o.text, o.votes) for o in poll.options] == [
        ("Konoba kod Mate", 3),
        ("Nosimo sendviče", 1),
        ("Vidjet ćemo", 0),
    ]
    assert poll.total == 4  # a retracted vote is not a voter
    assert by_id(store.list_messages(ro, fx.EKIPA), "E06").text is None


def test_poll_without_stored_options_shows_the_question(settings: Settings, ro) -> None:
    conn = sqlite3.connect(settings.store_path / "messages.db")
    conn.execute("DELETE FROM poll_options")
    conn.execute("DELETE FROM poll_current_votes")
    conn.commit()
    conn.close()
    poll = by_id(store.list_messages(ro, fx.EKIPA), "E06").poll
    assert poll is not None and poll.question == "Ručak poslije?" and poll.options == [] and poll.total == 0


def test_text_returned_raw(ro) -> None:
    assert by_id(store.list_messages(ro, fx.EKIPA), "E11").text == "<b>hi</b> <script>alert(1)</script>"


def test_contact_card_parsed(ro) -> None:
    msg = by_id(store.list_messages(ro, fx.EKIPA), "E12")
    assert msg.text is None
    assert msg.contact_cards == [store.ContactCard(name="Ivo Horvat", phones=["+385 91 000 0777", "+385 51 000 777"])]


def test_contact_cards_array_and_old_rows() -> None:
    text = "[Contact: Ana]\n📞 +1 555 0100\n\n[Contact: Bo]\n📞 +1 555 0101\n📞 +1 555 0102"
    assert store.parse_contact_cards(text) == [
        store.ContactCard("Ana", ["+1 555 0100"]),
        store.ContactCard("Bo", ["+1 555 0101", "+1 555 0102"]),
    ]
    assert store.parse_contact_cards("[Contact: Old Row]") == [store.ContactCard("Old Row", [])]
    assert store.parse_contact_cards("[Contact: x] and more text") is None
    assert store.parse_contact_cards("hello") is None


def test_import_messages_have_no_reply_affordance(settings: Settings, ro) -> None:
    conn = sqlite3.connect(settings.store_path / "messages.db")
    conn.execute(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?,?,?,?,?,0)",
        ("import-abc123", fx.LUKAS, "import:Someone", "from an export", "2020-01-01 10:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    msgs = store.list_messages(ro, fx.LUKAS)
    assert not by_id(msgs, "import-abc123").actionable
    assert all(m.actionable for m in msgs if not m.id.startswith("import-"))


def test_message_kinds(ro) -> None:
    msgs = store.list_messages(ro, fx.EKIPA)
    assert [m.id for m in msgs] == [f"E{i:02d}" for i in range(1, 16)]
    deleted = by_id(msgs, "E04")
    assert deleted.deleted and deleted.text is None and deleted.sender_name == "Ivana"
    assert by_id(msgs, "E02").from_me and by_id(msgs, "E02").sender_name is None
    assert by_id(msgs, "E14").from_me  # filed under our LID
    image = by_id(msgs, "E05")
    assert image.text == "Ovo je ruta, 11 km, cca 4h s pauzama"
    assert image.media is not None and image.media.type == "image" and not image.media.has_file
    voice = by_id(msgs, "E07").media
    assert voice is not None and voice.type == "ptt" and voice.has_file and voice.duration_s == 37
    assert by_id(msgs, "E15").system
    assert all(m.ticks is None for m in msgs)
    assert by_id(msgs, "E01").sender_name == "Nikola" and by_id(msgs, "E01").sender_jid == fx.NIKOLA


def test_chat_summary_and_members(ro, settings: Settings) -> None:
    app = appdb.connect(settings)
    try:
        chat = store.get_chat(ro, app, fx.EKIPA)
        assert chat is not None and chat.name == "Ekipa za vikend" and chat.kind == "group"
        archived = store.get_chat(ro, app, fx.OLD_ARCHIVED)
        assert archived is not None and archived.archived
        assert store.get_chat(ro, app, fx.TOMO_LID).jid == fx.TOMO_PHONE  # type: ignore[union-attr]
        assert store.get_chat(ro, app, "999@s.whatsapp.net") is None
    finally:
        app.close()
    line = store.members_line(ro, fx.EKIPA)
    assert line is not None
    assert set(line.split(", ")) == {"Nikola", "Petra", "Ana Kovačević", "Ivana", "you"}
    assert store.members_line(ro, fx.ANA) is None

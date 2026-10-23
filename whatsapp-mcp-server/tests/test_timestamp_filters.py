"""Timestamp filters compare instants, not text.

The bridge stores "2026-10-21 20:54:59+02:00". list_messages compared that
string against the caller's bound, so a bound in another offset (or "Z") moved
the cutoff by hours, and one chat's history mixing +01:00 and +02:00 misordered
across the DST change. The same text compare sat in get_message_context and in
the auto-transcribe age cutoff.
"""

import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest

import whatsapp
from lib import transcribe
from lib.utils import to_sql_utc

CHAT = "123456789@s.whatsapp.net"


def _insert(db: str, rows: list[tuple[str, str]], media_type: str | None = None) -> None:
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type)"
        " VALUES (?, ?, ?, ?, ?, 0, ?)",
        [(mid, CHAT, CHAT, None if media_type else mid, ts, media_type) for mid, ts in rows],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db(monkeypatch, temp_messages_db, temp_whatsapp_db):
    monkeypatch.setattr(whatsapp, "MESSAGES_DB_PATH", temp_messages_db)
    monkeypatch.setattr(whatsapp, "WHATSAPP_DB_PATH", temp_whatsapp_db)
    # Drop the fixture's "now" messages so only the rows under test match.
    conn = sqlite3.connect(temp_messages_db)
    conn.execute("DELETE FROM messages")
    conn.commit()
    conn.close()
    return temp_messages_db


def test_to_sql_utc_normalises_offsets_and_z():
    assert to_sql_utc("2026-10-21T00:00:00Z") == "2026-10-21 00:00:00"
    assert to_sql_utc("2026-10-21T02:00:00+02:00") == "2026-10-21 00:00:00"
    assert to_sql_utc(None) is None
    with pytest.raises(ValueError):
        to_sql_utc("last tuesday")


def test_list_messages_after_a_utc_bound(db):
    _insert(
        db,
        [
            ("before-cutoff", "2026-10-21 01:30:00+02:00"),  # 23:30 UTC the day before
            ("after-cutoff", "2026-10-21 08:00:00+02:00"),
        ],
    )

    got = whatsapp.list_messages(chat_jid=CHAT, after="2026-10-21T00:00:00Z")

    # As text, "2026-10-21 01:30:00+02:00" > "2026-10-21 00:00:00" let the
    # first one through.
    assert [m["id"] for m in got] == ["after-cutoff"]


def test_list_messages_before_with_mixed_offsets(db):
    _insert(
        db,
        [
            ("winter", "2026-10-25 02:10:00+01:00"),  # 01:10 UTC
            ("summer", "2026-10-25 02:30:00+02:00"),  # 00:30 UTC
        ],
    )

    got = whatsapp.list_messages(chat_jid=CHAT, before="2026-10-25T01:00:00Z")

    assert [m["id"] for m in got] == ["summer"]


def test_list_messages_rejects_a_bad_date(db):
    with pytest.raises(ValueError, match="ISO-8601"):
        whatsapp.list_messages(after="yesterday")


def test_message_context_orders_across_the_dst_change(db):
    _insert(
        db,
        [
            ("first", "2026-10-25 02:30:00+02:00"),  # 00:30 UTC
            ("second", "2026-10-25 02:10:00+01:00"),  # 01:10 UTC, smaller as text
        ],
    )

    ctx = whatsapp.get_message_context("first", before=5, after=5)

    assert [m.id for m in ctx.after] == ["second"]
    assert ctx.before == []


def test_transcribe_age_cutoff_compares_in_utc(db, monkeypatch):
    monkeypatch.setattr(transcribe, "MESSAGES_DB_PATH", db)
    cutoff = datetime.now(UTC) - timedelta(days=1)

    def bridge_text(instant: datetime, hours: int) -> str:
        # "+05:00" form, as the bridge writes it.
        return instant.astimezone(timezone(timedelta(hours=hours))).isoformat(sep=" ", timespec="seconds")

    _insert(
        db,
        [
            # An hour too old, but its +05:00 text reads 4h later than the cutoff.
            ("too-old", bridge_text(cutoff - timedelta(hours=1), 5)),
            # An hour inside, but its -05:00 text reads 4h earlier.
            ("recent", bridge_text(cutoff + timedelta(hours=1), -5)),
        ],
        media_type="ptt",
    )

    pending = transcribe._pending_voice_notes(limit=10, max_age_days=1)

    assert [mid for mid, _ in pending] == ["recent"]


def test_list_messages_orders_by_instant_across_the_dst_hour(db):
    # 25 Oct: 02:00-03:00 local happens twice. As text the summer 02:30 sorts
    # after the winter 02:10 although it is 40 minutes earlier.
    _insert(
        db,
        [
            ("summer", "2026-10-25 02:30:00+02:00"),  # 00:30 UTC
            ("winter", "2026-10-25 02:10:00+01:00"),  # 01:10 UTC
        ],
    )

    got = whatsapp.list_messages(chat_jid=CHAT)

    assert [m["id"] for m in got] == ["winter", "summer"]


def test_the_repeated_0230_filters_and_sorts_as_two_instants(db):
    # The same wall-clock text twice, an hour apart: 02:30 summer time, then
    # 02:30 winter time once the clocks went back.
    _insert(
        db,
        [
            ("summer-0230", "2026-10-25 02:30:00+02:00"),  # 00:30 UTC
            ("winter-0230", "2026-10-25 02:30:00+01:00"),  # 01:30 UTC
        ],
    )

    assert [m["id"] for m in whatsapp.list_messages(chat_jid=CHAT)] == ["winter-0230", "summer-0230"]
    assert [m["id"] for m in whatsapp.list_messages(chat_jid=CHAT, after="2026-10-25T01:00:00Z")] == ["winter-0230"]
    assert [m["id"] for m in whatsapp.list_messages(chat_jid=CHAT, before="2026-10-25T01:00:00Z")] == ["summer-0230"]

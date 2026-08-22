"""Tests for the unified identity directory reader."""

import importlib
import sqlite3
import tempfile
from pathlib import Path

import pytest

IDENTITY_SCHEMA = """
CREATE TABLE identities (
    jid TEXT PRIMARY KEY, lid TEXT, phone_jid TEXT, phone TEXT, redacted_phone TEXT,
    kind TEXT NOT NULL DEFAULT 'user',
    first_name TEXT, full_name TEXT, push_name TEXT, business_name TEXT,
    nickname TEXT, display_name TEXT, chat_name TEXT,
    is_business BOOLEAN DEFAULT 0, is_contact BOOLEAN DEFAULT 0, has_chat BOOLEAN DEFAULT 0,
    message_count INTEGER DEFAULT 0, participant_count INTEGER, is_announce BOOLEAN DEFAULT 0,
    owner_jid TEXT, first_seen TIMESTAMP, last_seen TIMESTAMP, sources TEXT,
    created_at TIMESTAMP, updated_at TIMESTAMP
);
CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP, merged_into TEXT);
"""

# Jane is the interesting case: one person WhatsApp files under two identities.
JANE = (
    "441234@s.whatsapp.net",
    "999@lid",
    "441234@s.whatsapp.net",
    "441234",
    None,
    "user",
    "Jane",
    "Jane Doe",
    "jd",
    None,
    None,
    "Jane Doe",
    None,
    0,
    1,
    1,
    42,
    None,
    0,
    None,
    None,
    "2026-08-01T10:00:00",
    "contacts,chats",
    None,
    "2026-08-20T09:00:00",
)
GROUP = (
    "120363@g.us",
    None,
    None,
    None,
    None,
    "group",
    None,
    "Project Alpha",
    None,
    None,
    None,
    "Project Alpha",
    "Project Alpha",
    0,
    0,
    1,
    300,
    12,
    1,
    "441234@s.whatsapp.net",
    None,
    "2026-08-19T10:00:00",
    "chats,groups",
    None,
    "2026-08-20T09:00:00",
)
LID_ONLY = (
    "777@lid",
    "777@lid",
    None,
    None,
    "+44*****99",
    "user",
    None,
    None,
    "Unknown Caller",
    None,
    None,
    "Unknown Caller",
    None,
    0,
    0,
    1,
    2,
    None,
    0,
    None,
    None,
    "2026-07-01T10:00:00",
    "contacts",
    None,
    "2026-08-20T09:00:00",
)


@pytest.fixture
def identity_module(monkeypatch):
    """A lib.identity bound to a throwaway store with the directory populated."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    conn = sqlite3.connect(db_path)
    conn.executescript(IDENTITY_SCHEMA)
    conn.executemany(f"INSERT INTO identities VALUES ({','.join('?' * 25)})", [JANE, GROUP, LID_ONLY])
    conn.execute("INSERT INTO chats VALUES (?, ?, ?, ?)", ("999@lid", "Jane", None, "441234@s.whatsapp.net"))
    conn.execute("INSERT INTO chats VALUES (?, ?, ?, ?)", ("441234@s.whatsapp.net", "Jane Doe", None, None))
    conn.commit()
    conn.close()

    import lib.identity as identity

    importlib.reload(identity)
    monkeypatch.setattr(identity, "MESSAGES_DB_PATH", db_path)
    yield identity
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def empty_module(monkeypatch):
    """A lib.identity bound to a store that predates the directory."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()

    import lib.identity as identity

    importlib.reload(identity)
    monkeypatch.setattr(identity, "MESSAGES_DB_PATH", db_path)
    yield identity
    Path(db_path).unlink(missing_ok=True)


@pytest.mark.parametrize(
    "handle",
    [
        "441234@s.whatsapp.net",  # canonical jid
        "999@lid",  # the other half of the same person
        "999",  # bare lid
        "441234",  # bare phone number
        "Jane Doe",  # exact display name
        "jane doe",  # display name, wrong case
    ],
)
def test_get_identity_resolves_every_handle_to_one_record(identity_module, handle):
    """Both halves of a split identity, and every way of writing them, land on the same row."""
    identity = identity_module.get_identity(handle)
    assert identity is not None, f"{handle!r} did not resolve"
    assert identity.jid == "441234@s.whatsapp.net"
    assert identity.lid == "999@lid"
    assert identity.display_name == "Jane Doe"


def test_get_identity_returns_none_for_unknown(identity_module):
    assert identity_module.get_identity("does-not-exist") is None
    assert identity_module.get_identity("") is None
    assert identity_module.get_identity("   ") is None


def test_resolve_jid_returns_canonical_form(identity_module):
    assert identity_module.resolve_jid("999@lid") == "441234@s.whatsapp.net"
    assert identity_module.resolve_jid("nobody") is None


def test_directory_unavailable_degrades_quietly(empty_module):
    """A store without the table must return empty, not raise — callers fall back."""
    assert empty_module.directory_available() is False
    assert empty_module.get_identity("441234") is None
    assert empty_module.list_identities() == []
    assert empty_module.directory_stats()["available"] is False


def test_list_identities_query_spans_names_and_jids(identity_module):
    by_name = identity_module.list_identities(query="jane")
    by_phone = identity_module.list_identities(query="441234")
    by_lid = identity_module.list_identities(query="999")

    assert [i.jid for i in by_name] == ["441234@s.whatsapp.net"]
    assert [i.jid for i in by_phone] == ["441234@s.whatsapp.net"]
    assert [i.jid for i in by_lid] == ["441234@s.whatsapp.net"]


def test_list_identities_filters(identity_module):
    assert [i.jid for i in identity_module.list_identities(kind="group")] == ["120363@g.us"]
    assert {i.jid for i in identity_module.list_identities(kind="user")} == {
        "441234@s.whatsapp.net",
        "777@lid",
    }
    assert [i.jid for i in identity_module.list_identities(is_contact=True)] == ["441234@s.whatsapp.net"]


def test_list_identities_sorting_and_paging(identity_module):
    by_messages = identity_module.list_identities(sort_by="messages")
    assert [i.message_count for i in by_messages] == [300, 42, 2]

    by_name = identity_module.list_identities(sort_by="name")
    assert [i.display_name for i in by_name] == ["Jane Doe", "Project Alpha", "Unknown Caller"]

    first_page = identity_module.list_identities(sort_by="name", limit=2, page=0)
    second_page = identity_module.list_identities(sort_by="name", limit=2, page=1)
    assert [i.jid for i in first_page] + [i.jid for i in second_page] == [i.jid for i in by_name]


def test_to_dict_shapes_users_and_groups_differently(identity_module):
    user = identity_module.get_identity("441234").to_dict()
    group = identity_module.get_identity("120363@g.us").to_dict()

    assert user["names"]["full_name"] == "Jane Doe"
    assert user["lid"] == "999@lid"
    assert "participant_count" not in user

    assert group["participant_count"] == 12
    assert group["is_announce"] is True
    assert "names" not in group


def test_redacted_phone_surfaces_only_when_the_real_one_is_unknown(identity_module):
    lid_only = identity_module.get_identity("777@lid").to_dict()
    jane = identity_module.get_identity("441234").to_dict()

    assert lid_only["redacted_phone"] == "+44*****99"
    assert lid_only["phone"] is None
    assert "redacted_phone" not in jane


def test_directory_stats_reports_coverage(identity_module):
    stats = identity_module.directory_stats()

    assert stats["available"] is True
    assert stats["total"] == 3
    assert stats["users"] == 2
    assert stats["groups"] == 1
    assert stats["lid_and_phone_known"] == 1
    assert stats["lid_only"] == 1
    assert stats["merged_lid_chats"] == 1

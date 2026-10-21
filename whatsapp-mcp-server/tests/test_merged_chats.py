"""Tools taking a chat JID must follow chats.merged_into.

The bridge folds a LID-filed chat into its phone-JID twin and leaves the LID row
behind as an empty shell. get_direct_chat_by_contact did `IN (...) LIMIT 1` with
no ORDER BY and no merged_into filter, and returned the shell for 31 of 50
merged pairs on a real store; get_chat, list_messages and check_inbox handed an
old LID straight to SQL and read the shell's zero messages.
"""

import shutil
import sqlite3

import pytest

import whatsapp
from lib.utils import canonical_chat_jid
from tests.conftest import MERGED_LID

PHONE_JID = "123456789@s.whatsapp.net"


@pytest.fixture
def store(monkeypatch, tmp_path, temp_messages_db, temp_whatsapp_db):
    """The fixture DBs copied into a private store dir.

    Private because check_inbox writes its cursor file next to messages.db,
    and the shared fixture lives directly in /tmp.
    """
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    messages_db = str(store_dir / "messages.db")
    shutil.copy(temp_messages_db, messages_db)

    import lib.identity as identity
    import lib.utils as utils

    for mod in (whatsapp, identity, utils):
        monkeypatch.setattr(mod, "MESSAGES_DB_PATH", messages_db, raising=False)
        monkeypatch.setattr(mod, "WHATSAPP_DB_PATH", temp_whatsapp_db, raising=False)
    return messages_db


def _conn(path: str) -> sqlite3.Connection:
    return sqlite3.connect(path)


def test_canonical_follows_a_merge(store):
    with _conn(store) as conn:
        assert canonical_chat_jid(conn, MERGED_LID) == PHONE_JID
        assert canonical_chat_jid(conn, PHONE_JID) == PHONE_JID
        assert canonical_chat_jid(conn, "unknown@lid") == "unknown@lid"


def test_canonical_treats_empty_merged_into_as_unmerged(store):
    with _conn(store) as conn:
        conn.execute("UPDATE chats SET merged_into = '' WHERE jid = ?", (PHONE_JID,))
        assert canonical_chat_jid(conn, MERGED_LID) == PHONE_JID


def test_canonical_survives_a_cycle(store, caplog):
    with _conn(store) as conn:
        conn.execute("UPDATE chats SET merged_into = ? WHERE jid = ?", (MERGED_LID, PHONE_JID))
        assert canonical_chat_jid(conn, MERGED_LID) == MERGED_LID
    assert "loops" in caplog.text


def test_canonical_on_a_store_without_the_column():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT)")
    assert canonical_chat_jid(conn, MERGED_LID) == MERGED_LID


def test_get_chat_resolves_an_old_lid(store):
    chat = whatsapp.get_chat(MERGED_LID)
    assert chat is not None
    assert chat["jid"] == PHONE_JID
    assert chat["last_message"] is not None


def test_list_messages_by_old_lid_returns_the_merged_history(store):
    messages = whatsapp.list_messages(chat_jid=MERGED_LID)
    assert {m["id"] for m in messages} == {"msg1", "msg2"}


def test_direct_chat_skips_the_merged_shell(store):
    chat = whatsapp.get_direct_chat_by_contact("123456789")
    assert chat is not None
    assert chat["jid"] == PHONE_JID


def test_direct_chat_prefers_the_phone_jid_over_an_unmerged_lid(store):
    # A split chat whose merge has not happened yet: both rows are live.
    with _conn(store) as conn:
        conn.execute("UPDATE chats SET merged_into = NULL WHERE jid = ?", (MERGED_LID,))
    chat = whatsapp.get_direct_chat_by_contact("123456789")
    assert chat is not None
    assert chat["jid"] == PHONE_JID


def test_check_inbox_by_old_lid_sees_new_messages(store, monkeypatch):
    monkeypatch.setenv("WHATSAPP_MCP_TOOLSETS", "all")
    import importlib

    import main

    importlib.reload(main)

    first = main.check_inbox(chat_jid=MERGED_LID)
    assert first["messages"] == []
    with _conn(store) as conn:
        conn.execute(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, ?, ?, ?, ?)",
            ("msg3", PHONE_JID, PHONE_JID, "after the merge", "2026-10-21 20:00:00+02:00", 0),
        )
    second = main.check_inbox(chat_jid=MERGED_LID)
    assert [m["content"] for m in second["messages"]] == ["after the merge"]

"""Edit/delete markers and the get_message_edits / list_calls tools.

The bridge keeps a revoked message's text (the store is the user's archive),
so without the "(deleted)" marker an agent would quote a withdrawn message as
if it still stood. Each reader must also keep working on a store from before
bridge migration 009, which is what the unmigrated fixture checks.
"""

from __future__ import annotations

import importlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from lib import changes

CHAT = "123456789@s.whatsapp.net"
LID = "555000111@lid"  # merged into CHAT in the fixture
GROUP = "987654321@g.us"
MIGRATION = Path(__file__).resolve().parents[2] / "whatsapp-bridge" / "migrations" / "009_edits_calls.sql"


def migrate(db: str) -> None:
    """Apply 009 the way the bridge does: guarded ALTERs, then the file."""
    conn = sqlite3.connect(db)
    for col in ("edited_at TIMESTAMP", "deleted_at TIMESTAMP", "deleted_by TEXT"):
        try:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col}")
        except sqlite3.OperationalError as exc:
            assert "duplicate column" in str(exc)
    conn.executescript(MIGRATION.read_text())
    conn.commit()
    conn.close()


@pytest.fixture
def store(temp_messages_db, monkeypatch):
    """The conftest store, migrated, with msg1 edited and msg2 revoked."""
    migrate(temp_messages_db)
    migrate(temp_messages_db)  # idempotent
    conn = sqlite3.connect(temp_messages_db)
    conn.execute("UPDATE messages SET content = 'Hello world!', is_edited = 1, edit_count = 2 WHERE id = 'msg1'")
    conn.executemany(
        "INSERT INTO message_edits (message_id, chat_jid, edit_id, old_content, new_content, edited_at, editor_jid, source)"
        " VALUES ('msg1', '123456789@s.whatsapp.net', ?, ?, ?, ?, ?, ?)",
        [
            # Stored out of order and with mixed offsets: 10:05+02:00 is 08:05 UTC,
            # before 09:00+00:00, though it sorts after it as text.
            ("E2", "Hello world", "Hello world!", "2026-10-20 09:00:00+00:00", CHAT, "live"),
            ("E1", "Hello wrld", "Hello world", "2026-10-20 10:05:00+02:00", CHAT, "history"),
        ],
    )
    conn.execute("UPDATE messages SET deleted_at = '2026-10-20 11:00:00+00:00', deleted_by = 'me' WHERE id = 'msg2'")
    conn.commit()
    conn.close()
    _point_at(monkeypatch, temp_messages_db)
    return temp_messages_db


def _point_at(monkeypatch, db: str) -> None:
    import whatsapp

    monkeypatch.setattr(changes, "MESSAGES_DB_PATH", db)
    monkeypatch.setattr(whatsapp, "MESSAGES_DB_PATH", db)


def _by_id(messages: list[dict]) -> dict[str, dict]:
    return {m["id"]: m for m in messages}


@pytest.mark.parametrize(
    ("content", "edited", "deleted", "want"),
    [
        ("hi", 0, None, "hi"),
        ("hi", 1, None, "hi (edited)"),
        ("hi", 0, "2026-10-20", "(deleted) hi"),
        ("hi", 1, "2026-10-20", "(deleted) hi (edited)"),
        (None, 0, "2026-10-20", "(deleted)"),
        (None, 1, None, None),
    ],
)
def test_mark_content(content, edited, deleted, want):
    assert changes.mark_content(content, edited, deleted) == want


def test_list_messages_marks_edited_and_deleted(store):
    import whatsapp

    got = _by_id(whatsapp.list_messages(chat_jid=CHAT))
    assert got["msg1"]["content"] == "Hello world! (edited)"
    assert got["msg1"]["edited"] is True and "deleted" not in got["msg1"]
    assert got["msg2"]["content"] == "(deleted) Hi there"
    assert got["msg2"]["deleted"] is True and "edited" not in got["msg2"]


def test_get_message_context_marks_changes(store):
    import whatsapp

    conn = sqlite3.connect(store)  # context excludes rows with the target's exact timestamp
    conn.execute("UPDATE messages SET timestamp = datetime(timestamp, '+1 minute') WHERE id = 'msg2'")
    conn.commit()
    conn.close()
    ctx = whatsapp.get_message_context("msg1", before=5, after=5).to_dict()
    everything = _by_id([ctx["message"], *ctx["before"], *ctx["after"]])
    assert everything["msg1"]["content"].endswith("(edited)")
    assert everything["msg2"]["content"].startswith("(deleted)")


def test_readers_work_on_a_store_before_migration(temp_messages_db, monkeypatch):
    import whatsapp

    _point_at(monkeypatch, temp_messages_db)
    got = _by_id(whatsapp.list_messages(chat_jid=CHAT))
    assert got["msg1"]["content"] == "Hello world"
    assert "edited" not in got["msg1"] and "deleted" not in got["msg2"]
    assert whatsapp.get_message_context("msg1").message.content == "Hello world"

    edits = changes.get_message_edits("msg1")
    assert edits["edits"] == [] and "note" in edits
    calls = changes.list_calls()
    assert calls["calls"] == [] and "note" in calls


def test_get_message_edits_oldest_first_by_real_time(store):
    got = changes.get_message_edits("msg1")
    assert got["edited"] is True and got["edit_count"] == 2 and got["deleted"] is False
    assert [e["new_content"] for e in got["edits"]] == ["Hello world", "Hello world!"]
    assert got["edits"][0]["source"] == "history"


def test_get_message_edits_shows_status_and_flags_rejected(store):
    conn = sqlite3.connect(store)
    conn.execute(
        "INSERT INTO message_edits (message_id, chat_jid, edit_id, new_content, edited_at, editor_jid, source, status)"
        " VALUES ('msg1', ?, 'F1', 'forged', '2026-10-20 09:30:00+00:00', '4915187654321@s.whatsapp.net', 'live', 'rejected')",
        (CHAT,),
    )
    conn.commit()
    conn.close()
    got = changes.get_message_edits("msg1")
    assert [e["status"] for e in got["edits"]] == ["applied", "applied", "rejected"]
    assert got["content"] == "Hello world!"  # the rejected text never becomes the content
    assert "1 edit(s)" in got["note"]


def test_get_message_edits_before_status_column(store):
    conn = sqlite3.connect(store)
    conn.execute("ALTER TABLE message_edits DROP COLUMN status")
    conn.commit()
    conn.close()
    got = changes.get_message_edits("msg1")
    assert [e["status"] for e in got["edits"]] == ["applied", "applied"]
    assert "note" not in got


def test_get_message_edits_resolves_a_merged_lid(store):
    assert changes.get_message_edits("msg1", chat_jid=LID)["chat_jid"] == CHAT


def test_get_message_edits_reports_deletion(store):
    got = changes.get_message_edits("msg2")
    assert got["deleted"] is True and got["deleted_by"] == "me" and got["content"] == "Hi there"


def test_get_message_edits_unknown_message_raises(store):
    with pytest.raises(ValueError, match="not found"):
        changes.get_message_edits("nope")
    with pytest.raises(ValueError, match="not found"):
        changes.get_message_edits("msg1", chat_jid=GROUP)


def _add_calls(db: str) -> None:
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO calls (call_id, chat_jid, caller_jid, direction, is_video, is_group, started_at,"
        " duration_seconds, outcome, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("C1", CHAT, CHAT, "incoming", 0, 0, "2026-10-20 10:30:00+02:00", None, "missed", "live"),
            # 09:00 UTC is after 10:30+02:00 (08:30 UTC) even though it sorts first as text.
            ("C2", CHAT, "", "outgoing", 1, 0, "2026-10-20 09:00:00+00:00", 75, "answered", "live"),
            ("C3", GROUP, CHAT, "incoming", 0, 1, "2026-10-19 18:00:00+00:00", None, "unknown", "message"),
        ],
    )
    conn.commit()
    conn.close()


def test_list_calls_newest_first(store):
    _add_calls(store)
    got = changes.list_calls()
    assert [c["call_id"] for c in got["calls"]] == ["C2", "C1", "C3"]
    c2 = got["calls"][0]
    assert c2["video"] is True and c2["duration_seconds"] == 75 and c2["outcome"] == "answered"
    assert c2["caller_jid"] is None
    assert got["calls"][2]["group"] is True and "note" in got["calls"][2]
    assert got["calls"][2]["chat_name"] == "Test Group"


def test_list_calls_filters(store):
    _add_calls(store)
    assert {c["call_id"] for c in changes.list_calls(chat_jid=LID)["calls"]} == {"C1", "C2"}
    assert [c["call_id"] for c in changes.list_calls(since="2026-10-20T08:45:00Z")["calls"]] == ["C2"]
    assert len(changes.list_calls(limit=1)["calls"]) == 1
    with pytest.raises(ValueError):
        changes.list_calls(limit=0)
    with pytest.raises(ValueError):
        changes.list_calls(since="yesterday-ish")


def test_list_calls_sees_the_seeded_call_messages(temp_messages_db, monkeypatch):
    conn = sqlite3.connect(temp_messages_db)
    conn.execute(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type)"
        " VALUES ('call-ABC', ?, '123456789', '📞 Missed call from Test User', '2026-09-01 18:00:00+02:00', 0, 'call')",
        (CHAT,),
    )
    conn.commit()
    conn.close()
    migrate(temp_messages_db)
    _point_at(monkeypatch, temp_messages_db)
    (call,) = changes.list_calls()["calls"]
    assert call["call_id"] == "ABC" and call["outcome"] == "missed" and call["caller_jid"] == CHAT


@pytest.fixture
def main_with_store(store, tmp_path, monkeypatch):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    db = str(store_dir / "messages.db")
    shutil.copy(store, db)
    import lib.utils as utils

    monkeypatch.setattr(utils, "MESSAGES_DB_PATH", db)
    _point_at(monkeypatch, db)
    monkeypatch.setenv("WHATSAPP_MCP_TOOLSETS", "all")
    import main

    importlib.reload(main)
    return main, db


def test_check_inbox_marks_changes(main_with_store):
    main, db = main_with_store
    main.check_inbox()  # first call sets the cursor
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, is_edited, deleted_at)"
        " VALUES ('new1', ?, ?, 'typo fixed', '2026-10-21 20:00:00+02:00', 0, 1, NULL),"
        "        ('new2', ?, ?, NULL, '2026-10-21 20:01:00+02:00', 0, 0, '2026-10-21 18:02:00+00:00')",
        (CHAT, CHAT, CHAT, CHAT),
    )
    conn.commit()
    conn.close()
    got = main.check_inbox()["messages"]
    assert [m["content"] for m in got] == ["typo fixed (edited)", "(deleted)"]
    assert got[0]["edited"] is True and got[1]["deleted"] is True


def test_tools_are_registered_read_only(main_with_store):
    main, _ = main_with_store
    tools = {t.name: t for t in main.mcp._tool_manager.list_tools()}
    for name in ("get_message_edits", "list_calls"):
        ann = tools[name].annotations
        assert ann.read_only_hint is True and ann.idempotent_hint is True and ann.open_world_hint is False
    assert main.get_message_edits("msg1")["edit_count"] == 2
    assert main.list_calls()["count"] == 0


def test_recall_marks_changes(store, monkeypatch):
    np = pytest.importorskip("numpy")
    from lib import recall as recall_mod

    monkeypatch.setattr(recall_mod, "MESSAGES_DB_PATH", store)
    monkeypatch.setattr(recall_mod, "_ensure_indexer_running", lambda: None)
    monkeypatch.setattr(recall_mod, "_ensure_unloader_running", lambda: None)

    def fake_embed(texts: list[str]):
        out = np.zeros((len(texts), recall_mod.EMBED_DIM), dtype=np.float32)
        out[:, 0] = 1.0
        return out

    monkeypatch.setattr(recall_mod, "_embed", fake_embed)
    monkeypatch.setattr(recall_mod, "_get_model", lambda: object())
    recall_mod._index_pending()
    got = {r["message_id"]: r for r in recall_mod.recall("hello", limit=5)["results"]}
    assert got["msg1"]["content"] == "Hello world! (edited)" and got["msg1"]["edited"] is True
    assert got["msg2"]["content"] == "(deleted) Hi there" and got["msg2"]["deleted"] is True

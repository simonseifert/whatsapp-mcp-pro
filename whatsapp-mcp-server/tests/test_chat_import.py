"""Tests for lib.chat_import (WhatsApp "Export chat" importer).

All names and numbers are fictional. iOS exports put U+200E in front of system
lines, attachments and omitted markers; it is spelled LRM below so the fixtures
stay readable and the source stays free of invisible characters.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from lib import chat_import as ci
from lib import recall as recall_mod

LRM = chr(0x200E)
NNBSP = chr(0x202F)

BOB = "4915112345678@s.whatsapp.net"
ME = "4915199999999@s.whatsapp.net"
GROUP = "120363000000000001@g.us"
CAROL_LID = "111111111111111@lid"
CAROL = "4915100000001@s.whatsapp.net"
DAVE = "4915100000002@s.whatsapp.net"
ZAGREB = ZoneInfo("Europe/Zagreb")

IOS_DIRECT = "\n".join(
    [
        f"[23.10.25, 09:41:12] Bob: {LRM}Messages and calls are end-to-end encrypted. No one outside of this chat can read them.",
        "[23.10.25, 09:41:12] Bob: Hi Alice",
        "[23.10.25, 09:42:00] Alice: Hello",
        "second line",
        "third line",
        f"[23.10.25, 09:43:05] Bob: {LRM}<attached: 00000012-PHOTO-2025-10-23-09-43-05.jpg>",
        f"[23.10.25, 09:44:00] Bob: {LRM}image omitted",
        f"[23.10.25, 09:45:00] Alice: {LRM}This message was deleted.",
        f"[23.10.25, 09:46:00] Bob: Fixed typo {LRM}<This message was edited>",
        f"[23.10.25, 09:47:00] Bob: {LRM}Missed voice call",
        "[23.10.25, 09:48:00] Bob: location: https://maps.google.com/?q=45.0,14.0",
        "[23.10.25, 09:49:30] Alice: brand new",
    ]
)

ANDROID_GROUP = "\n".join(
    [
        "23/10/2025, 09:41 - Messages and calls are end-to-end encrypted. No one outside of this chat can read them.",
        '23/10/2025, 09:41 - Carol created group "Test"',
        "23/10/2025, 09:42 - Carol added Dave",
        "23/10/2025, 09:43 - Carol: Welcome",
        "23/10/2025, 09:44 - Dave: IMG-20251023-WA0001.jpg (file attached)",
        "nice pic",
        "23/10/2025, 09:45 - +49 151 12345679: hi from a number",
        "23/10/2025, 09:46 - Dave: <Media omitted>",
        "23/10/2025, 09:47 - Eve: who am I",
        "23/10/2025, 09:48 - Alice: hello all",
    ]
)


def _make_store(path: Path) -> str:
    """A messages.db with the bridge's columns (internal/database/store.go)."""
    db = str(path / "messages.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP, merged_into TEXT);
        CREATE TABLE messages (
            id TEXT, chat_jid TEXT, sender TEXT, sender_name TEXT, content TEXT, timestamp TIMESTAMP,
            is_from_me BOOLEAN, media_type TEXT, filename TEXT, url TEXT, media_key BLOB, file_sha256 BLOB,
            file_enc_sha256 BLOB, file_length INTEGER, direct_path TEXT, is_edited BOOLEAN DEFAULT 0,
            PRIMARY KEY (id, chat_jid)
        );
        CREATE TABLE identities (
            jid TEXT PRIMARY KEY, lid TEXT, phone_jid TEXT, phone TEXT, kind TEXT NOT NULL DEFAULT 'user',
            first_name TEXT, full_name TEXT, push_name TEXT, business_name TEXT, nickname TEXT,
            display_name TEXT, chat_name TEXT
        );
        CREATE TABLE contact_nicknames (jid TEXT PRIMARY KEY, nickname TEXT NOT NULL, source TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
        [(BOB, "Bob", "2025-10-23 10:00:00+02:00"), (GROUP, "Test", "2025-10-23 10:00:00+02:00")],
    )
    conn.executemany(
        "INSERT INTO identities (jid, lid, phone_jid, phone, display_name, push_name) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (BOB, None, BOB, "4915112345678", "Bob", "Bobby"),
            (CAROL, CAROL_LID, CAROL, "4915100000001", "Carol", None),
            (DAVE, None, DAVE, "4915100000002", "David", "Dave"),
            (ME, None, ME, "4915199999999", "Alice", None),
        ],
    )
    rows = [
        # What the bridge already has of the iOS chat: two plain texts, an
        # edited one, and a location it renders differently from the export.
        ("3EB0AAA1", BOB, BOB, "Hi Alice", "2025-10-23 09:41:12+02:00", 0, ""),
        ("3EB0AAA2", BOB, ME, "Hello\nsecond line\nthird line", "2025-10-23 09:42:00+02:00", 1, ""),
        ("3EB0AAA3", BOB, BOB, "Fixed typo", "2025-10-23 09:46:00+02:00", 0, ""),
        ("3EB0AAA4", BOB, BOB, "[location 45.0,14.0]", "2025-10-23 09:48:00+02:00", 0, ""),
        # The group: Carol is stored under her LID.
        ("3EB0BBB1", GROUP, CAROL_LID, "Welcome", "2025-10-23 09:43:20+02:00", 0, ""),
    ]
    conn.executemany(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def store(tmp_path: Path) -> str:
    return _make_store(tmp_path)


def _zip(tmp_path: Path, name: str, chat: str, media: dict[str, bytes], transcript: str = "_chat.txt") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(transcript, chat.encode())
        for n, data in media.items():
            zf.writestr(n, data)
    return path


def _plan(db: str, export: ci.Export, chat: str, **kw: object) -> ci.Plan:
    with sqlite3.connect(db) as conn:
        return ci.build_plan(conn, export, chat, store_dir=os.path.dirname(db), **kw)  # type: ignore[arg-type]


def _apply(db: str, plan: ci.Plan, export: ci.Export) -> int:
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        return ci.apply_plan(conn, plan, export)
    finally:
        conn.close()


# --------------------------------------------------------------------------- parsing


def test_ios_lines_are_classified():
    fmt, msgs = ci.parse_export(IOS_DIRECT, media_names={"00000012-PHOTO-2025-10-23-09-43-05.jpg"})
    assert (fmt.style, fmt.date_order, fmt.has_seconds) == ("ios", "DMY", True)
    kinds = [(m.sender_name, m.kind) for m in msgs]
    assert kinds == [
        ("Bob", "system"),
        ("Bob", "text"),
        ("Alice", "text"),
        ("Bob", "media"),
        ("Bob", "omitted"),
        ("Alice", "deleted"),
        ("Bob", "text"),
        ("Bob", "system"),
        ("Bob", "text"),
        ("Alice", "text"),
    ]
    assert msgs[2].text == "Hello\nsecond line\nthird line"
    assert msgs[3].attachment == "00000012-PHOTO-2025-10-23-09-43-05.jpg"
    assert msgs[6].text == "Fixed typo" and msgs[6].is_edited
    assert msgs[1].local == datetime(2025, 10, 23, 9, 41, 12)


def test_android_group_system_lines_media_and_captions():
    fmt, msgs = ci.parse_export(ANDROID_GROUP)
    assert (fmt.style, fmt.date_order, fmt.has_seconds) == ("android", "DMY", False)
    assert sum(1 for m in msgs if m.kind == "system") == 3
    media = next(m for m in msgs if m.kind == "media")
    assert (media.attachment, media.text) == ("IMG-20251023-WA0001.jpg", "nice pic")
    assert [m.sender_name for m in msgs if m.kind == "omitted"] == ["Dave"]
    assert "+49 151 12345679" in {m.sender_name for m in msgs}


def test_header_lookalikes_stay_message_text():
    text = "\n".join(
        [
            "[23.10.25, 09:41:12] Bob: forwarding this:",
            "23/10/2025, 09:00 - Carol: an Android line pasted into an iOS chat",
            "31.02.25, 10:00 is not a date either",
            "[23.10.25, 09:42:00] Alice: ok",
        ]
    )
    _, msgs = ci.parse_export(text)
    assert [m.sender_name for m in msgs] == ["Bob", "Alice"]
    assert msgs[0].text.count("\n") == 2


def test_android_us_12h_month_first():
    text = "\n".join(
        [
            "10/23/25, 9:41 AM - Bob: morning",
            "10/23/25, 1:05 PM - Alice: afternoon",
            f"10/23/25, 12:06{NNBSP}AM - Bob: just after midnight",
        ]
    )
    fmt, msgs = ci.parse_export(text)
    assert fmt.date_order == "MDY"
    assert [m.local for m in msgs] == [
        datetime(2025, 10, 23, 9, 41),
        datetime(2025, 10, 23, 13, 5),
        datetime(2025, 10, 23, 0, 6),
    ]


def test_croatian_android_and_german_ios():
    hr = "\n".join(
        [
            "23. 10. 2025. 09:41 - Bob: Bok",
            "23. 10. 2025. 09:42 - Bob: <Medijski zapis izostavljen>",
            "23. 10. 2025. 09:43 - Alice: Ova poruka je izbrisana",
            "23. 10. 2025. 09:44 - Bob: Ispravljeno <Ova poruka je uređena>",
        ]
    )
    fmt, msgs = ci.parse_export(hr)
    assert (fmt.style, fmt.date_order) == ("android", "DMY")
    assert [m.kind for m in msgs] == ["text", "omitted", "deleted", "text"]
    assert msgs[3].text == "Ispravljeno" and msgs[3].is_edited

    de = "\n".join(
        [
            f"[01.02.25, 09:41:12] Bob: {LRM}Bild weggelassen",
            f"[01.02.25, 09:41:13] Bob: {LRM}<Anhang: 00000013-AUDIO-2025-02-01-09-41-13.opus>",
            f"[01.02.25, 09:41:14] Alice: {LRM}Diese Nachricht wurde gelöscht.",
            f"[01.02.25, 09:41:15] Bob: {LRM}Alice hat Bob hinzugefügt.",
        ]
    )
    fmt, msgs = ci.parse_export(de)
    # Dots are never month-first, so 01.02 is the first of February.
    assert fmt.date_order == "DMY" and msgs[0].local.month == 2
    assert [m.kind for m in msgs] == ["omitted", "media", "deleted", "system"]
    assert ci.media_type_for(msgs[1].attachment or "") == "audio"


def test_ambiguous_dates_need_the_whole_file_or_an_override():
    text = "01/02/2025, 10:00 - Bob: x\n03/04/2025, 10:00 - Alice: y"
    with pytest.raises(ci.ChatImportError, match="date-order"):
        ci.parse_export(text)
    _, msgs = ci.parse_export(text, date_order="MDY")
    assert msgs[0].local == datetime(2025, 1, 2, 10, 0)
    # One unambiguous line anywhere settles every other line.
    fmt, msgs = ci.parse_export(text + "\n13/04/2025, 10:00 - Bob: z")
    assert fmt.date_order == "DMY" and msgs[0].local == datetime(2025, 2, 1, 10, 0)


def test_dst_repeated_hour_follows_transcript_order():
    text = "\n".join(
        [
            "26/10/2025, 02:10 - Bob: a",
            "26/10/2025, 02:50 - Alice: b",
            "26/10/2025, 02:05 - Bob: c",
            "26/10/2025, 02:40 - Alice: d",
            "26/10/2025, 03:10 - Bob: e",
        ]
    )
    _, msgs = ci.parse_export(text)
    res = ci.resolve_times(msgs, ZAGREB)
    assert [r.stored for r in res] == [
        "2025-10-26 02:10:00+02:00",
        "2025-10-26 02:50:00+02:00",
        "2025-10-26 02:05:00+01:00",
        "2025-10-26 02:40:00+01:00",
        "2025-10-26 03:10:00+01:00",
    ]
    utcs = [r.utc for r in res]
    assert utcs == sorted(utcs)
    assert utcs[2] == datetime(2025, 10, 26, 1, 5, tzinfo=UTC)
    assert sum(r.dst_ambiguous for r in res) == 4


def test_sanitize_path_matches_bridge():
    # Same table as TestSanitizePath in internal/mediastore/mediastore_test.go.
    cases = {
        "": "_",
        "120363@g.us": "120363@g_us",
        "../../etc": "______etc",
        ".hidden": "_hidden",
        "-flag": "_flag",
        "3EB0ABC_def-1": "3EB0ABC_def-1",
    }
    for raw, want in cases.items():
        assert ci.sanitize_path(raw) == want
    assert ci.media_extension("document", "Invoice 2025.pdf") == ".pdf"
    assert ci.media_extension("audio", "x.opus") == ".ogg"


# --------------------------------------------------------------------------- store side


def test_direct_chat_dedupes_detects_me_and_imports_only_missing(store, tmp_path):
    photo = b"\xff\xd8 not really a jpeg"
    export = ci.read_export(
        _zip(tmp_path, "WhatsApp Chat - Bob.zip", IOS_DIRECT, {"00000012-PHOTO-2025-10-23-09-43-05.jpg": photo})
    )
    assert export.title == "Bob"
    plan = _plan(store, export, BOB)

    assert plan.me_name == "Alice" and "stored own messages" in plan.me_source
    assert plan.mapping["Bob"][0] == BOB
    c = plan.counts
    assert c["matched_exact"] == 3  # Hi Alice, the multi-line Hello, the edited text
    assert c["matched_fuzzy"] == 1  # the location
    assert c["new"] == 2  # the photo and "brand new"
    assert (c["skipped_system"], c["skipped_deleted"], c["skipped_media_omitted"]) == (2, 1, 1)

    assert _apply(store, plan, export) == 2
    conn = sqlite3.connect(store)
    rows = conn.execute(
        "SELECT id, sender, is_from_me, media_type, filename, file_sha256, timestamp FROM messages "
        "WHERE id LIKE 'import-%' ORDER BY timestamp"
    ).fetchall()
    assert len(rows) == 2
    photo_row, text_row = rows
    assert photo_row[1:5] == (BOB, 0, "image", "00000012-PHOTO-2025-10-23-09-43-05.jpg")
    assert photo_row[5] == hashlib.sha256(photo).digest()
    assert text_row[1:3] == (ME, 1) and text_row[6] == "2025-10-23 09:49:30+02:00"
    saved = ci.media_path(os.path.dirname(store), BOB, photo_row[0], "image", photo_row[4])
    assert Path(saved).read_bytes() == photo
    assert saved.endswith(os.path.join("media", "4915112345678@s_whatsapp_net", photo_row[0] + ".jpg"))
    # Old history must not move the chat's activity time backwards.
    assert (
        conn.execute("SELECT last_message_time FROM chats WHERE jid = ?", (BOB,)).fetchone()[0]
        == "2025-10-23 10:00:00+02:00"
    )
    conn.close()

    # Re-running is a no-op: every row is recognised by its ID.
    again = _plan(store, export, BOB)
    assert again.counts["new"] == 0 and again.counts["already_imported"] == 2
    assert _apply(store, again, export) == 0


def test_group_mapping_and_unmapped_senders(store, tmp_path):
    export = ci.read_export(_zip(tmp_path, "g.zip", ANDROID_GROUP, {"IMG-20251023-WA0001.jpg": b"img"}))
    plan = _plan(store, export, GROUP, me="Alice")
    assert plan.mapping["Carol"][0] == CAROL  # matched her stored message under the LID
    assert plan.mapping["Dave"][0] == DAVE  # push name, unique in the directory
    assert plan.mapping["+49 151 12345679"] == ("4915112345679@s.whatsapp.net", "phone number")
    assert plan.unmapped == ["Eve"]
    assert plan.counts["matched_exact"] == 1  # Carol's "Welcome", minute precision
    assert plan.counts["new_from_unmapped"] == 1

    _apply(store, plan, export)
    conn = sqlite3.connect(store)
    eve = conn.execute("SELECT sender, sender_name FROM messages WHERE content = 'who am I'").fetchone()
    assert eve == ("import:Eve", "Eve")
    dave = conn.execute(
        "SELECT sender, content, media_type FROM messages WHERE filename = 'IMG-20251023-WA0001.jpg'"
    ).fetchone()
    assert dave == (DAVE, "nice pic", "image")
    conn.close()


def test_agent_inferred_nickname_is_not_evidence(store, tmp_path):
    conn = sqlite3.connect(store)
    conn.execute("INSERT INTO contact_nicknames (jid, nickname, source) VALUES (?, 'Eve', 'agent_inferred')", (BOB,))
    conn.commit()
    conn.close()
    export = ci.read_export(_zip(tmp_path, "g.zip", ANDROID_GROUP, {}))
    plan = _plan(store, export, GROUP, me="Alice")
    assert "Eve" in plan.unmapped


def test_me_must_be_given_when_nothing_identifies_it(store, tmp_path):
    txt = tmp_path / "WhatsApp Chat with Bob.txt"
    txt.write_text("23/10/2020, 09:41 - Zed: one\n23/10/2020, 09:42 - Yan: two\n", encoding="utf-8")
    with pytest.raises(ci.ChatImportError, match="--me"):
        _plan(store, ci.read_export(txt), BOB)
    plan = _plan(store, ci.read_export(txt), BOB, me="Yan")
    assert plan.counts["new"] == 2 and plan.mapping["Zed"][0] == BOB


def test_chat_jid_validation(store, tmp_path):
    txt = tmp_path / "chat.txt"
    txt.write_text("23/10/2020, 09:41 - Bob: one\n23/10/2020, 09:42 - Alice: two\n", encoding="utf-8")
    export = ci.read_export(txt)
    with pytest.raises(ci.ChatImportError, match="well-formed"):
        _plan(store, export, "not-a-jid")
    new_chat = "4915100000009@s.whatsapp.net"
    plan = _plan(store, export, new_chat, me="Alice")
    assert not plan.chat_exists
    _apply(store, plan, export)
    conn = sqlite3.connect(store)
    assert conn.execute("SELECT name, last_message_time FROM chats WHERE jid = ?", (new_chat,)).fetchone() == (
        "chat",
        "2020-10-23 09:42:00+02:00",
    )
    conn.close()


def test_bridge_write_during_planning_aborts(store, tmp_path):
    export = ci.read_export(_zip(tmp_path, "b.zip", IOS_DIRECT, {}))
    plan = _plan(store, export, BOB)
    conn = sqlite3.connect(store)
    conn.execute(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES "
        "('3EB0LATE', ?, ?, 'brand new', '2025-10-23 09:49:30+02:00', 1)",
        (BOB, ME),
    )
    conn.commit()
    conn.close()
    with pytest.raises(ci.ChatImportError, match="re-run"):
        _apply(store, plan, export)
    assert _plan(store, export, BOB).counts["matched_exact"] == 4


def test_cli_dry_run_writes_nothing_and_apply_backs_up(store, tmp_path, capsys):
    path = _zip(tmp_path, "c.zip", IOS_DIRECT, {})
    before = sqlite3.connect(store).execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert ci.main([str(path), "--chat", BOB, "--db", store]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "new " in out
    assert sqlite3.connect(store).execute("SELECT COUNT(*) FROM messages").fetchone()[0] == before

    backups = tmp_path / "backups"
    assert ci.main([str(path), "--chat", BOB, "--db", store, "--apply", "--backup-dir", str(backups)]) == 0
    assert len(list(backups.glob("messages-preimport-*.db"))) == 1
    assert sqlite3.connect(store).execute("SELECT COUNT(*) FROM messages").fetchone()[0] == before + 2


def test_recall_backfill_picks_up_imported_rows(store, tmp_path, monkeypatch):
    export = ci.read_export(_zip(tmp_path, "r.zip", IOS_DIRECT, {}))
    _apply(store, _plan(store, export, BOB), export)

    monkeypatch.setattr(recall_mod, "MESSAGES_DB_PATH", store)

    def fake_embed(texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), recall_mod.EMBED_DIM), dtype=np.float32)
        out[:, 0] = 1.0
        return out

    monkeypatch.setattr(recall_mod, "_embed", fake_embed)
    recall_mod._index_pending()
    conn = sqlite3.connect(store)
    embedded = {r[0] for r in conn.execute("SELECT message_id FROM message_embeddings")}
    imported_with_text = {
        r[0]
        for r in conn.execute("SELECT id FROM messages WHERE id LIKE 'import-%' AND TRIM(COALESCE(content, '')) <> ''")
    }
    conn.close()
    assert imported_with_text and imported_with_text <= embedded


def test_list_messages_labels_unmapped_import_senders(store, tmp_path, monkeypatch):
    import whatsapp

    export = ci.read_export(_zip(tmp_path, "l.zip", ANDROID_GROUP, {}))
    _apply(store, _plan(store, export, GROUP, me="Alice"), export)
    monkeypatch.setattr(whatsapp, "MESSAGES_DB_PATH", store)
    got = {m["content"]: m for m in whatsapp.list_messages(chat_jid=GROUP, limit=50)}
    eve = got["who am I"]
    assert eve["sender"] == "import:Eve"
    assert (eve["sender_name"], eve["sender_name_source"]) == ("Eve", "chat_export")
    assert eve["id"].startswith("import-")

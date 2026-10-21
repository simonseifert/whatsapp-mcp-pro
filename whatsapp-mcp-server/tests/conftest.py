"""Pytest fixtures for WhatsApp MCP Server tests."""

import os

# Must be set before any test module imports lib.utils — importing lib.utils raises
# FileNotFoundError when messages.db doesn't exist (by design, to prevent silent empty
# results in production). Tests use temp DBs and don't need the real store.
os.environ.setdefault("WA_SKIP_DB_CHECK", "1")

import sqlite3
import tempfile
from datetime import datetime

import pytest

# The LID twin of 123456789@s.whatsapp.net in the fixtures below.
MERGED_LID = "555000111@lid"


@pytest.fixture
def temp_messages_db():
    """Create a temporary messages database with test data."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Create tables
    cursor.execute("""
        CREATE TABLE chats (
            jid TEXT PRIMARY KEY,
            name TEXT,
            last_message_time TEXT,
            merged_into TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            chat_jid TEXT,
            sender TEXT,
            content TEXT,
            timestamp TEXT,
            is_from_me INTEGER,
            media_type TEXT,
            filename TEXT,
            file_length INTEGER,
            sender_name TEXT,
            quoted_message_id TEXT,
            quoted_sender_name TEXT,
            reply_to_message_id TEXT,
            edit_count INTEGER DEFAULT 0,
            is_edited INTEGER DEFAULT 0,
            is_forwarded INTEGER DEFAULT 0,
            forwarded_from TEXT,
            is_system_message INTEGER DEFAULT 0,
            system_message_type TEXT,
            FOREIGN KEY (chat_jid) REFERENCES chats(jid)
        )
    """)

    cursor.execute("""
        CREATE TABLE contact_nicknames (
            jid TEXT PRIMARY KEY,
            nickname TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    # Insert test data
    now = datetime.now().isoformat()
    # A LID chat the bridge has merged into its phone twin: an empty shell
    # stamped with merged_into. Inserted first, so a query without ORDER BY or a
    # merged_into filter meets the shell before the live chat — as on a real
    # store, where the shell was returned for 31 of 50 merged pairs.
    cursor.execute(
        "INSERT INTO chats (jid, name, last_message_time, merged_into) VALUES (?, ?, ?, ?)",
        (MERGED_LID, "Test User (lid)", now, "123456789@s.whatsapp.net"),
    )
    cursor.execute(
        "INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
        ("123456789@s.whatsapp.net", "Test User", now),
    )
    cursor.execute(
        "INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
        ("987654321@g.us", "Test Group", now),
    )

    # Insert test messages
    cursor.execute(
        """INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename, file_length, sender_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "msg1",
            "123456789@s.whatsapp.net",
            "123456789@s.whatsapp.net",
            "Hello world",
            now,
            0,
            None,
            None,
            None,
            "Test User",
        ),
    )
    cursor.execute(
        """INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename, file_length, sender_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("msg2", "123456789@s.whatsapp.net", "me", "Hi there", now, 1, None, None, None, "Me"),
    )

    conn.commit()
    conn.close()

    yield db_path

    # Cleanup
    os.unlink(db_path)


@pytest.fixture
def temp_whatsapp_db():
    """Create a temporary WhatsApp database with test contacts."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE whatsmeow_contacts (
            their_jid TEXT PRIMARY KEY,
            first_name TEXT,
            full_name TEXT,
            push_name TEXT,
            business_name TEXT
        )
    """)

    cursor.execute(
        """INSERT INTO whatsmeow_contacts (their_jid, first_name, full_name, push_name, business_name)
           VALUES (?, ?, ?, ?, ?)""",
        ("123456789@s.whatsapp.net", "John", "John Doe", "Johnny", None),
    )

    # Maps the phone JID to MERGED_LID, so contact lookups reach the shell too.
    cursor.execute("CREATE TABLE whatsmeow_lid_map (lid TEXT PRIMARY KEY, pn TEXT UNIQUE NOT NULL)")
    cursor.execute(
        "INSERT INTO whatsmeow_lid_map (lid, pn) VALUES (?, ?)",
        (MERGED_LID.split("@")[0], "123456789"),
    )

    conn.commit()
    conn.close()

    yield db_path

    os.unlink(db_path)


@pytest.fixture
def mock_bridge_url(monkeypatch):
    """Mock the bridge URL to prevent real API calls."""
    monkeypatch.setenv("BRIDGE_HOST", "localhost:9999")


# --- naming loop store -------------------------------------------------------

NAMING_SCHEMA = """
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
CREATE TABLE messages (
    id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP, is_from_me BOOLEAN,
    media_type TEXT, filename TEXT, file_length INTEGER, is_system_message BOOLEAN DEFAULT 0,
    PRIMARY KEY (id, chat_jid)
);
CREATE TABLE contact_nicknames (
    jid TEXT PRIMARY KEY, nickname TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# (jid, lid, phone_jid, phone, kind, full_name, push_name, nickname, display_name, chat_name)
NAMING_IDENTITIES = [
    # Named through the address book: never a candidate.
    (
        "441234@s.whatsapp.net",
        "999@lid",
        "441234@s.whatsapp.net",
        "441234",
        "user",
        "Jane Doe",
        "jd",
        None,
        "Jane Doe",
        "Jane Doe",
    ),
    # Unknown with a DM and a push name; split across LID and phone JID.
    ("385111@s.whatsapp.net", "111@lid", "385111@s.whatsapp.net", "385111", "user", None, "Ivo", None, "Ivo", "385111"),
    # Unknown seen only as a LID, only in a group, no push name.
    ("222@lid", "222@lid", None, None, "user", None, None, None, "222@lid", None),
    # Unknown with a single old message.
    ("385333@s.whatsapp.net", None, "385333@s.whatsapp.net", "385333", "user", None, None, None, "385333", None),
    # The linked account itself.
    ("385000@s.whatsapp.net", "500@lid", "385000@s.whatsapp.net", "385000", "user", None, "Me", None, "Me", None),
    ("120363@g.us", None, None, None, "group", "Boat Club", None, None, "Boat Club", "Boat Club"),
    ("120999@g.us", None, None, None, "group", None, None, None, "120999@g.us", None),
]

LONG_TEXT = "Reminder about the berth fee " + "x" * 200

# (id, chat_jid, sender, content, timestamp, is_from_me)
NAMING_MESSAGES = [
    ("j1", "441234@s.whatsapp.net", "441234", "Hi from Jane", "2026-10-21 18:00:00+02:00", 0),
    ("a1", "385111@s.whatsapp.net", "385111", "Hi, this is Ivo from the marina", "2026-10-20 09:00:00+02:00", 0),
    ("a2", "385111@s.whatsapp.net", "385000", "Thanks Ivo!", "2026-10-20 09:05:00+02:00", 1),
    ("b1", "120363@g.us", "222", "Morning all", "2026-10-21 08:00:00+02:00", 0),
    ("b2", "120363@g.us", "222@lid", LONG_TEXT, "2026-10-21 09:00:00+02:00", 0),
    ("b3", "120363@g.us", "222", "", "2026-10-21 09:30:00+02:00", 0),
    ("c1", "385333@s.whatsapp.net", "385333", "Old hello", "2025-01-01 10:00:00+01:00", 0),
    ("s1", "385000@s.whatsapp.net", "385000", "note to self", "2026-10-21 20:00:00+02:00", 1),
    ("g1", "120999@g.us", "441234", "Unnamed group ping", "2026-10-19 10:00:00+02:00", 0),
]


@pytest.fixture
def naming_store(monkeypatch, tmp_path):
    """A store with the identity directory, chats, messages and nicknames for the naming loop.

    Every module that reads the store is pointed at it, so the tools and the
    display-name overlay are exercised end to end.
    """
    messages_db = str(tmp_path / "messages.db")
    whatsapp_db = str(tmp_path / "whatsapp.db")

    conn = sqlite3.connect(messages_db)
    conn.executescript(NAMING_SCHEMA)
    for row in NAMING_IDENTITIES:
        conn.execute(
            "INSERT INTO identities (jid, lid, phone_jid, phone, kind, full_name, push_name, nickname,"
            " display_name, chat_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    chats = {
        "441234@s.whatsapp.net": "Jane Doe",
        "385111@s.whatsapp.net": "385111",
        "385333@s.whatsapp.net": None,
        "385000@s.whatsapp.net": None,
        "120363@g.us": "Boat Club",
        "120999@g.us": None,
    }
    for jid, name in chats.items():
        conn.execute("INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)", (jid, name, "2026-10-21"))
    conn.execute(
        "INSERT INTO chats (jid, name, merged_into) VALUES (?, ?, ?)", ("111@lid", "Ivo", "385111@s.whatsapp.net")
    )
    conn.executemany(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, ?, ?, ?, ?)",
        NAMING_MESSAGES,
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(whatsapp_db)
    conn.execute("CREATE TABLE whatsmeow_device (jid TEXT PRIMARY KEY, lid TEXT)")
    conn.execute("INSERT INTO whatsmeow_device VALUES (?, ?)", ("385000:12@s.whatsapp.net", "500:12@lid"))
    conn.execute("CREATE TABLE whatsmeow_lid_map (lid TEXT PRIMARY KEY, pn TEXT UNIQUE NOT NULL)")
    conn.execute(
        "CREATE TABLE whatsmeow_contacts (their_jid TEXT PRIMARY KEY, first_name TEXT, full_name TEXT,"
        " push_name TEXT, business_name TEXT)"
    )
    conn.commit()
    conn.close()

    import lib.identity as identity
    import lib.naming as naming
    import lib.utils as utils
    import whatsapp

    for mod in (whatsapp, identity, naming, utils):
        monkeypatch.setattr(mod, "MESSAGES_DB_PATH", messages_db, raising=False)
        monkeypatch.setattr(mod, "WHATSAPP_DB_PATH", whatsapp_db, raising=False)
    return messages_db

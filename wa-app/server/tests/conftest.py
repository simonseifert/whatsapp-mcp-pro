"""Shared fixtures: a tiny fictional bridge store and an app client."""

import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

# wa_app.mcp_lib imports the bridge helpers, which resolve a store path at import time. Point it at nothing
# real: the app always passes its own connections, and no test may touch a live store.
os.environ["WA_STORE_PATH"] = "/nonexistent/wa-app-tests/store"

from wa_app.config import Settings
from wa_app.main import create_app

PASSPHRASE = "correct horse battery"

# Real CREATE TABLE statements, copied from whatsapp-bridge/internal/database/{store,identities,chat_state}.go and
# whatsapp-bridge/migrations/{009,011,012,013}. Keep in step with them.
_SCHEMA = """
CREATE TABLE chats (
    jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP, merged_into TEXT
);
CREATE TABLE messages (
    id TEXT, chat_jid TEXT, sender TEXT, sender_name TEXT, content TEXT, timestamp TIMESTAMP,
    is_from_me BOOLEAN, media_type TEXT, filename TEXT, url TEXT, media_key BLOB, file_sha256 BLOB,
    file_enc_sha256 BLOB, file_length INTEGER, direct_path TEXT,
    quoted_message_id TEXT, quoted_sender_name TEXT, quoted_text_preview TEXT, reply_to_message_id TEXT,
    edit_count INTEGER DEFAULT 0, is_edited BOOLEAN DEFAULT 0, is_forwarded BOOLEAN DEFAULT 0, forwarded_from TEXT,
    is_system_message BOOLEAN DEFAULT 0, system_message_type TEXT,
    edited_at TIMESTAMP, deleted_at TIMESTAMP, deleted_by TEXT,
    PRIMARY KEY (id, chat_jid),
    FOREIGN KEY (chat_jid) REFERENCES chats(jid)
);
CREATE INDEX idx_messages_chat_timestamp ON messages(chat_jid, timestamp DESC);
CREATE TABLE contact_nicknames (
    jid TEXT PRIMARY KEY, nickname TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE message_reactions (
    chat_jid TEXT NOT NULL, message_id TEXT NOT NULL, sender TEXT NOT NULL, emoji TEXT NOT NULL,
    reacted_at TIMESTAMP NOT NULL, PRIMARY KEY (chat_jid, message_id, sender)
);
CREATE TABLE chat_state (
    chat_jid TEXT PRIMARY KEY,
    pinned INTEGER NOT NULL DEFAULT 0, pinned_changed_at TIMESTAMP,
    muted_until INTEGER NOT NULL DEFAULT 0, muted_until_changed_at TIMESTAMP,
    archived INTEGER NOT NULL DEFAULT 0, archived_changed_at TIMESTAMP,
    marked_unread INTEGER NOT NULL DEFAULT 0, marked_unread_changed_at TIMESTAMP,
    last_read_at TIMESTAMP, history_unread_count INTEGER
);
CREATE TABLE identities (
    jid TEXT PRIMARY KEY, lid TEXT, phone_jid TEXT, phone TEXT, redacted_phone TEXT,
    kind TEXT NOT NULL DEFAULT 'user', first_name TEXT, full_name TEXT, push_name TEXT, business_name TEXT,
    nickname TEXT, display_name TEXT, chat_name TEXT,
    is_business BOOLEAN DEFAULT 0, is_contact BOOLEAN DEFAULT 0, has_chat BOOLEAN DEFAULT 0,
    message_count INTEGER DEFAULT 0, participant_count INTEGER, is_announce BOOLEAN DEFAULT 0, owner_jid TEXT,
    first_seen TIMESTAMP, last_seen TIMESTAMP, sources TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_community INTEGER NOT NULL DEFAULT 0, community_jid TEXT,
    is_default_subgroup INTEGER NOT NULL DEFAULT 0, i_am_admin INTEGER NOT NULL DEFAULT 0
);
"""

# Fictional JIDs.
ANA = "385910000001@s.whatsapp.net"
FAMILY = "120363000000000002@g.us"
ACCOUNTANT = "385910000003@s.whatsapp.net"
NEIGHBOURHOOD = "120363000000000004@g.us"
COMMUNITY = "120363000000000099@g.us"
LUKAS = "4915100000005@s.whatsapp.net"
NIKOLA = "385910000006@s.whatsapp.net"
STUDY = "120363000000000007@g.us"
UNKNOWN = "385910000008@s.whatsapp.net"
IVANA = "385910000009@s.whatsapp.net"
IVANA_LID_SHELL = "333@lid"  # merged into IVANA
GYM = "385910000010@s.whatsapp.net"
TOMO_LID = "111@lid"
TOMO_PHONE = "4915112345678@s.whatsapp.net"
CHANNEL = "120363000000000011@newsletter"
OLD_ARCHIVED = "385910000012@s.whatsapp.net"
OLD_ARCHIVED_GROUP = "120363000000000013@g.us"
ACCOUNTANT_NAME = "Đurđica Šimunović-Petrović (Računovodstvo d.o.o.)"
VOICE_SECONDS = 42


def fake_ogg_opus(seconds: int, pre_skip: int = 312) -> bytes:
    """A minimal Ogg/Opus stream (head page + final page) that reports `seconds` of audio."""

    def page(header_type: int, granule: int, seq: int, payload: bytes) -> bytes:
        head = b"OggS" + bytes([0, header_type]) + granule.to_bytes(8, "little") + (1).to_bytes(4, "little")
        return head + seq.to_bytes(4, "little") + b"\0\0\0\0" + bytes([1, len(payload)]) + payload

    opus_head = b"OpusHead" + bytes([1, 1]) + pre_skip.to_bytes(2, "little") + (48000).to_bytes(4, "little") + b"\0\0\0"
    return page(2, 0, 0, opus_head) + page(4, seconds * 48000 + pre_skip, 1, b"xxxx")


def make_store(tmp_path: Path) -> Path:
    """Create a fictional bridge store dir (real schema, fictional chats); return the dir."""
    store = tmp_path / "store"
    store.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(store / "messages.db")
    conn.executescript(_SCHEMA)
    _fill(conn, store)
    conn.commit()
    conn.close()
    return store


def _fill(conn: sqlite3.Connection, store: Path) -> None:
    now = datetime.now().astimezone().replace(microsecond=0)

    def ts(minutes_ago: float) -> str:
        return (now - timedelta(minutes=minutes_ago)).isoformat(sep=" ")

    def chat(jid: str, name: str, minutes_ago: float | None = None, merged_into: str | None = None) -> None:
        conn.execute(
            "INSERT INTO chats (jid, name, last_message_time, merged_into) VALUES (?,?,?,?)",
            (jid, name, ts(minutes_ago) if minutes_ago is not None else None, merged_into),
        )

    counter = iter(range(1, 10_000))

    def msg(chat_jid: str, minutes_ago: float, content: str | None = "", **kw: object) -> str:
        mid = f"M{next(counter):04d}"
        conn.execute(
            "INSERT INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, media_type,"
            " filename, is_system_message, deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                mid, chat_jid, kw.get("sender"), kw.get("sender_name"), content, ts(minutes_ago),
                1 if kw.get("from_me") else 0, kw.get("media_type"), kw.get("filename"),
                1 if kw.get("system") else 0, ts(minutes_ago - 1) if kw.get("deleted") else None,
            ),
        )  # fmt: skip
        return mid

    def state(jid: str, **kw: object) -> None:
        cols = ["chat_jid", *kw]
        conn.execute(
            f"INSERT INTO chat_state ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", (jid, *kw.values())
        )

    def identity(jid: str, **kw: object) -> None:
        cols = ["jid", *kw]
        conn.execute(
            f"INSERT INTO identities ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", (jid, *kw.values())
        )

    # Ana: pinned, two incoming messages after she was last read.
    chat(ANA, "Ana Kovačević", 16)
    identity(ANA, phone="385910000001", phone_jid=ANA, full_name="Ana Kovačević", display_name="Ana Kovačević")
    msg(ANA, 300, "Vidimo se sutra?", sender=ANA)
    msg(ANA, 200, "Može, kad?", from_me=True)
    msg(ANA, 30, "Ja sam slobodna u 7", sender=ANA)
    msg(ANA, 16, "Može, vidimo se u 7 kod Molo Longo 👍", sender=ANA)
    state(ANA, pinned=1, last_read_at=ts(100))

    # Family group: pinned, muted for a day, voice note from Mama, history says 5 unread.
    chat(FAMILY, "Obitelj 🏡", 12)
    identity(FAMILY, kind="group", display_name="Obitelj 🏡", chat_name="Obitelj 🏡")
    msg(FAMILY, 60, "Tko donosi kruh?", sender="385910000020@s.whatsapp.net", sender_name="Tata")
    msg(FAMILY, 12, "", sender="385910000021@s.whatsapp.net", sender_name="Mama", media_type="audio",
        filename="audio_20261122_204800.ogg")  # fmt: skip
    state(FAMILY, pinned=1, muted_until=int(now.timestamp()) + 86400, history_unread_count=5)
    voice_dir = store / FAMILY
    voice_dir.mkdir()
    (voice_dir / "audio_20261122_204800.ogg").write_bytes(fake_ogg_opus(VOICE_SECONDS))

    # Accountant: very long name, unread 0 (read after the last message).
    chat(ACCOUNTANT, ACCOUNTANT_NAME, 120)
    identity(ACCOUNTANT, phone="385910000003", phone_jid=ACCOUNTANT, full_name=ACCOUNTANT_NAME,
             display_name=ACCOUNTANT_NAME)  # fmt: skip
    msg(ACCOUNTANT, 120, "Šaljem vam račun za listopad, molim potvrdu primitka.", sender=ACCOUNTANT)
    state(ACCOUNTANT, last_read_at=ts(60))

    # Community announcement group.
    chat(COMMUNITY, "Kvart Trsat")
    identity(COMMUNITY, kind="group", display_name="Kvart Trsat", is_community=1)
    chat(NEIGHBOURHOOD, "Kvart Trsat · Obavijesti", 300)
    identity(NEIGHBOURHOOD, kind="group", display_name="Kvart Trsat · Obavijesti", is_announce=1,
             community_jid=COMMUNITY, is_default_subgroup=1)  # fmt: skip
    msg(NEIGHBOURHOOD, 300, "Sutra od 8 do 12 nema vode u ulicama Strossmayerova i Slavka Krautzeka.",
        sender="385910000022@s.whatsapp.net", sender_name="Admin")  # fmt: skip
    state(NEIGHBOURHOOD, history_unread_count=1)

    # Lukas: last message is mine.
    chat(LUKAS, "Lukas Brenner", 400)
    identity(LUKAS, phone="4915100000005", phone_jid=LUKAS, full_name="Lukas Brenner", display_name="Lukas Brenner")
    msg(LUKAS, 400, "Danke! Ich schick dir die Unterlagen morgen früh.", from_me=True)

    # Nikola: photo, marked unread on the phone.
    chat(NIKOLA, "Nikola", 1500)
    identity(NIKOLA, phone="385910000006", phone_jid=NIKOLA, full_name="Nikola", display_name="Nikola")
    msg(NIKOLA, 1500, "", sender=NIKOLA, media_type="image", filename="image_1.jpg")
    state(NIKOLA, marked_unread=1)

    # Study group: poll, history says 12 unread.
    chat(STUDY, "Studij FIDIT 2. god.", 1600)
    identity(STUDY, kind="group", display_name="Studij FIDIT 2. god.")
    msg(STUDY, 1600, "[Poll: Kad idemo na kolokvij pripreme?]", sender="385910000023@s.whatsapp.net",
        sender_name="Petra")  # fmt: skip
    state(STUDY, history_unread_count=12)

    # Unknown number, deleted message.
    chat(UNKNOWN, "385910000008", 3000)
    identity(UNKNOWN, phone="385910000008", phone_jid=UNKNOWN, display_name="385910000008")
    msg(UNKNOWN, 3000, "secret text", sender=UNKNOWN, deleted=True)

    # Ivana: one chat under her phone JID, an empty shell under her LID merged into it.
    chat(IVANA, "Ivana", 3100)
    chat(IVANA_LID_SHELL, "Ivana", None, merged_into=IVANA)
    identity(IVANA, lid=IVANA_LID_SHELL, phone="385910000009", phone_jid=IVANA, full_name="Ivana", display_name="Ivana")
    msg(
        IVANA,
        3100,
        "Haha ne mogu vjerovat 😂😂 jesi vidio što je napisao u grupi?? ja sam mislila da se šali",
        sender=IVANA,
    )

    # Gym: sticker.
    chat(GYM, "Teretana Kantrida", 8000)
    identity(GYM, phone="385910000010", phone_jid=GYM, full_name="Teretana Kantrida", display_name="Teretana Kantrida")
    msg(GYM, 8000, "", sender=GYM, media_type="sticker", filename="sticker_1.webp")

    # One person split across his LID and phone JID, named by a nickname; the newer message sits under the LID.
    chat(TOMO_LID, "111", 5)
    chat(TOMO_PHONE, "4915112345678", 40)
    identity(
        TOMO_PHONE, lid=TOMO_LID, phone_jid=TOMO_PHONE, phone="4915112345678", push_name="tomi", display_name="tomi"
    )
    conn.execute("INSERT INTO contact_nicknames (jid, nickname) VALUES (?, ?)", (TOMO_PHONE, "Tomo (posao)"))
    msg(TOMO_PHONE, 40, "Pozdrav iz Njemačke", sender=TOMO_PHONE)
    msg(TOMO_LID, 5, "Stigao sam", sender=TOMO_LID)

    # A newsletter.
    chat(CHANNEL, "Lučka uprava · vijesti", 9000)
    identity(CHANNEL, kind="newsletter", display_name="Lučka uprava · vijesti")
    msg(CHANNEL, 9000, "Nova linija u petak.", sender=CHANNEL)

    # Two archived chats.
    chat(OLD_ARCHIVED, "Stara Kolegica", 20000)
    identity(OLD_ARCHIVED, phone="385910000012", phone_jid=OLD_ARCHIVED, full_name="Stara Kolegica",
             display_name="Stara Kolegica")  # fmt: skip
    msg(OLD_ARCHIVED, 20000, "Čujemo se", sender=OLD_ARCHIVED)
    state(OLD_ARCHIVED, archived=1)
    chat(OLD_ARCHIVED_GROUP, "Stari tim", 21000)
    identity(OLD_ARCHIVED_GROUP, kind="group", display_name="Stari tim")
    msg(OLD_ARCHIVED_GROUP, 21000, "Hvala svima", sender="385910000024@s.whatsapp.net", sender_name="Marin")
    state(OLD_ARCHIVED_GROUP, archived=1)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=18085,
        data_dir=tmp_path / "data",
        store_path=make_store(tmp_path),
        secure_cookies=True,
    )


@pytest.fixture
def client(settings: Settings):
    # https base so the Secure cookie is stored and replayed by the test client.
    with TestClient(create_app(settings), base_url="https://testserver") as c:
        yield c


@pytest.fixture
def logged_in(client: TestClient) -> TestClient:
    client.post("/api/auth/setup", json={"passphrase": PASSPHRASE})
    assert client.post("/api/auth/login", json={"passphrase": PASSPHRASE}).status_code == 200
    return client

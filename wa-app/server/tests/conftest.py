"""Shared fixtures: a tiny fictional bridge store and an app client."""

import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from wa_app.config import Settings
from wa_app.main import create_app

PASSPHRASE = "correct horse battery"

_SCHEMA = """
CREATE TABLE chats (
    jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP, merged_into TEXT
);
CREATE TABLE messages (
    id TEXT, chat_jid TEXT, sender TEXT, sender_name TEXT, content TEXT,
    timestamp TIMESTAMP, is_from_me BOOLEAN, media_type TEXT, filename TEXT,
    quoted_message_id TEXT, quoted_sender_name TEXT, quoted_text_preview TEXT,
    is_edited BOOLEAN DEFAULT 0, is_forwarded BOOLEAN DEFAULT 0,
    is_system_message BOOLEAN DEFAULT 0,
    PRIMARY KEY (id, chat_jid)
);
"""


def make_store(tmp_path: Path) -> Path:
    """Create a fictional bridge store dir holding an empty-schema messages.db; return the dir."""
    store = tmp_path / "store"
    store.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(store / "messages.db")
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return store


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

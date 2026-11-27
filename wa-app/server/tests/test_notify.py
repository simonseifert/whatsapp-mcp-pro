import json
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from wa_app import appdb, notify, push, store
from wa_app.config import Settings
from wa_app.live import Event

NOW = 1_800_000_000.0
PRIVATE = "385911110001@s.whatsapp.net"
GROUP = "120363000000000001@g.us"


class FakeHub:
    def __init__(self) -> None:
        self.open: set[str] = set()

    def chat_open(self, chat_jid: str) -> bool:
        return chat_jid in self.open


def chat(
    jid: str = PRIVATE, name: str = "Ana Kovačić", kind: str = "private", muted: bool = False
) -> store.ChatSummary:
    return store.ChatSummary(
        jid=jid,
        name=name,
        kind=kind,  # type: ignore[arg-type]
        avatar_url="",
        last_message=store.LastMessage("m", None, False, "", None, False, ""),
        unread=1,
        marked_unread=False,
        pinned=False,
        muted=muted,
        archived=False,
        community_name=None,
    )


def message(mid: str = "m1", *, age: float = 5, **over: object) -> dict:
    stamp = datetime.fromtimestamp(NOW - age, UTC).isoformat()
    base = {
        "id": mid,
        "from_me": False,
        "system": False,
        "deleted": False,
        "timestamp_iso": stamp,
        "sender_name": "Ana Kovačić",
        "text": "Stižem za 5 min",
        "media": None,
    }
    base.update(over)
    return base


def event(msg: dict, jid: str = PRIVATE) -> Event:
    return Event("message", {"chat_jid": jid, "message": msg})


@pytest.fixture
def rig(settings: Settings):
    chats = {PRIVATE: chat(), GROUP: chat(GROUP, "Obitelj", "group")}
    sent: list[dict] = []
    hub = FakeHub()
    appconn = appdb.connect(settings)
    notifier = notify.Notifier(settings, appconn, hub, now=lambda: NOW)

    def fake_get_chat(ro: sqlite3.Connection, app: sqlite3.Connection, jid: str) -> store.ChatSummary | None:
        return chats.get(jid)

    def fake_send(s: Settings, a: sqlite3.Connection, payload: dict) -> int:
        sent.append(payload)
        return 1

    with patch.object(store, "get_chat", fake_get_chat), patch.object(push, "send", fake_send):
        yield notifier, sent, hub, chats
    notifier.close()


def test_notifies_fresh_incoming_message(rig) -> None:
    notifier, sent, _, _ = rig
    notifier.handle([event(message())])
    assert sent == [
        {
            "chat_jid": PRIVATE,
            "title": "Ana Kovačić",
            "body": "Stižem za 5 min",
            "tag": PRIVATE,
            "url": "/chat/385911110001%40s.whatsapp.net",
        }
    ]


def test_group_body_has_sender(rig) -> None:
    notifier, sent, _, _ = rig
    notifier.handle([event(message(sender_name="Marko", text="Ručak u 14"), GROUP)])
    assert sent[0]["title"] == "Obitelj" and sent[0]["body"] == "Marko: Ručak u 14"


def test_skips_own_system_deleted(rig) -> None:
    notifier, sent, _, _ = rig
    notifier.handle(
        [
            event(message("a", from_me=True)),
            event(message("b", system=True)),
            event(message("c", deleted=True, text=None)),
        ]
    )
    assert sent == []


def test_skips_muted_chat(rig) -> None:
    notifier, sent, _, chats = rig
    chats[PRIVATE] = chat(muted=True)
    notifier.handle([event(message())])
    assert sent == []


def test_skips_newsletter_channels(rig) -> None:
    notifier, sent, _, chats = rig
    jid = "120363000000000099@newsletter"
    chats[jid] = chat(jid, "Vijesti", "newsletter")
    notifier.handle([event(message(), jid=jid)])
    assert sent == []


def test_skips_old_message(rig) -> None:
    notifier, sent, _, _ = rig
    notifier.handle([event(message("old", age=121))])
    assert sent == []
    notifier.handle([event(message("new", age=119))])
    assert len(sent) == 1


def test_same_message_twice_notifies_once(rig) -> None:
    notifier, sent, _, _ = rig
    notifier.handle([event(message())])
    notifier.handle([event(message())])  # INSERT OR REPLACE re-insert: a new rowid, the same (chat, id)
    assert len(sent) == 1


def test_seen_keys_are_bounded(rig) -> None:
    notifier, sent, _, _ = rig
    notifier.handle([event(message(f"m{i}")) for i in range(notify.SEEN_KEYS + 5)])
    assert len(notifier._seen) == notify.SEEN_KEYS


def test_skips_when_chat_open_in_visible_client(rig) -> None:
    notifier, sent, hub, _ = rig
    hub.open.add(PRIVATE)
    notifier.handle([event(message())])
    assert sent == []


def test_notifies_when_open_chat_is_hidden(rig) -> None:
    notifier, sent, hub, _ = rig
    notifier.handle([event(message("m1"))])
    hub.open.add(PRIVATE)
    notifier.handle([event(message("m2"))])
    hub.open.clear()  # hidden, or another chat opened
    notifier.handle([event(message("m3"))])
    assert [p["body"] for p in sent] == ["Stižem za 5 min"] * 2


def test_media_bodies(rig) -> None:
    notifier, sent, _, _ = rig
    photo = {"type": "image", "filename": None, "duration_s": None, "has_file": True}
    doc = {"type": "document", "filename": "Račun_studeni.pdf", "duration_s": None, "has_file": True}
    notifier.handle(
        [
            event(message("1", text=None, media=photo)),
            event(message("2", text=None, media=doc)),
            event(message("3", text="Pogledaj", media=photo)),
            event(message("4", text=None, media={**photo, "type": "video"})),
            event(message("5", text=None, media={**photo, "type": "ptt"})),
            event(message("6", text=None, media={**photo, "type": "audio"})),
            event(message("7", text=None, media={**photo, "type": "sticker"})),
            event(message("8", text=None, media={**doc, "filename": None})),
        ]
    )
    assert [p["body"] for p in sent] == [
        "📷 Photo",
        "📄 Račun_studeni.pdf",
        "📷 Pogledaj",
        "🎥 Video",
        "🎤 Voice message",
        "🎤 Voice message",
        "Sticker",
        "📄 Document",
    ]


def test_no_push_for_call_log_and_unknown_media(rig) -> None:
    notifier, sent, _, _ = rig
    call = {"type": "call", "filename": None, "duration_s": None, "has_file": False}
    notifier.handle(
        [
            event(message("1", text="Missed call", media=call)),
            event(message("2", text=None, media={**call, "type": "mystery"})),
            event(message("3", text="x", media={**call, "type": "mystery"}), GROUP),
        ]
    )
    assert sent == []


def test_poll_and_contact_bodies(rig) -> None:
    notifier, sent, _, _ = rig
    notifier.handle(
        [
            event(message("1", text=None, poll={"question": "Gdje za ručak?", "options": [], "total": 0})),
            event(message("2", text=None, poll={"question": "", "options": [], "total": 0})),
            event(message("3", text=None, contact_cards=[{"name": "Ana", "phones": ["+385"]}])),
            event(message("4", sender_name="Marko", text=None, contact_cards=[{"name": "Ana", "phones": []}]), GROUP),
        ]
    )
    assert [p["body"] for p in sent] == ["📊 Gdje za ručak?", "📊 Poll", "👤 Contact", "Marko: 👤 Contact"]


def test_never_pushes_an_empty_body(rig) -> None:
    notifier, sent, _, _ = rig
    notifier.handle(
        [
            event(message("1", text=None)),
            event(message("2", text="   ")),
            event(message("3", sender_name="Marko", text=None), GROUP),
            event(message("4", sender_name="Marko", text=""), GROUP),
        ]
    )
    assert sent == []


def test_long_text_truncated_to_180_chars(rig) -> None:
    notifier, sent, _, _ = rig
    notifier.handle([event(message(text="č" * 500))])
    assert len(sent[0]["body"]) == 180 and sent[0]["body"].endswith("…")


def test_payload_stays_under_3_kb_and_title_is_capped(rig) -> None:
    notifier, sent, _, chats = rig
    chats[PRIVATE] = chat(name="Š" * 200)
    notifier.handle([event(message(text="😀" * 500))])
    payload = sent[0]
    assert len(payload["title"]) == 80 and payload["title"].endswith("…")
    assert len(json.dumps(payload).encode()) <= 3 * 1024
    assert payload["body"].endswith("…")


def test_resync_event_notifies_nothing(rig) -> None:
    notifier, sent, _, _ = rig
    notifier.handle([Event("resync", {})])
    assert sent == []


def test_unknown_chat_notifies_nothing(rig) -> None:
    notifier, sent, _, _ = rig
    notifier.handle([event(message(), "385999999999@s.whatsapp.net")])
    assert sent == []


def test_on_events_runs_on_the_worker_thread(rig) -> None:
    notifier, sent, _, _ = rig
    notifier.on_events([event(message())])
    notifier._executor.shutdown(wait=True)
    assert len(sent) == 1


def test_a_failing_send_does_not_stop_the_next(rig) -> None:
    notifier, sent, _, _ = rig
    with patch.object(push, "send", side_effect=RuntimeError("boom")):
        notifier._safe([event(message("a"))])
    notifier._safe([event(message("b"))])
    assert len(sent) == 1


def test_timezone_offset_stamp(rig) -> None:
    notifier, sent, _, _ = rig
    stamp = (datetime.fromtimestamp(NOW - 10, UTC) + timedelta(0)).astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    notifier.handle([event(message(timestamp_iso=stamp))])
    assert len(sent) == 1

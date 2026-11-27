"""Captions and quotes on sends: payloads handed to the bridge's /api/send."""

import pytest

import whatsapp


@pytest.fixture
def posted(monkeypatch, tmp_path):
    seen: dict[str, object] = {}
    monkeypatch.setattr(whatsapp, "MEDIA_TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(whatsapp, "_get_headers", lambda: {})

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"success": True, "message_id": "MSG1", "recipient": "synthetic-recipient"}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(whatsapp.requests, "post", fake_post)
    return seen


def test_send_file_sends_caption_and_quote(posted, tmp_path):
    f = tmp_path / "pic.png"
    f.write_bytes(b"png")
    result = whatsapp.send_file(
        "synthetic-recipient",
        media_path=str(f),
        caption="Evo slike",
        quoted_message_id="QID1",
        quoted_chat_jid="120363000000000001@g.us",
    )
    assert result["success"] is True
    assert posted["payload"]["message"] == "Evo slike"
    assert posted["payload"]["quoted_message_id"] == "QID1"
    assert posted["payload"]["quoted_chat_jid"] == "120363000000000001@g.us"


def test_send_file_without_caption_has_no_message_key(posted, tmp_path):
    f = tmp_path / "pic.png"
    f.write_bytes(b"png")
    whatsapp.send_file("synthetic-recipient", media_path=str(f))
    assert not {"message", "quoted_message_id", "quoted_chat_jid"} & set(posted["payload"])


def test_send_message_passes_quoted_chat_jid(posted):
    whatsapp.send_message(
        "4915112345678@s.whatsapp.net", "hi", quoted_message_id="QID1", quoted_chat_jid="120363000000000001@g.us"
    )
    assert posted["payload"]["quoted_message_id"] == "QID1"
    assert posted["payload"]["quoted_chat_jid"] == "120363000000000001@g.us"


def test_send_message_without_quoted_chat_omits_it(posted):
    whatsapp.send_message("4915112345678@s.whatsapp.net", "hi", quoted_message_id="QID1")
    assert "quoted_chat_jid" not in posted["payload"]


def test_send_audio_passes_quote(posted, tmp_path):
    f = tmp_path / "note.ogg"
    f.write_bytes(b"ogg")
    whatsapp.send_audio_message(
        "synthetic-recipient", str(f), quoted_message_id="QID1", quoted_chat_jid="120363000000000001@g.us"
    )
    assert posted["payload"]["quoted_message_id"] == "QID1"
    assert posted["payload"]["quoted_chat_jid"] == "120363000000000001@g.us"

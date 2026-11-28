"""Star, mark unread and forwarded sends: payloads handed to the bridge."""

import pytest

import whatsapp


@pytest.fixture
def posted(monkeypatch, tmp_path):
    seen: dict[str, object] = {}
    monkeypatch.setattr(whatsapp, "MEDIA_TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(whatsapp, "_get_headers", lambda: {})

    class FakeResponse:
        status_code = 200
        text = "ok"

        @staticmethod
        def json():
            return {"success": True, "message_id": "MSG1", "recipient": "synthetic-recipient"}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["url"] = url
        seen["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(whatsapp.requests, "post", fake_post)
    return seen


def test_star_message_posts_expected_json(posted):
    whatsapp.star_message("120363000000000001@g.us", "M1", sender_jid="100000000000001@lid")
    assert posted["url"].endswith("/star")
    assert posted["payload"] == {
        "chat_jid": "120363000000000001@g.us",
        "message_id": "M1",
        "star": True,
        "from_me": False,
        "sender_jid": "100000000000001@lid",
    }


def test_unstar_own_message_omits_sender(posted):
    whatsapp.star_message("4915112345678@s.whatsapp.net", "M1", star=False, from_me=True)
    assert posted["payload"] == {
        "chat_jid": "4915112345678@s.whatsapp.net",
        "message_id": "M1",
        "star": False,
        "from_me": True,
    }


def test_star_message_reports_http_error(monkeypatch):
    class Bad:
        status_code = 500
        text = "boom"

    monkeypatch.setattr(whatsapp, "_get_headers", lambda: {})
    monkeypatch.setattr(whatsapp.requests, "post", lambda *a, **k: Bad())
    result = whatsapp.star_message("1@s.whatsapp.net", "M1")
    assert result["success"] is False and "HTTP 500" in result["error"]


def test_mark_chat_unread_posts_expected_json(posted):
    whatsapp.mark_chat_unread("4915112345678@s.whatsapp.net")
    assert posted["url"].endswith("/mark-unread")
    assert posted["payload"] == {"chat_jid": "4915112345678@s.whatsapp.net", "unread": True}
    whatsapp.mark_chat_unread("4915112345678@s.whatsapp.net", unread=False)
    assert posted["payload"]["unread"] is False


def test_send_message_forwarded_only_sent_when_true(posted):
    whatsapp.send_message("4915112345678@s.whatsapp.net", "hi")
    assert "forwarded" not in posted["payload"]
    whatsapp.send_message("4915112345678@s.whatsapp.net", "hi", forwarded=True)
    assert posted["payload"]["forwarded"] is True


def test_send_file_forwarded_only_sent_when_true(posted, tmp_path):
    f = tmp_path / "pic.png"
    f.write_bytes(b"png")
    whatsapp.send_file("synthetic-recipient", media_path=str(f))
    assert "forwarded" not in posted["payload"]
    whatsapp.send_file("synthetic-recipient", media_path=str(f), forwarded=True)
    assert posted["payload"]["forwarded"] is True

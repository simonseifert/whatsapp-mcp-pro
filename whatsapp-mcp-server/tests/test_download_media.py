"""download_media passes no_phone_retry to the bridge only when asked, and stays backward compatible."""

import pytest

import whatsapp


@pytest.fixture
def posted(monkeypatch):
    payloads: list[dict] = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"success": True, "path": "/tmp/store/media/x/M1.jpg"}

    def fake_post(url, json=None, headers=None, timeout=None):
        payloads.append(json)
        return FakeResponse()

    monkeypatch.setattr(whatsapp.requests, "post", fake_post)
    return payloads


def test_default_payload_has_no_retry_flag(posted):
    assert whatsapp.download_media("M1", "123@s.whatsapp.net") == "/tmp/store/media/x/M1.jpg"
    assert posted == [{"message_id": "M1", "chat_jid": "123@s.whatsapp.net"}]


def test_no_phone_retry_is_forwarded(posted):
    whatsapp.download_media("M1", "123@s.whatsapp.net", no_phone_retry=True)
    assert posted[0]["no_phone_retry"] is True

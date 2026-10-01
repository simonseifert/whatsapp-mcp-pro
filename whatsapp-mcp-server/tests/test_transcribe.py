"""Backend selection and fallback for lib.transcribe."""

from typing import Any

import pytest
import requests

from lib import transcribe


class _Resp:
    def __init__(self, status: int, body: dict[str, Any]):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self) -> dict[str, Any]:
        return self._body


@pytest.fixture
def audio(tmp_path):
    f = tmp_path / "note.ogg"
    f.write_bytes(b"OggS fake")
    return str(f)


def test_auto_prefers_whisper_server_when_url_set(monkeypatch):
    monkeypatch.setenv("WHISPER_BACKEND", "auto")
    monkeypatch.setattr(transcribe, "WHISPER_SERVER_URL", "http://127.0.0.1:8093")
    assert transcribe.available_backend() == "whisper-server"


def test_whisper_server_success(monkeypatch, audio):
    monkeypatch.setenv("WHISPER_BACKEND", "whisper-server")
    monkeypatch.setattr(transcribe, "WHISPER_SERVER_URL", "http://127.0.0.1:8093")
    seen = {}

    def fake_post(url, files, data, timeout):
        seen["url"], seen["data"] = url, data
        return _Resp(200, {"text": " Bok, stižem za deset minuta. ", "detected_language": "croatian"})

    monkeypatch.setattr(requests, "post", fake_post)
    result = transcribe.transcribe_file(audio)

    assert result == {
        "success": True,
        "backend": "whisper-server",
        "text": "Bok, stižem za deset minuta.",
        "language": "croatian",
    }
    assert seen["url"] == "http://127.0.0.1:8093/inference"
    assert seen["data"]["language"] == "auto"


def test_local_failure_falls_back_to_groq(monkeypatch, audio):
    monkeypatch.setenv("WHISPER_BACKEND", "whisper-server")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setattr(transcribe, "WHISPER_SERVER_URL", "http://127.0.0.1:8093")

    def fake_post(url, **kwargs):
        if "8093" in url:
            raise requests.ConnectionError("server down")
        return _Resp(200, {"text": "from groq", "language": "hr"})

    monkeypatch.setattr(requests, "post", fake_post)
    result = transcribe.transcribe_file(audio)

    assert result["success"] and result["backend"] == "groq" and result["text"] == "from groq"


def test_local_failure_without_groq_key_reports_local_error(monkeypatch, audio):
    monkeypatch.setenv("WHISPER_BACKEND", "whisper-server")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(transcribe, "WHISPER_SERVER_URL", "http://127.0.0.1:8093")
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp(500, {"error": "boom"}))

    result = transcribe.transcribe_file(audio)

    assert not result["success"]
    assert result["backend"] == "whisper-server"
    assert "500" in result["message"]

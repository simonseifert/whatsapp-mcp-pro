"""Voice-note transcripts: stored in app.db, one transcription per message, never written to messages.db."""

import sqlite3

import pytest
from starlette.testclient import TestClient

from tests.conftest import FAMILY, NIKOLA
from wa_app import mcp_lib, store


@pytest.fixture
def voice_id(settings) -> str:
    ro = store.open_ro(settings)
    try:
        messages, _ = store.list_messages_page(ro, FAMILY)
    finally:
        ro.close()
    return next(m.id for m in messages if m.media and m.media.type == "audio")


@pytest.fixture
def transcriber(monkeypatch, settings, voice_id):
    calls: list[str] = []
    answer = {"success": True, "text": "Kupi kruh i mlijeko.", "language": "hr"}

    def fake(path, language=None):
        calls.append(path)
        return answer

    monkeypatch.setattr(mcp_lib, "transcribe_file", fake)
    monkeypatch.setattr(
        mcp_lib,
        "download_media",
        lambda message_id, chat_jid, *, no_phone_retry: str(
            settings.store_path / FAMILY.replace("@", "_") / "audio_20261122_204800.ogg"
        ),
    )
    return calls, answer


def test_transcript_stored_once(logged_in: TestClient, transcriber, voice_id, settings):
    calls, _ = transcriber
    first = logged_in.post(f"/api/transcripts/{FAMILY}/{voice_id}")
    second = logged_in.post(f"/api/transcripts/{FAMILY}/{voice_id}")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"text": "Kupi kruh i mlijeko.", "language": "hr"}
    assert len(calls) == 1
    # The bridge's messages.db is untouched: no transcript text leaks into messages.content.
    ro = sqlite3.connect(f"file:{settings.store_path / 'messages.db'}?mode=ro", uri=True)
    assert ro.execute("SELECT content FROM messages WHERE id=?", (voice_id,)).fetchone()[0] in ("", None)


def test_transcript_backend_missing(logged_in: TestClient, transcriber, voice_id):
    _, answer = transcriber
    answer.clear()
    answer.update({"success": False, "backend": "none", "message": "No transcription backend available. Install..."})
    response = logged_in.post(f"/api/transcripts/{FAMILY}/{voice_id}")
    assert response.status_code == 503
    assert response.json() == {"error": "No transcription backend"}
    # A failure is not stored: once a backend exists, the next try works.
    answer.clear()
    answer.update({"success": True, "text": "Ok.", "language": "en"})
    assert logged_in.post(f"/api/transcripts/{FAMILY}/{voice_id}").json()["text"] == "Ok."


def test_transcript_backend_error_is_502(logged_in: TestClient, transcriber, voice_id):
    _, answer = transcriber
    answer.clear()
    answer.update({"success": False, "backend": "groq", "message": "Transcription failed: boom"})
    response = logged_in.post(f"/api/transcripts/{FAMILY}/{voice_id}")
    assert response.status_code == 502
    assert response.json() == {"error": "Transcription failed"}


def test_transcript_missing_audio_is_404(logged_in: TestClient, monkeypatch, voice_id):
    monkeypatch.setattr(mcp_lib, "download_media", lambda *a, **k: None)
    monkeypatch.setattr(mcp_lib, "transcribe_file", lambda *a, **k: pytest.fail("no audio, no transcription"))
    response = logged_in.post(f"/api/transcripts/{FAMILY}/{voice_id}")
    assert response.status_code == 404


def test_transcript_only_for_voice_notes(logged_in: TestClient, monkeypatch):
    monkeypatch.setattr(mcp_lib, "transcribe_file", lambda *a, **k: pytest.fail("not audio"))
    assert logged_in.post(f"/api/transcripts/{NIKOLA}/nope").status_code == 404


def test_transcript_post_checks_origin(logged_in: TestClient, voice_id):
    response = logged_in.post(f"/api/transcripts/{FAMILY}/{voice_id}", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_transcriptions_run_one_at_a_time(settings, monkeypatch):
    import threading
    import time

    from wa_app import appdb, transcripts

    conn = appdb.connect(settings)
    audio = settings.store_path / FAMILY.replace("@", "_") / "audio_20261122_204800.ogg"
    monkeypatch.setattr(mcp_lib, "download_media", lambda *a, **k: str(audio))
    running = peak = 0
    lock = threading.Lock()

    def slow(path, language=None):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return {"success": True, "text": "x", "language": "hr"}

    monkeypatch.setattr(mcp_lib, "transcribe_file", slow)
    threads = [
        threading.Thread(target=transcripts.get_or_make, args=(conn, settings, f"V{i}", FAMILY)) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 1
    assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 4

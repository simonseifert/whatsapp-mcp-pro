"""Voice-note transcripts: stored in app.db, one transcription per message, never written to messages.db."""

import sqlite3
import time

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

    def fake(path, language=None, *, local_only=False):
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

    def slow(path, language=None, *, local_only=False):
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


# ---------- local-only chats never reach a cloud transcriber ----------


@pytest.fixture
def groq_stand_in(monkeypatch):
    """The real MCP transcriber, local whisper down, Groq replaced by a local server that records every hit."""
    import http.server
    import sys
    import threading

    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = b'{"text": "from stand-in groq", "language": "en"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    mcp_lib._ensure_path()
    from lib import transcribe as lib_transcribe

    monkeypatch.setattr(lib_transcribe, "GROQ_URL", f"http://127.0.0.1:{server.server_port}/transcribe")
    monkeypatch.setattr(lib_transcribe, "WHISPER_SERVER_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("WHISPER_BACKEND", "whisper-server")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.delenv("TRANSCRIBE_CLOUD_FALLBACK", raising=False)
    assert "lib.transcribe" in sys.modules
    yield hits
    server.shutdown()
    server.server_close()


@pytest.fixture
def real_audio(monkeypatch, settings):
    monkeypatch.setattr(
        mcp_lib,
        "download_media",
        lambda message_id, chat_jid, *, no_phone_retry: str(
            settings.store_path / FAMILY.replace("@", "_") / "audio_20261122_204800.ogg"
        ),
    )


def test_local_only_chat_never_reaches_groq(logged_in: TestClient, groq_stand_in, real_audio, voice_id):
    assert logged_in.put(f"/api/chats/{FAMILY}/ai", json={"local_only": True}).status_code == 200

    response = logged_in.post(f"/api/transcripts/{FAMILY}/{voice_id}")

    assert response.status_code == 503
    assert response.json() == {"error": "Local transcription unavailable"}
    assert groq_stand_in == []


def test_other_chats_still_fall_back_to_groq_by_default(logged_in: TestClient, groq_stand_in, real_audio, voice_id):
    response = logged_in.post(f"/api/transcripts/{FAMILY}/{voice_id}")
    assert response.status_code == 200
    assert response.json()["text"] == "from stand-in groq"
    assert groq_stand_in == ["/transcribe"]


def test_cloud_fallback_switch_applies_in_wa_app(
    logged_in: TestClient, groq_stand_in, real_audio, voice_id, monkeypatch
):
    monkeypatch.setenv("TRANSCRIBE_CLOUD_FALLBACK", "0")
    response = logged_in.post(f"/api/transcripts/{FAMILY}/{voice_id}")
    assert response.status_code == 502
    assert groq_stand_in == []


def _flag(conn, jid):
    conn.execute("INSERT INTO chat_ai (chat_jid, local_only) VALUES (?, 1)", (jid,))


@pytest.mark.parametrize(
    ("flagged", "asked"),
    [("IVANA_LID_SHELL", "IVANA"), ("IVANA", "IVANA_LID_SHELL")],
)
def test_flag_on_any_twin_form_keeps_audio_off_groq(settings, groq_stand_in, monkeypatch, flagged, asked):
    from tests import conftest as fx
    from wa_app import appdb, transcripts

    conn = appdb.connect(settings)
    _flag(conn, getattr(fx, flagged))
    audio = settings.store_path / FAMILY.replace("@", "_") / "audio_20261122_204800.ogg"
    monkeypatch.setattr(transcripts, "file_for", lambda *a, **k: audio)

    result = transcripts.get_or_make(conn, settings, "V1", getattr(fx, asked))

    assert result == {"error": "Local transcription unavailable"}
    assert groq_stand_in == []
    assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0


def test_unreadable_message_store_fails_closed(settings, groq_stand_in, monkeypatch):
    """If the aliases of a chat cannot be resolved, its voice note is treated as local only: no Groq."""
    import sqlite3

    from wa_app import appdb, transcripts

    conn = appdb.connect(settings)
    audio = settings.store_path / FAMILY.replace("@", "_") / "audio_20261122_204800.ogg"
    monkeypatch.setattr(transcripts, "file_for", lambda *a, **k: audio)

    def broken(_settings):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(transcripts, "open_ro", broken)
    result = transcripts.get_or_make(conn, settings, "V1", FAMILY)
    assert result == {"error": "Local transcription unavailable"}
    assert groq_stand_in == []


def test_a_new_transcript_makes_the_voice_note_searchable(logged_in: TestClient, transcriber, voice_id):
    assert logged_in.get("/api/search", params={"q": "mlijeko"}).json()["hits"] == []
    assert logged_in.post(f"/api/transcripts/{FAMILY}/{voice_id}").status_code == 200
    for _ in range(100):  # the index updates on its own thread
        hits = logged_in.get("/api/search", params={"q": "mlijeko"}).json()["hits"]
        if hits:
            break
        time.sleep(0.05)
    assert [(h["chat_jid"], h["message_id"]) for h in hits] == [(FAMILY, voice_id)]

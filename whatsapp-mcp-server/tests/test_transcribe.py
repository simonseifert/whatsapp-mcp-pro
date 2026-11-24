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


# ---------- local-only chats and the cloud switch ----------


class _StandInGroq:
    """A local HTTP server in Groq's place. Any request it receives is recorded."""

    def __init__(self) -> None:
        import http.server
        import threading

        outer = self
        self.hits: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                outer.hits.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = b'{"text": "from stand-in groq", "language": "en"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/transcribe"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def groq(monkeypatch):
    stand_in = _StandInGroq()
    monkeypatch.setattr(transcribe, "GROQ_URL", stand_in.url)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("WHISPER_BACKEND", "whisper-server")
    # A closed port: the local server is down, which is when the fallback fires.
    monkeypatch.setattr(transcribe, "WHISPER_SERVER_URL", "http://127.0.0.1:9")
    yield stand_in
    stand_in.close()


def test_stand_in_groq_is_reached_by_the_default_fallback(groq, audio):
    result = transcribe.transcribe_file(audio)
    assert result["backend"] == "groq"
    assert groq.hits == ["/transcribe"]


def test_local_only_never_calls_groq_when_whisper_fails(groq, audio):
    result = transcribe.transcribe_file(audio, local_only=True)

    assert result["success"] is False
    assert result["message"] == transcribe.LOCAL_UNAVAILABLE == "Local transcription unavailable"
    assert groq.hits == []


def test_local_only_refuses_an_explicit_groq_backend(groq, monkeypatch, audio):
    monkeypatch.setenv("WHISPER_BACKEND", "groq")
    result = transcribe.transcribe_file(audio, local_only=True)
    assert result["message"] == transcribe.LOCAL_UNAVAILABLE
    assert groq.hits == []


def test_local_only_never_auto_picks_groq(groq, monkeypatch, audio):
    monkeypatch.setenv("WHISPER_BACKEND", "auto")
    monkeypatch.setattr(transcribe, "WHISPER_SERVER_URL", "")
    monkeypatch.setitem(__import__("sys").modules, "mlx_whisper", None)
    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", None)
    assert transcribe.available_backend() == "groq"
    assert transcribe.available_backend(local_only=True) == "none"
    result = transcribe.transcribe_file(audio, local_only=True)
    assert result["message"] == transcribe.LOCAL_UNAVAILABLE
    assert groq.hits == []


def test_local_only_still_uses_a_working_local_backend(groq, monkeypatch, audio):
    monkeypatch.setattr(transcribe, "WHISPER_SERVER_URL", "http://127.0.0.1:8093")
    real_post = requests.post

    def fake_post(url, **kwargs):
        if "8093" in url:
            return _Resp(200, {"text": "local text", "language": "en"})
        return real_post(url, **kwargs)

    monkeypatch.setattr(requests, "post", fake_post)
    result = transcribe.transcribe_file(audio, local_only=True)
    assert result["success"] and result["backend"] == "whisper-server"
    assert groq.hits == []


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
def test_cloud_fallback_switch_disables_the_groq_fallback(groq, monkeypatch, audio, value):
    monkeypatch.setenv("TRANSCRIBE_CLOUD_FALLBACK", value)
    result = transcribe.transcribe_file(audio)
    assert result["success"] is False
    assert result["backend"] == "whisper-server"
    assert groq.hits == []


def test_cloud_fallback_switch_stops_auto_from_choosing_groq(groq, monkeypatch):
    monkeypatch.setenv("WHISPER_BACKEND", "auto")
    monkeypatch.setattr(transcribe, "WHISPER_SERVER_URL", "")
    monkeypatch.setitem(__import__("sys").modules, "mlx_whisper", None)
    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", None)
    monkeypatch.setenv("TRANSCRIBE_CLOUD_FALLBACK", "0")
    assert transcribe.available_backend() == "none"


def test_cloud_fallback_defaults_to_on(monkeypatch):
    monkeypatch.delenv("TRANSCRIBE_CLOUD_FALLBACK", raising=False)
    assert transcribe.cloud_fallback_enabled() is True


def _app_db(tmp_path, local_only: int):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "app.db")
    conn.execute("CREATE TABLE chat_ai (chat_jid TEXT PRIMARY KEY, local_only INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO chat_ai VALUES ('priv@s.whatsapp.net', ?)", (local_only,))
    conn.execute("INSERT INTO chat_ai VALUES ('open@s.whatsapp.net', 0)")
    conn.commit()
    conn.close()


def test_is_local_only_chat_reads_the_wa_app_flag(monkeypatch, tmp_path):
    _app_db(tmp_path, 1)
    monkeypatch.setenv("WA_APP_DATA", str(tmp_path))
    assert transcribe.is_local_only_chat("priv@s.whatsapp.net") is True
    assert transcribe.is_local_only_chat("open@s.whatsapp.net") is False
    assert transcribe.is_local_only_chat("unknown@s.whatsapp.net") is False


def test_is_local_only_chat_without_wa_app_is_false(monkeypatch, tmp_path):
    monkeypatch.delenv("WA_APP_DATA", raising=False)
    monkeypatch.delenv("WA_APP_AGENT_TOKEN", raising=False)
    monkeypatch.setattr(transcribe.Path, "home", lambda: tmp_path / "no-home-here")
    assert transcribe.is_local_only_chat("priv@s.whatsapp.net") is False


@pytest.mark.parametrize("setting", ["WA_APP_DATA", "WA_APP_AGENT_TOKEN"])
def test_a_configured_wa_app_with_no_readable_app_db_fails_closed(monkeypatch, tmp_path, setting):
    """The data-dir dependency is explicit: if wa-app is set up but its app.db is not where this process looks,
    every chat counts as local only instead of silently none."""
    monkeypatch.delenv("WA_APP_DATA", raising=False)
    monkeypatch.delenv("WA_APP_AGENT_TOKEN", raising=False)
    monkeypatch.setenv(setting, str(tmp_path / "elsewhere") if setting == "WA_APP_DATA" else "t" * 40)
    monkeypatch.setattr(transcribe.Path, "home", lambda: tmp_path / "no-home-here")
    assert transcribe.is_local_only_chat("open@s.whatsapp.net") is True


def test_is_local_only_chat_fails_closed_on_an_unreadable_db(monkeypatch, tmp_path):
    (tmp_path / "app.db").write_bytes(b"this is not a sqlite file" * 10)
    monkeypatch.setenv("WA_APP_DATA", str(tmp_path))
    assert transcribe.is_local_only_chat("open@s.whatsapp.net") is True


def test_auto_tick_keeps_local_only_chats_off_groq(groq, monkeypatch, tmp_path, audio):
    import whatsapp

    _app_db(tmp_path, 1)
    monkeypatch.setenv("WA_APP_DATA", str(tmp_path))
    monkeypatch.setattr(transcribe, "_failed", set())
    monkeypatch.setattr(transcribe, "_pending_voice_notes", lambda limit, age: [("m1", "priv@s.whatsapp.net")])
    monkeypatch.setattr(whatsapp, "download_media", lambda message_id, chat_jid: audio)
    stored: list[tuple] = []
    monkeypatch.setattr(transcribe, "_store_transcript", lambda *a: stored.append(a))

    transcribe._auto_tick(3, 3)

    assert groq.hits == []
    assert stored == []


def test_transcribe_audio_tool_passes_the_chat_flag(monkeypatch, tmp_path, audio):
    import main

    _app_db(tmp_path, 1)
    monkeypatch.setenv("WA_APP_DATA", str(tmp_path))
    monkeypatch.setattr(main, "whatsapp_download_media", lambda message_id, chat_jid: audio)
    seen: list[bool] = []

    def fake(path, language=None, *, local_only=False):
        seen.append(local_only)
        return {"success": False, "backend": "local", "message": transcribe.LOCAL_UNAVAILABLE}

    monkeypatch.setattr(transcribe, "transcribe_file", fake)
    main.transcribe_audio("m1", "priv@s.whatsapp.net")
    main.transcribe_audio("m1", "open@s.whatsapp.net")
    assert seen == [True, False]


def _messages_db(path):
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TEXT, merged_into TEXT);
        CREATE TABLE identities (jid TEXT PRIMARY KEY, lid TEXT, phone_jid TEXT);
        INSERT INTO chats VALUES ('15550001@s.whatsapp.net', 'Twin', NULL, NULL);
        INSERT INTO chats VALUES ('999@lid', 'Twin', NULL, '15550001@s.whatsapp.net');
        INSERT INTO identities VALUES ('15550001@s.whatsapp.net', '999@lid', '15550001@s.whatsapp.net');
        """
    )
    conn.commit()
    return conn


@pytest.mark.parametrize(
    ("flagged", "asked"),
    [
        ("999@lid", "15550001@s.whatsapp.net"),
        ("15550001@s.whatsapp.net", "999@lid"),
    ],
)
def test_flag_on_any_twin_form_counts(monkeypatch, tmp_path, flagged, asked):
    import sqlite3

    msgs = _messages_db(tmp_path / "messages.db")
    (tmp_path / "data").mkdir()
    app = sqlite3.connect(tmp_path / "data" / "app.db")
    app.execute("CREATE TABLE chat_ai (chat_jid TEXT PRIMARY KEY, local_only INTEGER NOT NULL DEFAULT 0)")
    app.execute("INSERT INTO chat_ai VALUES (?, 1)", (flagged,))
    app.commit()
    app.close()
    monkeypatch.setenv("WA_APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(transcribe, "MESSAGES_DB_PATH", str(tmp_path / "messages.db"))

    assert transcribe.is_local_only_chat(asked) is True
    assert transcribe.is_local_only_chat(asked, messages_conn=msgs) is True
    assert transcribe.is_local_only_chat("other@s.whatsapp.net", messages_conn=msgs) is False


@pytest.mark.parametrize(
    ("flagged", "asked"), [("999@lid", "15550001@s.whatsapp.net"), ("15550001@s.whatsapp.net", "999@lid")]
)
def test_auto_tick_twin_flag_never_reaches_groq(groq, monkeypatch, tmp_path, audio, flagged, asked):
    import sqlite3

    import whatsapp

    _messages_db(tmp_path / "messages.db").close()
    (tmp_path / "data").mkdir()
    app = sqlite3.connect(tmp_path / "data" / "app.db")
    app.execute("CREATE TABLE chat_ai (chat_jid TEXT PRIMARY KEY, local_only INTEGER NOT NULL DEFAULT 0)")
    app.execute("INSERT INTO chat_ai VALUES (?, 1)", (flagged,))
    app.commit()
    app.close()
    monkeypatch.setenv("WA_APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(transcribe, "MESSAGES_DB_PATH", str(tmp_path / "messages.db"))
    monkeypatch.setattr(transcribe, "_failed", set())
    monkeypatch.setattr(transcribe, "_pending_voice_notes", lambda limit, age: [("m1", asked)])
    monkeypatch.setattr(whatsapp, "download_media", lambda message_id, chat_jid: audio)

    transcribe._auto_tick(3, 3)

    assert groq.hits == []


def test_transcribe_audio_file_tool_is_always_local_only(monkeypatch, tmp_path):
    import main

    f = tmp_path / "note.ogg"
    f.write_bytes(b"OggS")
    monkeypatch.setattr(main, "validate_outgoing_media_path", lambda p: p)
    seen: list[bool] = []

    def fake(path, language=None, *, local_only=False):
        seen.append(local_only)
        return {"success": False, "backend": "local", "message": transcribe.LOCAL_UNAVAILABLE}

    monkeypatch.setattr(transcribe, "transcribe_file", fake)
    main.transcribe_audio_file(str(f))
    assert seen == [True]


def test_transcribe_audio_file_never_reaches_groq(groq, monkeypatch, tmp_path):
    import main

    f = tmp_path / "note.ogg"
    f.write_bytes(b"OggS")
    monkeypatch.setattr(main, "validate_outgoing_media_path", lambda p: p)
    result = main.transcribe_audio_file(str(f))
    assert result["message"] == transcribe.LOCAL_UNAVAILABLE
    assert groq.hits == []


def test_local_only_voice_note_that_failed_is_retried_after_the_delay(groq, monkeypatch, tmp_path, audio):
    import whatsapp

    _app_db(tmp_path, 1)
    monkeypatch.setenv("WA_APP_DATA", str(tmp_path))
    monkeypatch.setattr(transcribe, "_failed", set())
    monkeypatch.setattr(transcribe, "_retry_at", {})
    monkeypatch.setattr(transcribe, "_pending_voice_notes", lambda limit, age: [("m1", "priv@s.whatsapp.net")])
    monkeypatch.setattr(whatsapp, "download_media", lambda message_id, chat_jid: audio)
    stored: list[tuple] = []
    monkeypatch.setattr(transcribe, "_store_transcript", lambda *a: stored.append(a))
    clock = [1000.0]
    monkeypatch.setattr(transcribe.time, "time", lambda: clock[0])

    transcribe._auto_tick(3, 3)  # whisper is down: LOCAL_UNAVAILABLE, not a permanent failure
    assert ("m1", "priv@s.whatsapp.net") not in transcribe._failed
    calls: list[str] = []
    real = transcribe.transcribe_file

    def counting(*a, **k):
        calls.append("x")
        return real(*a, **k)

    monkeypatch.setattr(transcribe, "transcribe_file", counting)
    transcribe._auto_tick(3, 3)  # inside the delay: not tried
    assert calls == []

    clock[0] += transcribe.LOCAL_RETRY_SECONDS + 1
    monkeypatch.setattr(transcribe, "WHISPER_SERVER_URL", "http://127.0.0.1:8093")
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, **kw: _Resp(200, {"text": "now it works", "language": "en"}) if "8093" in url else None,
    )
    transcribe._auto_tick(3, 3)  # whisper is back: transcribed
    assert [s[2] for s in stored] == ["now it works"]
    assert groq.hits == []


def test_auto_tick_logs_forged_message_ids_on_one_line(groq, monkeypatch, tmp_path, audio, caplog):
    import whatsapp

    monkeypatch.setenv("WA_APP_DATA", str(tmp_path / "nothing"))
    monkeypatch.delenv("WA_APP_AGENT_TOKEN", raising=False)
    monkeypatch.setattr(transcribe.Path, "home", lambda: tmp_path / "h")
    monkeypatch.setenv("TRANSCRIBE_CLOUD_FALLBACK", "0")
    monkeypatch.setattr(transcribe, "_failed", set())
    forged = "AB\n2026-11-24 INFO forged line"
    monkeypatch.setattr(transcribe, "_pending_voice_notes", lambda limit, age: [(forged, "x@s.whatsapp.net")])
    monkeypatch.setattr(whatsapp, "download_media", lambda message_id, chat_jid: audio)
    with caplog.at_level("INFO"):
        transcribe._auto_tick(3, 3)
    assert not any("\n" in r.getMessage() for r in caplog.records)


def test_wa_app_data_expands_a_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WA_APP_DATA", "~/wa-data")
    assert transcribe._wa_app_db_path() == tmp_path / "wa-data" / "app.db"


def test_local_only_retries_stop_after_the_attempt_limit(groq, monkeypatch, tmp_path, audio):
    import whatsapp

    _app_db(tmp_path, 1)
    monkeypatch.setenv("WA_APP_DATA", str(tmp_path))
    key = ("m1", "priv@s.whatsapp.net")
    monkeypatch.setattr(transcribe, "_failed", set())
    monkeypatch.setattr(transcribe, "_retry_at", {})
    monkeypatch.setattr(transcribe, "_attempts", {})
    monkeypatch.setattr(transcribe, "_pending_voice_notes", lambda limit, age: [key])
    monkeypatch.setattr(whatsapp, "download_media", lambda message_id, chat_jid: audio)
    clock = [1000.0]
    monkeypatch.setattr(transcribe.time, "time", lambda: clock[0])

    for _ in range(transcribe.LOCAL_MAX_ATTEMPTS):
        assert key not in transcribe._failed
        transcribe._auto_tick(3, 3)
        clock[0] += transcribe.LOCAL_RETRY_SECONDS + 1

    assert key in transcribe._failed  # given up on
    assert key not in transcribe._retry_at and key not in transcribe._attempts  # and cleaned up
    assert groq.hits == []

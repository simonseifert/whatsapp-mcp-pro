import sys

from wa_app import mcp_lib


def test_server_dir_is_the_repo_sibling_and_on_the_path() -> None:
    assert (mcp_lib.server_dir() / "lib" / "naming.py").is_file()
    assert str(mcp_lib.server_dir()) in sys.path


def test_env_overrides_server_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WA_MCP_SERVER_DIR", str(tmp_path))
    assert mcp_lib.server_dir() == tmp_path


def test_helpers_are_importable_without_a_real_store() -> None:
    assert callable(mcp_lib.resolve_display_names) and callable(mcp_lib.chat_forms)


def test_history_backfill_pass_throughs(monkeypatch) -> None:
    import whatsapp

    seen = {}

    def start(chat_jid, until=None, max_batches=40):
        seen["start"] = (chat_jid, until, max_batches)
        return {"success": True}

    monkeypatch.setattr(whatsapp, "start_history_backfill", start)
    monkeypatch.setattr(whatsapp, "history_backfill_status", lambda: {"success": True, "jobs": []})
    assert {"start_history_backfill", "history_backfill_status"} <= set(mcp_lib.__all__)
    assert mcp_lib.start_history_backfill("x@s.whatsapp.net", "2025-01-01", 200) == {"success": True}
    assert seen["start"] == ("x@s.whatsapp.net", "2025-01-01", 200)
    assert mcp_lib.history_backfill_status() == {"success": True, "jobs": []}


def test_send_pass_throughs(monkeypatch) -> None:
    import whatsapp

    calls: dict[str, tuple] = {}

    def fake(name):
        def inner(*args, **kwargs):
            calls[name] = (args, kwargs)
            return {"success": True, "message_id": "M1"}

        return inner

    monkeypatch.setattr(whatsapp, "send_message", fake("message"))
    monkeypatch.setattr(whatsapp, "send_file", fake("file"))
    monkeypatch.setattr(whatsapp, "send_audio_message", fake("audio"))
    assert {"send_file", "send_audio_message"} <= set(mcp_lib.__all__)
    group = "120363000000000001@g.us"

    mcp_lib.send_message("a@s.whatsapp.net", "hi", "Q1", "k1", quoted_chat_jid=group)
    assert calls["message"] == (
        ("a@s.whatsapp.net", "hi"),
        {"quoted_message_id": "Q1", "idempotency_key": "k1", "quoted_chat_jid": group},
    )

    result = mcp_lib.send_file(
        "a@s.whatsapp.net",
        file_path="/tmp/x/račun.pdf",
        filename="račun.pdf",
        caption="Evo",
        quoted_message_id="Q1",
        quoted_chat_jid=group,
        idempotency_key="k2",
    )
    assert result["success"] is True
    assert calls["file"] == (
        ("a@s.whatsapp.net",),
        {
            "media_path": "/tmp/x/račun.pdf",
            "filename": "račun.pdf",
            "idempotency_key": "k2",
            "caption": "Evo",
            "quoted_message_id": "Q1",
            "quoted_chat_jid": group,
        },
    )

    mcp_lib.send_audio_message(
        "a@s.whatsapp.net", file_path="/tmp/x/v.ogg", quoted_message_id="Q1", idempotency_key="k3"
    )
    assert calls["audio"] == (
        ("a@s.whatsapp.net", "/tmp/x/v.ogg"),
        {"idempotency_key": "k3", "quoted_message_id": "Q1", "quoted_chat_jid": None},
    )

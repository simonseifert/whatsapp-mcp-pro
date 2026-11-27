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

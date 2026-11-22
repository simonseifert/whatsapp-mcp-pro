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

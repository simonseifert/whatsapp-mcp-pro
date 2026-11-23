import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

import httpx2
import pytest

from wa_app.ai import backends
from wa_app.ai.backends import BackendError, Prompt
from wa_app.ai.config import BackendCfg

KEY = "sk-test-FICTIONAL-0123456789"
PROMPT = Prompt(system="You draft replies.", user="Nikola: are we still on for Friday?")


def _run(coro):
    return asyncio.run(coro)


def _openai() -> BackendCfg:
    return BackendCfg(1, "openai", "Local", "http://127.0.0.1:11434/v1/", "llama-test", True)


def test_openai_payload_and_parse(settings) -> None:
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200, json={"choices": [{"message": {"content": '{"alternatives": ["Yes, Friday works"]}'}}]}
        )

    text = _run(backends.complete(_openai(), KEY, PROMPT, settings, transport=httpx2.MockTransport(handler)))
    assert backends.parse_alternatives(text) == ["Yes, Friday works"]
    assert seen["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["body"]["model"] == "llama-test"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["messages"] == [
        {"role": "system", "content": PROMPT.system},
        {"role": "user", "content": PROMPT.user},
    ]


def test_openai_without_key_sends_no_auth(settings) -> None:
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx2.Response(200, json={"choices": [{"message": {"content": "[]"}}]})

    _run(backends.complete(_openai(), None, PROMPT, settings, transport=httpx2.MockTransport(handler)))
    assert seen["auth"] is None


def test_anthropic_cache_control_and_headers(settings) -> None:
    cfg = BackendCfg(2, "anthropic", "Claude", "http://127.0.0.1:1/ignored", "claude-test", True)
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={"content": [{"type": "text", "text": '["Sure"]'}]})

    text = _run(backends.complete(cfg, KEY, PROMPT, settings, transport=httpx2.MockTransport(handler)))
    assert text == '["Sure"]'
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == KEY
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    body = seen["body"]
    assert body["max_tokens"] == 600
    assert body["model"] == "claude-test"
    assert body["system"] == [{"type": "text", "text": PROMPT.system, "cache_control": {"type": "ephemeral"}}]
    assert body["messages"] == [{"role": "user", "content": PROMPT.user}]


def test_http_error_is_backend_error_without_key(settings) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, text=f"bad key {KEY} for {PROMPT.user}")

    with pytest.raises(BackendError) as exc:
        _run(backends.complete(_openai(), KEY, PROMPT, settings, transport=httpx2.MockTransport(handler)))
    assert str(exc.value) == "AI request failed (HTTP 401)"
    assert KEY not in str(exc.value) and "Friday" not in str(exc.value)


def test_network_error_and_garbage_are_backend_errors(settings) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"connect failed {KEY}", request=request)

    with pytest.raises(BackendError) as exc:
        _run(backends.complete(_openai(), KEY, PROMPT, settings, transport=httpx2.MockTransport(refuse)))
    assert KEY not in str(exc.value)

    def junk(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<html>nope</html>")

    with pytest.raises(BackendError):
        _run(backends.complete(_openai(), KEY, PROMPT, settings, transport=httpx2.MockTransport(junk)))


def test_http_timeout_message(settings) -> None:
    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    with pytest.raises(BackendError, match="didn't answer in time"):
        _run(backends.complete(_openai(), KEY, PROMPT, settings, transport=httpx2.MockTransport(slow)))


# --- claude CLI against a fake binary -------------------------------------------------------------------------

FAKE = """#!{python}
import json, os, sys
out = os.environ["FAKE_OUT"]
mode = os.environ.get("FAKE_MODE", "ok")
open(out + ".argv", "w").write(json.dumps(sys.argv[1:]))
open(out + ".stdin", "w").write(sys.stdin.read())
open(out + ".cwd", "w").write(os.getcwd())
open(out + ".pid", "w").write(str(os.getpid()))
if mode == "hang":
    import time
    time.sleep(60)
elif mode == "fail":
    sys.exit(3)
elif mode == "garbage":
    print("not json")
elif mode == "error":
    print(json.dumps({{"is_error": True, "result": "boom sk-secret"}}))
else:
    print(json.dumps({{"is_error": False, "result": '{{"alternatives": ["Friday works"]}}'}}))
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(FAKE.format(python=sys.executable))
    path.chmod(0o755)
    monkeypatch.setenv("WA_APP_CLAUDE_BIN", str(path))
    monkeypatch.setenv("FAKE_OUT", str(tmp_path / "rec"))
    return tmp_path / "rec"


def _cli() -> BackendCfg:
    return BackendCfg(3, "claude_cli", "CLI", "http://ignored.example.com", "haiku-test", False)


def test_claude_cli_args_exact(settings, fake_claude) -> None:
    text = _run(backends.complete(_cli(), None, PROMPT, settings))
    assert backends.parse_alternatives(text) == ["Friday works"]
    argv = json.loads(Path(str(fake_claude) + ".argv").read_text())
    assert argv == [
        "-p",
        "--output-format",
        "json",
        "--model",
        "haiku-test",
        "--system-prompt",
        PROMPT.system,
        "--tools",
        "",
        "--disallowedTools",
        "*",
        "--max-turns",
        "1",
        "--strict-mcp-config",
        "--setting-sources",
        "",
    ]
    assert Path(str(fake_claude) + ".stdin").read_text() == PROMPT.user
    cwd = settings.data_dir / "claude-cwd"
    assert Path(str(fake_claude) + ".cwd").read_text() == str(cwd.resolve())
    assert list(cwd.iterdir()) == []
    assert stat.S_IMODE(cwd.stat().st_mode) == 0o700


def test_claude_cli_binary_from_path(settings, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("WA_APP_CLAUDE_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(BackendError, match="claude command"):
        _run(backends.complete(_cli(), None, PROMPT, settings))


def test_claude_cli_timeout_kills(settings, fake_claude, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "hang")
    started = time.monotonic()
    with pytest.raises(BackendError, match="didn't answer in time"):
        _run(backends.complete(_cli(), None, PROMPT, settings, timeout=1.0))
    assert time.monotonic() - started < 10
    pid = int(Path(str(fake_claude) + ".pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_claude_cli_cancel_kills(settings, fake_claude, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "hang")

    async def scenario() -> None:
        task = asyncio.ensure_future(backends.complete(_cli(), None, PROMPT, settings))
        pid_file = Path(str(fake_claude) + ".pid")
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(scenario())
    pid = int(Path(str(fake_claude) + ".pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("mode", ["fail", "garbage", "error"])
def test_claude_cli_failures_are_backend_errors(settings, fake_claude, monkeypatch, mode) -> None:
    monkeypatch.setenv("FAKE_MODE", mode)
    with pytest.raises(BackendError) as exc:
        _run(backends.complete(_cli(), None, PROMPT, settings))
    assert "sk-secret" not in str(exc.value)


# --- parse_alternatives ---------------------------------------------------------------------------------------


def test_parse_alternatives_variants() -> None:
    assert backends.parse_alternatives('{"alternatives": ["a", "b"]}') == ["a", "b"]
    assert backends.parse_alternatives('["a"]') == ["a"]
    assert backends.parse_alternatives('```json\n{"alternatives": [" a ", "a", "b"]}\n```') == ["a", "b"]
    assert backends.parse_alternatives('{"alternatives": ["a", "b", "c"]}') == ["a", "b"]
    for bad in ('[""]', '{"alternatives": []}', "not json", '{"x": 1}', '"just text"', ""):
        with pytest.raises(BackendError, match="No usable draft"):
            backends.parse_alternatives(bad)

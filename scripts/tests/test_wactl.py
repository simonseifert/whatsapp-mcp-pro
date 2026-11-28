"""Pure-function tests for wactl. No real services, no network, temp dirs only."""

from __future__ import annotations

import importlib.util
import plistlib
import re
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "wactl.py"
spec = importlib.util.spec_from_file_location("wactl", SCRIPT)
wactl = importlib.util.module_from_spec(spec)
sys.modules["wactl"] = wactl
spec.loader.exec_module(wactl)

SECRET = "f" * 64


# ---------------------------------------------------------------- env


def test_parse_env_handles_comments_quotes_and_export():
    text = "# c\n\nA=1\nB='two words'\nC=\"x # y\"\nexport D=4\nE=5 # trailing\nbad line\n"
    assert wactl.parse_env(text) == {"A": "1", "B": "two words", "C": "x # y", "D": "4", "E": "5"}


def test_set_env_value_replaces_active_then_commented_then_appends():
    text = "A=1\n# B=\nC=3\n"
    assert wactl.set_env_value(text, "A", "9") == "A=9\n# B=\nC=3\n"
    assert wactl.set_env_value(text, "B", "x") == "A=1\nB=x\nC=3\n"
    assert wactl.set_env_value(text, "Z", "z") == "A=1\n# B=\nC=3\nZ=z\n"


def test_generate_api_key_is_64_hex_and_random():
    a, b = wactl.generate_api_key(), wactl.generate_api_key()
    assert re.fullmatch(r"[0-9a-f]{64}", a) and a != b


def test_ensure_env_creates_from_example_with_key_mode_600(tmp_path):
    example = tmp_path / ".env.example"
    example.write_text("API_KEY=CHANGEME_USE_openssl_rand_hex_32\nAPI_PORT=8080\n")
    env = tmp_path / ".env"
    assert wactl.ensure_env(env, example) is True
    values = wactl.parse_env(env.read_text())
    assert re.fullmatch(r"[0-9a-f]{64}", values["API_KEY"])
    assert values["API_PORT"] == "8080"
    if sys.platform != "win32":
        assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_ensure_env_never_overwrites(tmp_path):
    example = tmp_path / ".env.example"
    example.write_text("API_KEY=CHANGEME\n")
    env = tmp_path / ".env"
    env.write_text("API_KEY=mine\nEXTRA=1\n")
    assert wactl.ensure_env(env, example) is False
    assert env.read_text() == "API_KEY=mine\nEXTRA=1\n"


def test_resolve_data_dir_precedence(tmp_path):
    repo = tmp_path
    assert wactl.resolve_data_dir(None, {}, repo) == repo / "whatsapp-bridge"
    assert wactl.resolve_data_dir(None, {"WA_DATA_DIR": str(tmp_path / "d")}, repo) == tmp_path / "d"
    assert wactl.resolve_data_dir(str(tmp_path / "x"), {"WA_DATA_DIR": "/nope"}, repo) == tmp_path / "x"
    assert wactl.resolve_data_dir(None, {"WA_DATA_DIR": "rel"}, repo) == repo / "rel"


def test_component_env_sets_store_and_isolates_app_data(tmp_path):
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    env = wactl.component_env("app", repo, data, {"API_PORT": "9000"})
    assert env["WA_STORE_PATH"] == str(data / "store")
    assert env["WA_APP_DATA"] == str(data / "wa-app")
    mcp = wactl.component_env("mcp", repo, data, {"API_PORT": "9000"})
    assert mcp["BRIDGE_HOST"] == "127.0.0.1:9000"
    assert mcp["MCP_HOST"] == "127.0.0.1"
    # default data dir keeps wa-app's own default location
    default = repo / "whatsapp-bridge"
    assert "WA_APP_DATA" not in wactl.component_env("app", repo, default, {})
    # explicit user values win
    assert wactl.component_env("app", repo, data, {"WA_STORE_PATH": "/s"})["WA_STORE_PATH"] == "/s"


# ---------------------------------------------------------------- doctor


@pytest.mark.parametrize(
    ("system", "expect", "skip"),
    [
        ("Linux", "apt", {"uv", "pnpm", "tailscale"}),
        ("Darwin", "brew", {"cc"}),
        ("Windows", "winget", {"pnpm"}),
    ],
)
def test_install_hint_per_os(system, expect, skip):
    for tool in ("uv", "go", "node", "pnpm", "ffmpeg", "tailscale", "cc"):
        hint = wactl.install_hint(tool, system)
        assert hint and (tool in skip or expect in hint)
    assert "xcode-select" in wactl.install_hint("cc", "Darwin")
    assert "corepack" in wactl.install_hint("pnpm", "Windows")


def test_go_required_version_from_go_mod():
    assert wactl.go_required_version("module x\n\ngo 1.26.0\n\ntoolchain go1.27.4\n") == (1, 26)
    assert wactl.parse_version("go version go1.24.4 linux/amd64") == (1, 24, 4)
    assert wactl.parse_version("v22.23.4") == (22, 23, 4)


def test_check_go_accepts_older_with_toolchain_download():
    ok, note = wactl.judge_go((1, 24, 4), (1, 26), toolchain_env="")
    assert ok and "download" in note
    ok, _ = wactl.judge_go((1, 24, 4), (1, 26), toolchain_env="local")
    assert not ok
    ok, _ = wactl.judge_go((1, 27, 0), (1, 26), toolchain_env="local")
    assert ok


# ---------------------------------------------------------------- services


ARGV = [
    "/opt/bin/uv",
    "run",
    "--quiet",
    "--no-project",
    "/r/scripts/wactl.py",
    "run",
    "bridge",
    "--data",
    "/d",
]


def test_service_argv_shape():
    argv = wactl.service_argv("bridge", "/opt/bin/uv", Path("/r/scripts/wactl.py"), Path("/d"))
    assert argv == [
        "/opt/bin/uv",
        "run",
        "--quiet",
        "--no-project",
        str(Path("/r/scripts/wactl.py")),
        "run",
        "bridge",
        "--data",
        str(Path("/d")),
    ]


def test_systemd_unit_exact_paths_and_exit_code_3():
    unit = wactl.render_systemd_unit("bridge", ARGV, Path("/d"))
    assert unit.startswith(wactl.MARKER)
    assert "ExecStart=/opt/bin/uv run --quiet --no-project /r/scripts/wactl.py run bridge --data /d\n" in unit
    assert "WorkingDirectory=/d\n" in unit
    assert "RestartPreventExitStatus=3" in unit
    assert "WantedBy=default.target" in unit


def test_systemd_unit_quotes_spaces_and_percent():
    argv = ["/o b/uv", "run", "/r 100%/wactl.py", "run", "app", "--data", "/d x"]
    unit = wactl.render_systemd_unit("app", argv, Path("/d x"))
    assert 'ExecStart="/o b/uv" run "/r 100%%/wactl.py" run app --data "/d x"\n' in unit
    assert "wa-bridge.service" in unit.split("After=")[1].splitlines()[0]


def test_plist_is_runatload_no_keepalive_and_calls_run(tmp_path):
    data = plistlib.loads(wactl.render_launchd_plist("bridge", ARGV, Path("/d"), "/usr/bin:/bin"))
    assert data["Label"] == "com.wactl.bridge"
    assert data["ProgramArguments"] == ARGV
    assert data["RunAtLoad"] is True and data["KeepAlive"] is False
    assert data["WorkingDirectory"] == "/d"
    assert data["StandardOutPath"].endswith("bridge.log")
    assert "API_KEY" not in str(data)


def test_write_secret_file_is_mode_600(tmp_path):
    p = tmp_path / "a" / "x.plist"
    wactl.write_private(p, b"x")
    if sys.platform != "win32":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_schtasks_args_quote_path_with_spaces():
    launcher = r"C:\Users\Jane Doe\wa tools\wactl.cmd"
    data = r"C:\Users\Jane Doe\wa data"
    args = wactl.schtasks_create_args("bridge", launcher, data)
    assert args[:1] == ["schtasks"]
    assert args[args.index("/SC") + 1] == "ONLOGON"
    assert args[args.index("/TN") + 1] == "wactl-bridge"
    assert args[args.index("/RL") + 1] == "LIMITED"
    assert "/F" in args
    tr = args[args.index("/TR") + 1]
    assert "-WindowStyle Hidden" in tr
    assert f"'{launcher}' run bridge --data '{data}'" in tr
    assert "--log" in tr
    # the full command line, as Windows would see it, keeps /TR as one argument
    line = subprocess.list2cmdline(args)
    assert tr.replace('"', '\\"') in line


def test_schtasks_args_escape_single_quote_in_path():
    tr = wactl.schtasks_create_args("app", r"C:\O'Neil\wactl.cmd", r"C:\d")[
        wactl.schtasks_create_args("app", "x", "y").index("/TR") + 1
    ]
    assert "O''Neil" in tr


def test_refuses_foreign_unit(tmp_path):
    d = tmp_path / "systemd"
    d.mkdir()
    (d / "wa-app.service").write_text("[Unit]\nDescription=someone else\n")
    msg = wactl.foreign_unit_conflict(d, ["bridge", "app"])
    assert msg and "wa-app.service" in msg
    (d / "wa-app.service").write_text(wactl.MARKER + "\n[Unit]\n")
    assert wactl.foreign_unit_conflict(d, ["bridge", "app"]) is None


# ---------------------------------------------------------------- supervisor


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeChild:
    def __init__(self, clock, code, uptime):
        self.clock, self.code, self.uptime = clock, code, uptime

    def wait(self):
        self.clock.t += self.uptime
        return self.code


def run_supervisor(script, max_starts=100):
    """script: list of (exit_code, uptime). Returns (result, sleeps)."""
    clock = FakeClock()
    sleeps = []
    it = iter(script)
    stop = threading.Event()

    def spawn():
        try:
            code, up = next(it)
        except StopIteration:
            stop.set()
            return FakeChild(clock, 0, 0)
        return FakeChild(clock, code, up)

    def sleep(sec):
        sleeps.append(sec)
        clock.t += sec
        return stop.is_set()

    result = wactl.supervise(spawn, sleep=sleep, clock=clock, stop=stop, log=lambda m: None)
    return result, sleeps


def test_backoff_sequence_doubles_to_60():
    assert [wactl.next_delay(p, 1) for p in (None, 2, 4, 8, 16, 32, 60)] == [2, 4, 8, 16, 32, 60, 60]


def test_backoff_resets_after_ten_minutes_uptime():
    assert wactl.next_delay(32, 600) == 2
    assert wactl.next_delay(32, 599) == 60


def test_crash_restarts_with_backoff_then_clean_exit_stops():
    result, sleeps = run_supervisor([(1, 1), (1, 1), (1, 1), (0, 5)])
    assert result == 0
    assert sleeps == [2, 4, 8]


def test_exit_3_stops_without_restart():
    result, sleeps = run_supervisor([(3, 1), (0, 1)])
    assert result == 3 and sleeps == []


def test_uptime_reset_in_supervise():
    result, sleeps = run_supervisor([(1, 1), (1, 1), (1, 700), (1, 1), (0, 1)])
    assert sleeps == [2, 4, 2, 4]


def test_stop_request_ends_loop_without_restart():
    clock = FakeClock()
    stop = threading.Event()

    class Child:
        def wait(self):
            stop.set()
            return -15

    spawned = []

    def spawn():
        spawned.append(1)
        return Child()

    res = wactl.supervise(spawn, sleep=lambda s: stop.is_set(), clock=clock, stop=stop, log=lambda m: None)
    assert res == 0 and len(spawned) == 1


# ---------------------------------------------------------------- update


def git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": __import__("os").environ["PATH"],
            "HOME": str(cwd),
        },
    )


@pytest.fixture
def clone(tmp_path):
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    work = tmp_path / "work"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", str(origin), str(seed))
    (seed / "a.txt").write_text("1")
    git(seed, "add", "a.txt")
    git(seed, "commit", "-m", "one")
    git(seed, "push", "origin", "HEAD:main")
    git(tmp_path, "clone", str(origin), str(work))
    return origin, seed, work


def test_update_ready_when_clean_and_behind(clone):
    _, seed, work = clone
    (seed / "a.txt").write_text("2")
    git(seed, "commit", "-am", "two")
    git(seed, "push", "origin", "HEAD:main")
    assert wactl.check_update_ready(work) is None


def test_update_refuses_dirty_tree(clone):
    _, _, work = clone
    (work / "a.txt").write_text("dirty")
    msg = wactl.check_update_ready(work)
    assert msg and "uncommitted" in msg


def test_update_refuses_non_fast_forward(clone):
    _, seed, work = clone
    (seed / "a.txt").write_text("remote")
    git(seed, "commit", "-am", "remote change")
    git(seed, "push", "origin", "HEAD:main")
    (work / "b.txt").write_text("local")
    git(work, "add", "b.txt")
    git(work, "commit", "-m", "local change")
    msg = wactl.check_update_ready(work)
    assert msg and "fast-forward" in msg


# ---------------------------------------------------------------- secrets


def test_no_secret_in_doctor_or_status_output(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "whatsapp-bridge").mkdir()
    (repo / "whatsapp-bridge" / "go.mod").write_text("module x\n\ngo 1.21\n")
    (repo / ".env").write_text(f"API_KEY={SECRET}\nWA_MCP_FULL_TOKEN={SECRET}\n")
    ctx = wactl.Context(repo=repo, data=tmp_path / "d", env=wactl.parse_env((repo / ".env").read_text()))
    wactl.cmd_doctor(ctx, argparse_ns())
    wactl.print_env_summary(ctx)
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    assert "API_KEY=<set>" in out.out


def argparse_ns(**kw):
    import argparse

    return argparse.Namespace(**kw)

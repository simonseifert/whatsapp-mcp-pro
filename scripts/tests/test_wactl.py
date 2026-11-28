"""Pure-function tests for wactl. No real services, no network, temp dirs only."""

from __future__ import annotations

import importlib.util
import plistlib
import re
import signal
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
        "--service",
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
    assert "--log" not in tr and tr.endswith('--service"')
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


# ------------------------------------------------------------ fix round 1

import os  # noqa: E402
import sqlite3  # noqa: E402


def make_ctx(tmp_path, env=None):
    repo = tmp_path / "repo"
    (repo / "whatsapp-bridge").mkdir(parents=True, exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return wactl.Context(repo=repo, data=data, env=env or {"API_PORT": "59123"})


def make_store(ctx, paired):
    store = ctx.data / "store"
    store.mkdir(exist_ok=True)
    con = sqlite3.connect(store / "whatsapp.db")
    con.execute("CREATE TABLE whatsmeow_device (jid TEXT)")
    if paired:
        con.execute("INSERT INTO whatsmeow_device VALUES ('x')")
    con.commit()
    con.close()
    con = sqlite3.connect(store / "messages.db")
    con.execute("CREATE TABLE IF NOT EXISTS messages (id TEXT)")
    con.commit()
    con.close()


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


def test_untracked_data_dir_files_do_not_block_update(clone):
    _, _, work = clone
    (work / "logs").mkdir()
    (work / "logs" / "bridge.log").write_text("x")
    (work / "backups").mkdir()
    (work / "backups" / "f").write_text("x")
    assert wactl.check_update_ready(work) is None


def test_gitignore_covers_default_data_dir_contents():
    text = (SCRIPT.parents[1] / ".gitignore").read_text()
    for d in ("run", "logs", "backups"):
        assert f"whatsapp-bridge/{d}/" in text


def test_is_paired(tmp_path):
    ctx = make_ctx(tmp_path)
    assert wactl.is_paired(ctx.store) is False  # no store
    make_store(ctx, paired=False)
    assert wactl.is_paired(ctx.store) is False  # logged out / no device row
    (ctx.store / "whatsapp.db").unlink()
    make_store(ctx, paired=True)
    assert wactl.is_paired(ctx.store) is True


def test_start_guard_refuses_unpaired_with_distinct_code(tmp_path):
    ctx = make_ctx(tmp_path)
    code, msg = wactl.bridge_start_problem(ctx)
    assert code == wactl.EXIT_UNPAIRED and "wactl pair" in msg
    make_store(ctx, paired=True)
    assert wactl.bridge_start_problem(ctx) is None
    # pair itself must not need a paired store
    (ctx.store / "whatsapp.db").unlink()
    assert wactl.bridge_start_problem(ctx, need_paired=False) is None


def test_exit_code_4_in_systemd_restart_prevent_and_supervisor_stops():
    unit = wactl.render_systemd_unit("bridge", ARGV, Path("/d"))
    assert "RestartPreventExitStatus=3 4" in unit and "Restart=on-failure" in unit
    result, sleeps = run_supervisor([(4, 1), (0, 1)])
    assert result == wactl.EXIT_UNPAIRED and sleeps == []


def test_supervisor_rechecks_before_each_restart():
    clock = FakeClock()
    stop = threading.Event()
    codes = iter([1, 1, 1])
    verdicts = iter([None, wactl.EXIT_UNPAIRED])
    spawned = []

    def spawn():
        spawned.append(1)
        return FakeChild(clock, next(codes), 1)

    res = wactl.supervise(
        spawn,
        sleep=lambda s: False,
        clock=clock,
        stop=stop,
        log=lambda m: None,
        before_restart=lambda: next(verdicts),
    )
    assert res == wactl.EXIT_UNPAIRED and len(spawned) == 2


def test_second_bridge_guard_port_and_pid(tmp_path):
    import socket

    ctx = make_ctx(tmp_path)
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        ctx.env["API_PORT"] = str(srv.getsockname()[1])
        assert "already in use" in wactl.bridge_conflict(ctx, as_service=True)
    ctx.env["API_PORT"] = "59124"
    assert wactl.bridge_conflict(ctx, as_service=True) is None
    # a live pid in the pid file (not ourselves) blocks
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (ctx.data / "run").mkdir()
        ctx.pidfile("bridge").write_text(str(other.pid))
        assert "already running" in wactl.bridge_conflict(ctx, as_service=True)
        ctx.pidfile("bridge").write_text(str(os.getpid()))
        assert wactl.bridge_conflict(ctx, as_service=True) is None
    finally:
        other.kill()
        other.wait()


def test_second_bridge_guard_active_service_unless_it_is_the_service(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    monkeypatch.setattr(wactl, "installed_services", lambda c: ["bridge"])
    monkeypatch.setattr(wactl, "service_state", lambda c, n: "active")
    assert "service is running" in wactl.bridge_conflict(ctx, as_service=False)
    assert wactl.bridge_conflict(ctx, as_service=True) is None


def test_parse_tasklist_pid_is_exact():
    out = '"python.exe","12345","Console","1","10,000 K"\n'
    assert wactl.parse_tasklist_pid(out, 12345)
    assert not wactl.parse_tasklist_pid(out, 1234)
    assert not wactl.parse_tasklist_pid("INFO: No tasks are running which match the specified criteria.", 1)


def test_schtasks_tr_within_limit_for_realistic_long_paths():
    launcher = r"C:\Users\Jane Doe\Documents\Projects\whatsapp-mcp-pro\wactl.cmd"
    data = r"C:\Users\Jane Doe\AppData\Local\whatsapp-mcp-pro"
    for c in wactl.COMPONENTS:
        tr = wactl.schtasks_create_args(c, launcher, data)[
            wactl.schtasks_create_args(c, launcher, data).index("/TR") + 1
        ]
        assert len(tr) <= wactl.TR_MAX, len(tr)


# ---- file modes


@posix_only
def test_backup_is_private_and_unique(tmp_path):
    ctx = make_ctx(tmp_path)
    make_store(ctx, paired=True)
    b1 = wactl.backup_store(ctx)
    b2 = wactl.backup_store(ctx)
    assert b1 != b2
    assert stat.S_IMODE(b1.stat().st_mode) == 0o700
    assert stat.S_IMODE((ctx.data / "backups").stat().st_mode) == 0o700
    for name in ("messages.db", "whatsapp.db"):
        assert stat.S_IMODE((b1 / name).stat().st_mode) == 0o600
    assert sqlite3.connect(b1 / "messages.db").execute("select count(*) from messages").fetchone() == (0,)


@posix_only
def test_private_dirs_and_logs(tmp_path):
    d = wactl.ensure_private_dir(tmp_path / "x" / "logs")
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    log = d / "a.log"
    log.write_text("old")
    os.chmod(log, 0o644)
    fh = wactl.open_private_log(log)
    fh.write(b"new")
    fh.close()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert log.read_text() == "oldnew"


def test_plist_has_umask_077():
    data = plistlib.loads(wactl.render_launchd_plist("bridge", ARGV, Path("/d"), "/bin"))
    assert data["Umask"] == 0o077


def test_systemd_dollar_and_percent_escaping():
    argv = ["/u v", "run", "/r/a$b%c/wactl.py"]
    unit = wactl.render_systemd_unit("bridge", argv, Path("/d%1"))
    assert "/r/a$$b%%c/wactl.py" in unit
    assert "WorkingDirectory=/d%%1\n" in unit


def test_macos_install_boots_out_before_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr(wactl, "IS_MAC", True)
    monkeypatch.setattr(wactl, "IS_WINDOWS", False)
    monkeypatch.setattr(wactl, "launchd_dir", lambda: tmp_path / "LA")
    ctx = make_ctx(tmp_path)
    files, cmds = wactl.plan_service_install(ctx, ["bridge"])
    verbs = [c[0][1] for c in cmds]
    assert verbs == ["bootout", "bootstrap"]
    assert cmds[0][1] is False and cmds[1][1] is True


def test_update_build_keeps_dev_extras_with_inexact(tmp_path, monkeypatch):
    monkeypatch.setattr(wactl, "pnpm_cmd", lambda *a: ["pnpm"])
    ctx = make_ctx(tmp_path)
    steps = wactl.build_steps(ctx, minimal=True, bridge_out="whatsapp-bridge.new", inexact=True)
    assert "--inexact" in steps[1][1] and "--inexact" in steps[2][1]
    assert "whatsapp-bridge.new" in steps[0][1]


# ---- update sequence with a fake runner


class FakeOps:
    def __init__(self, ctx, healthy=(True, True), replace_fail_on=None, build_error=None, mcp_active=False):
        self.ctx, self.calls = ctx, []
        self.health = list(healthy)
        self.replace_fail_on = replace_fail_on
        self.build_error = build_error
        self.mcp_active = mcp_active

    def ops(self):
        def build(ctx):
            self.calls.append("build")
            if self.build_error:
                raise wactl.StepError(self.build_error)
            ctx.bridge_bin.with_name(f"whatsapp-bridge.new{wactl.EXE}").write_text("NEW")

        def replace(src, dst):
            self.calls.append(f"replace {src.name}->{dst.name}")
            if self.replace_fail_on and src.name.startswith(self.replace_fail_on):
                raise OSError("denied")
            os.replace(src, dst)

        def healthy(url):
            self.calls.append("health")
            return self.health.pop(0) if self.health else True

        return wactl.UpdateOps(
            build=build,
            stop=lambda c, n: self.calls.append(f"stop {n}"),
            start=lambda c, n: self.calls.append(f"start {n}"),
            healthy=healthy,
            replace=replace,
            is_active=lambda c, n: self.mcp_active and n == "mcp",
        )


def update_ctx(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.bridge_bin.write_text("OLD")
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / ctx.bridge_bin.name).write_text("OLD")
    return ctx, backup


def test_update_success_sequence_swaps_binary_while_stopped(tmp_path):
    ctx, backup = update_ctx(tmp_path)
    fake = FakeOps(ctx, mcp_active=True)
    msgs = []
    assert wactl.apply_update(ctx, fake.ops(), backup, msgs.append) is True
    exe = wactl.EXE
    assert fake.calls == [
        "build",
        "stop bridge",
        f"replace whatsapp-bridge{exe}->whatsapp-bridge.old{exe}",
        f"replace whatsapp-bridge.new{exe}->whatsapp-bridge{exe}",
        "start bridge",
        "health",
        "stop app",
        "start app",
        "health",
        "stop mcp",
        "start mcp",
    ]
    assert ctx.bridge_bin.read_text() == "NEW"


def test_update_rolls_back_binary_when_bridge_unhealthy(tmp_path):
    ctx, backup = update_ctx(tmp_path)
    fake = FakeOps(ctx, healthy=(False, True))
    msgs = []
    assert wactl.apply_update(ctx, fake.ops(), backup, msgs.append) is False
    assert ctx.bridge_bin.read_text() == "OLD"
    text = "\n".join(msgs)
    assert "did not become healthy" in text and "put back and the bridge is healthy again" in text
    assert "restore the backup by hand" in text and "were not changed" not in text


def test_update_reports_failed_rollback_honestly(tmp_path):
    ctx, backup = update_ctx(tmp_path)
    fake = FakeOps(ctx, healthy=(False,))
    ops = fake.ops()
    real = ops.replace

    def flaky(src, dst):
        if src.name.startswith("whatsapp-bridge.old"):
            raise OSError("file in use")
        real(src, dst)

    ops.replace = flaky
    msgs = []
    assert wactl.apply_update(ctx, ops, backup, msgs.append) is False
    text = "\n".join(msgs)
    assert "Could not put the previous bridge binary back" in text and "NOT restored" in text
    assert "healthy again" not in text


def test_update_rollback_reports_still_unhealthy(tmp_path):
    ctx, backup = update_ctx(tmp_path)
    fake = FakeOps(ctx, healthy=(False, False))
    msgs = []
    wactl.apply_update(ctx, fake.ops(), backup, msgs.append)
    assert any("did not become healthy" in m and "put back" in m for m in msgs)


def test_update_swap_failure_restarts_old_bridge(tmp_path):
    ctx, backup = update_ctx(tmp_path)
    fake = FakeOps(ctx, replace_fail_on="whatsapp-bridge.new")
    msgs = []
    assert wactl.apply_update(ctx, fake.ops(), backup, msgs.append) is False
    assert ctx.bridge_bin.read_text() == "OLD"
    assert fake.calls[-1] == "start bridge"


def test_update_build_failure_never_stops_bridge(tmp_path):
    ctx, backup = update_ctx(tmp_path)
    fake = FakeOps(ctx, build_error="go build failed")
    assert wactl.apply_update(ctx, fake.ops(), backup, lambda m: None) is False
    assert fake.calls == ["build"]


# ------------------------------------------------------------ fix round 2


@posix_only
def test_open_private_log_leaves_user_parent_dir_alone(tmp_path):
    parent = tmp_path / "userdir"
    parent.mkdir()
    os.chmod(parent, 0o755)
    wactl.open_private_log(parent / "a.log").close()
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE((parent / "a.log").stat().st_mode) == 0o600


def test_update_unexpected_exception_still_rolls_back_and_restarts_app(tmp_path):
    ctx, backup = update_ctx(tmp_path)
    fake = FakeOps(ctx)
    ops = fake.ops()
    base_start = ops.start
    state = {"n": 0}

    def start(c, n):
        state["n"] += 1
        if n == "app" and state["n"] == 2:
            raise PermissionError("access denied")
        base_start(c, n)

    ops.start = start
    msgs = []
    assert wactl.apply_update(ctx, ops, backup, msgs.append) is False
    assert ctx.bridge_bin.read_text() == "OLD"
    assert "access denied" in "\n".join(msgs)
    assert "app was restarted" in "\n".join(msgs)
    assert fake.calls.count("start bridge") == 2


def test_update_rollback_restarts_app_and_mcp(tmp_path):
    ctx, backup = update_ctx(tmp_path)
    fake = FakeOps(ctx, healthy=(True, False), mcp_active=True)
    msgs = []
    assert wactl.apply_update(ctx, fake.ops(), backup, msgs.append) is False
    text = "\n".join(msgs)
    assert "app was restarted" in text
    assert fake.calls.count("start app") == 2


@posix_only
def test_stop_run_kills_orphaned_child_group(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)

    def sigkill(pid):
        os.kill(pid, signal.SIGKILL)
        return True

    monkeypatch.setattr(wactl, "stop_pid", sigkill)  # a supervisor that had to be SIGKILLed
    (ctx.data / "run").mkdir()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    sup = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ctx.pidfile("app").write_text(str(sup.pid))
        wactl.childfile(ctx, "app").write_text(str(child.pid))
        # reap the processes as they die, as init would for real (non-child) pids
        for proc in (sup, child):
            threading.Thread(target=proc.wait, daemon=True).start()
        wactl.stop_run(ctx, "app", sup.pid)
        sup.wait(timeout=5)
        child.wait(timeout=5)
        assert not ctx.pidfile("app").exists() and not wactl.childfile(ctx, "app").exists()
    finally:
        for p in (child, sup):
            if p.poll() is None:
                p.kill()


def test_run_refuses_second_app_supervisor(tmp_path, monkeypatch, capsys):
    ctx = make_ctx(tmp_path)
    (ctx.data / "run").mkdir()
    (ctx.repo / "wa-app" / "server" / ".venv").mkdir(parents=True)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        ctx.pidfile("app").write_text(str(other.pid))
        ns = argparse_ns(component="app", log=None, service=False)
        assert wactl.cmd_run(ctx, ns) == 1
        assert "already running" in capsys.readouterr().out
    finally:
        other.kill()
        other.wait()


# ------------------------------------------------------------ fix round 3


def test_is_own_child_group_needs_group_and_session_leader(monkeypatch):
    monkeypatch.setattr(wactl.os, "getpgid", lambda p: p, raising=False)
    monkeypatch.setattr(wactl.os, "getsid", lambda p: p, raising=False)
    assert wactl.is_own_child_group(500)
    monkeypatch.setattr(wactl.os, "getsid", lambda p: 1, raising=False)
    assert not wactl.is_own_child_group(500)
    monkeypatch.setattr(wactl.os, "getsid", lambda p: p, raising=False)
    monkeypatch.setattr(wactl.os, "getpgid", lambda p: 1, raising=False)
    assert not wactl.is_own_child_group(500)

    def gone(p):
        raise ProcessLookupError

    monkeypatch.setattr(wactl.os, "getpgid", gone, raising=False)
    assert not wactl.is_own_child_group(500)


def stop_run_fixture(tmp_path, monkeypatch, killed, own_group, child_alive=True):
    monkeypatch.setattr(wactl, "IS_WINDOWS", False)
    ctx = make_ctx(tmp_path)
    (ctx.data / "run").mkdir()
    ctx.pidfile("app").write_text("111")
    wactl.childfile(ctx, "app").write_text("222")
    sent = []
    monkeypatch.setattr(wactl, "stop_pid", lambda pid: killed)
    monkeypatch.setattr(wactl, "pid_alive", lambda pid: child_alive and not sent)
    monkeypatch.setattr(wactl, "is_own_child_group", lambda c: own_group)
    monkeypatch.setattr(wactl.os, "killpg", lambda pg, sig: sent.append((pg, sig)), raising=False)
    monkeypatch.setattr(wactl.time, "sleep", lambda s: None)
    wactl.stop_run(ctx, "app", 111)
    return ctx, sent


def test_stop_run_does_not_killpg_after_clean_supervisor_exit(tmp_path, monkeypatch):
    ctx, sent = stop_run_fixture(tmp_path, monkeypatch, killed=False, own_group=True)
    assert sent == []
    assert not wactl.childfile(ctx, "app").exists()


def test_stop_run_does_not_killpg_a_pid_that_is_not_our_group_leader(tmp_path, monkeypatch):
    _, sent = stop_run_fixture(tmp_path, monkeypatch, killed=True, own_group=False)
    assert sent == []


def test_stop_run_killpgs_verified_child_after_supervisor_sigkill(tmp_path, monkeypatch):
    _, sent = stop_run_fixture(tmp_path, monkeypatch, killed=True, own_group=True)
    assert sent and sent[0] == (222, signal.SIGTERM)


def test_run_clears_stale_child_file_and_unlinks_on_child_exit(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    (ctx.data / "run").mkdir()
    (ctx.repo / "wa-app" / "server" / ".venv").mkdir(parents=True)
    wactl.childfile(ctx, "app").write_text("999")
    seen = {}

    class P:
        pid = 4242
        returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            seen["during"] = wactl.childfile(ctx, "app").read_text()
            return 0

    monkeypatch.setattr(wactl, "popen_component", lambda *a, **k: P())
    monkeypatch.setattr(wactl.signal, "signal", lambda *a: None)
    ns = argparse_ns(component="app", log=None, service=False)
    assert wactl.cmd_run(ctx, ns) == 0
    assert seen["during"] == "4242"  # the stale 999 was replaced by the live child
    assert not wactl.childfile(ctx, "app").exists()  # removed as soon as the child exited


def test_pnpm_pin_reads_package_manager(tmp_path):
    web = tmp_path / "wa-app" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text('{"packageManager": "pnpm@11.22.0+sha512.abc"}')
    assert wactl.pnpm_pin(tmp_path) == "11.22.0"
    (web / "package.json").write_text("{}")
    assert wactl.pnpm_pin(tmp_path) is None


def _pnpm_env(monkeypatch, tmp_path, works, windows=False):
    """works: set of command strings that succeed; npm install adds 'pnpm --version' via the prefix path."""
    web = tmp_path / "wa-app" / "web"
    web.mkdir(parents=True, exist_ok=True)
    (web / "package.json").write_text('{"packageManager": "pnpm@11.22.0"}')
    prefix = tmp_path / "npmprefix"
    prefix.mkdir(exist_ok=True)
    ran = []

    def fake_runs_ok(cmd, cwd=None):
        key = " ".join(cmd) if not cmd[0].startswith(str(prefix)) else "global-pnpm --version"
        ran.append(key)
        return key in works

    def fake_run(cmd, **kw):
        ran.append(" ".join(cmd))
        out = str(prefix) if cmd[1:3] == ["prefix", "-g"] else ""
        if cmd[1] == "install":
            name = "pnpm.cmd" if windows else "bin/pnpm"
            f = prefix / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("")
            works.add("global-pnpm --version")
        return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()

    monkeypatch.setattr(wactl, "runs_ok", fake_runs_ok)
    monkeypatch.setattr(wactl, "IS_WINDOWS", windows)
    monkeypatch.setattr(wactl.shutil, "which", lambda n: f"/x/{n}")
    monkeypatch.setattr(wactl.subprocess, "run", fake_run)
    return ran, prefix


def test_pnpm_cmd_uses_working_pnpm(monkeypatch, tmp_path):
    _pnpm_env(monkeypatch, tmp_path, {"pnpm --version"})
    assert wactl.pnpm_cmd(tmp_path) == ["pnpm"]


def test_pnpm_cmd_skips_broken_pnpm_for_corepack(monkeypatch, tmp_path):
    _pnpm_env(monkeypatch, tmp_path, {"corepack --version", "corepack pnpm --version"})
    assert wactl.pnpm_cmd(tmp_path) == ["corepack", "pnpm"]


def test_pnpm_cmd_falls_back_to_npm_when_both_broken(monkeypatch, tmp_path, capsys):
    ran, prefix = _pnpm_env(monkeypatch, tmp_path, set())
    cmd = wactl.pnpm_cmd(tmp_path)
    assert cmd == [str(prefix / "bin" / "pnpm")]
    assert any(r.endswith("install -g pnpm@11.22.0") for r in ran)
    assert "installing pnpm@11.22.0" in capsys.readouterr().out


def test_pnpm_cmd_windows_uses_npm_prefix_cmd(monkeypatch, tmp_path):
    _, prefix = _pnpm_env(monkeypatch, tmp_path, set(), windows=True)
    assert wactl.pnpm_cmd(tmp_path) == [str(prefix / "pnpm.cmd")]

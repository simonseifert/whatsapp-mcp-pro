# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""wactl: install, pair, run, supervise, keep running and update whatsapp-mcp-pro.

One file, Python standard library only, for Linux, macOS and Windows.
Run it through the launcher (`./wactl` or `wactl.cmd`), which uses uv.

Sections: env, doctor, setup, run and supervise, process control, services
per OS, update, cli.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import platform
import plistlib
import re
import secrets
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
EXE = ".exe" if IS_WINDOWS else ""
COMPONENTS = ("bridge", "app", "mcp")
DEFAULT_COMPONENTS = ("bridge", "app")
MARKER = "# Managed by wactl. Do not edit; run `wactl service install` to regenerate."
EXIT_STREAM_REPLACED = 3
EXIT_UNPAIRED = 4
STOP_CODES = (EXIT_STREAM_REPLACED, EXIT_UNPAIRED)
TR_MAX = 261  # schtasks /TR limit
# Windows-only constants, looked up by name so POSIX type checkers stay quiet.
WIN_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WIN_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)
WIN_CTRL_BREAK = getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT)
BACKOFF_START, BACKOFF_MAX, BACKOFF_RESET_AFTER = 2, 60, 600
PLACEHOLDER_KEYS = ("", "CHANGEME")


# ====================================================================== env


def parse_env(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines. Handles comments, blank lines, `export`, quotes."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if value[:1] in ("'", '"') and value.count(value[0]) >= 2:
            value = value[1 : value.index(value[0], 1)]
        else:
            value = re.sub(r"\s+#.*$", "", value)
        out[key] = value
    return out


def set_env_value(text: str, key: str, value: str) -> str:
    """Set KEY in env text: replace the active line, else a `# KEY=` line, else append."""
    lines = text.splitlines()
    for pattern in (rf"\s*(export\s+)?{re.escape(key)}\s*=", rf"#\s*{re.escape(key)}\s*="):
        for i, line in enumerate(lines):
            if re.match(pattern, line):
                lines[i] = f"{key}={value}"
                return "\n".join(lines) + "\n"
    return "\n".join([*lines, f"{key}={value}"]) + "\n"


def generate_api_key() -> str:
    return secrets.token_hex(32)


def write_private(path: Path, data: bytes | str) -> None:
    """Write a file readable by the owner only (mode 600 on POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode() if isinstance(data, str) else data
    if IS_WINDOWS:
        path.write_bytes(raw)
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
    os.chmod(path, 0o600)


def ensure_private_dir(path: Path) -> Path:
    """Create a directory only the owner can enter (mode 700 on POSIX)."""
    path.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:
        os.chmod(path, 0o700)
    return path


def open_private_log(path: Path):
    """Open a log for appending, created and kept at mode 600 on POSIX.

    The parent directory is created if missing but its mode is left alone: it may be one the user
    chose with --log. Callers that own the directory call ensure_private_dir first.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    if not IS_WINDOWS:
        os.chmod(path, 0o600)
    return os.fdopen(fd, "ab", buffering=0)


def ensure_env(env_path: Path, example_path: Path) -> bool:
    """Create .env from .env.example with a random API_KEY. Never overwrites. True if created."""
    if env_path.exists():
        return False
    text = set_env_value(example_path.read_text(), "API_KEY", generate_api_key())
    write_private(env_path, text)
    return True


def resolve_data_dir(arg: str | None, env: dict[str, str], repo: Path) -> Path:
    """--data beats WA_DATA_DIR beats the legacy default whatsapp-bridge/."""
    raw = arg or env.get("WA_DATA_DIR") or os.environ.get("WA_DATA_DIR")
    if not raw:
        return repo / "whatsapp-bridge"
    p = Path(raw).expanduser()
    return p if p.is_absolute() else repo / p


def load_env(repo: Path, data: Path) -> dict[str, str]:
    """Repo .env, then <data>/.env on top. Real environment variables win over both."""
    merged: dict[str, str] = {}
    for p in (repo / ".env", data / ".env"):
        if p.is_file():
            merged.update(parse_env(p.read_text(errors="replace")))
    return merged


def component_env(component: str, repo: Path, data: Path, env: dict[str, str]) -> dict[str, str]:
    """Environment for one component: process env over .env, plus wactl's defaults."""
    out = {**env, **tool_env()}
    if component != "bridge":
        out.setdefault("WA_STORE_PATH", str(data / "store"))
    if component in ("app", "mcp") and data != repo / "whatsapp-bridge":
        out.setdefault("WA_APP_DATA", str(data / "wa-app"))
    if component == "mcp":
        out.setdefault("BRIDGE_HOST", f"127.0.0.1:{out.get('API_PORT', '8080')}")
        out.setdefault("MCP_HOST", "127.0.0.1")
        if sys.platform.startswith("linux"):
            out.setdefault("MALLOC_ARENA_MAX", "2")
    return out


def bridge_port(env: dict[str, str]) -> int:
    return int(os.environ.get("API_PORT") or env.get("API_PORT") or 8080)


def app_port(env: dict[str, str]) -> int:
    return int(os.environ.get("WA_APP_PORT") or env.get("WA_APP_PORT") or 8085)


@dataclass
class Context:
    repo: Path
    data: Path
    env: dict[str, str] = field(default_factory=dict)
    yes: bool = False

    @property
    def script(self) -> Path:
        return self.repo / "scripts" / "wactl.py"

    @property
    def launcher(self) -> Path:
        return self.repo / ("wactl.cmd" if IS_WINDOWS else "wactl")

    @property
    def bridge_bin(self) -> Path:
        return self.repo / "whatsapp-bridge" / f"whatsapp-bridge{EXE}"

    def logs(self) -> Path:
        return self.data / "logs"

    def pidfile(self, c: str) -> Path:
        return self.data / "run" / f"{c}.pid"

    @property
    def store(self) -> Path:
        return self.data / "store"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def print_env_summary(ctx: Context) -> None:
    """Show which secrets are configured, never their values."""
    for key in ("API_KEY", "WA_MCP_FULL_TOKEN", "WA_MCP_READONLY_TOKEN", "WA_APP_AGENT_TOKEN"):
        val = ctx.env.get(key, "")
        state = "<set>" if val and not val.startswith("CHANGEME") else "<missing>"
        if key == "API_KEY" or val:
            say(f"  {key}={state}")


def can_prompt(yes: bool) -> bool:
    return sys.stdin.isatty() and not yes


# ==================================================================== doctor


def install_hint(tool: str, system: str) -> str:
    """Exact install command for a missing tool on this OS."""
    table = {
        "Linux": {
            "uv": "curl -LsSf https://astral.sh/uv/install.sh | sh   (or: pipx install uv)",
            "go": "sudo apt install golang-go   (or download from https://go.dev/dl/)",
            "cc": "sudo apt install build-essential",
            "node": "sudo apt install nodejs npm   (Node 20 or newer: https://nodejs.org)",
            "pnpm": "corepack enable   (ships with Node), or: npm install -g pnpm   (apt has no pnpm)",
            "ffmpeg": "sudo apt install ffmpeg",
            "tailscale": "curl -fsSL https://tailscale.com/install.sh | sh   (apt repo: https://tailscale.com/download/linux)",
        },
        "Darwin": {
            "uv": "brew install uv",
            "go": "brew install go",
            "cc": "xcode-select --install",
            "node": "brew install node",
            "pnpm": "brew install pnpm   (or: corepack enable)",
            "ffmpeg": "brew install ffmpeg",
            "tailscale": "brew install tailscale",
        },
        "Windows": {
            "uv": "winget install --id astral-sh.uv",
            "go": "winget install --id GoLang.Go",
            "cc": "winget install --id BrechtSanders.WinLibs.POSIX.UCRT   (gcc; reopen the terminal)",
            "node": "winget install --id OpenJS.NodeJS.LTS",
            "pnpm": "corepack enable   (ships with Node), or: winget install --id pnpm.pnpm",
            "ffmpeg": "winget install --id Gyan.FFmpeg",
            "tailscale": "winget install --id Tailscale.Tailscale",
        },
    }
    return table.get(system, table["Linux"]).get(tool, f"install {tool}")


def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(x) for x in m.groups() if x is not None) if m else None


def go_required_version(gomod: str) -> tuple[int, int] | None:
    m = re.search(r"^go\s+(\d+)\.(\d+)", gomod, re.M)
    return (int(m.group(1)), int(m.group(2))) if m else None


def judge_go(have: tuple[int, ...], need: tuple[int, int], toolchain_env: str) -> tuple[bool, str]:
    if have[:2] >= need:
        return True, ""
    if have[:2] >= (1, 21) and toolchain_env != "local":
        return (True, "older than go.mod asks for; Go will download the right toolchain on first build")
    return False, f"go.mod needs Go {need[0]}.{need[1]} or newer"


def tool_output(cmd: list[str], timeout: int = 15) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (r.stdout + r.stderr).strip()


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def http_ok(url: str, timeout: float = 3) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (fixed loopback URL)
            return 200 <= r.status < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def cmd_doctor(ctx: Context, args: argparse.Namespace) -> int:
    system = platform.system()
    failed = 0

    def row(ok: bool, name: str, detail: str, required: bool = True, hint: str | None = None) -> None:
        nonlocal failed
        tag = "ok  " if ok else ("FAIL" if required else "warn")
        say(f"[{tag}] {name}: {detail}")
        if not ok:
            if required:
                failed += 1
            if hint:
                say(f"        fix: {hint}")

    say(f"wactl doctor on {system} {platform.release()} ({platform.machine()})")
    say(f"repo: {ctx.repo}\ndata: {ctx.data}\n")
    py_ok = sys.version_info >= (3, 11)
    row(py_ok, "Python", platform.python_version(), hint="wactl needs Python 3.11+; uv fetches it for you")
    uv = shutil.which("uv")
    row(bool(uv), "uv", uv or "not found", hint=install_hint("uv", system))

    go = shutil.which("go")
    gomod = ctx.repo / "whatsapp-bridge" / "go.mod"
    need = go_required_version(gomod.read_text()) if gomod.is_file() else None
    if go:
        have = parse_version(tool_output([go, "version"]))
        if have and need:
            ok, note = judge_go(have, need, os.environ.get("GOTOOLCHAIN", ""))
            row(
                ok,
                "Go",
                ".".join(map(str, have)) + (f" ({note})" if note else ""),
                hint=install_hint("go", system),
            )
        else:
            row(True, "Go", go)
    else:
        row(False, "Go", "not found", hint=install_hint("go", system))

    if IS_WINDOWS:
        cc = shutil.which("gcc")
        row(bool(cc), "C compiler (cgo, gcc)", cc or "gcc not on PATH", hint=install_hint("cc", system))
    elif IS_MAC:
        out = tool_output(["xcode-select", "-p"])
        row(
            bool(out) and os.path.isdir(out),
            "C compiler (Xcode tools)",
            out or "not installed",
            hint=install_hint("cc", system),
        )
    else:
        cc = shutil.which("gcc") or shutil.which("cc") or shutil.which("clang")
        row(bool(cc), "C compiler (cgo)", cc or "not found", hint=install_hint("cc", system))

    node = shutil.which("node")
    nv = parse_version(tool_output([node, "-v"])) if node else None
    row(
        bool(nv and nv[0] >= 20),
        "Node",
        ".".join(map(str, nv)) if nv else "not found",
        hint=install_hint("node", system),
    )
    pnpm = shutil.which("pnpm")
    corepack = shutil.which("corepack")
    row(
        bool(pnpm or corepack),
        "pnpm",
        pnpm or (f"via corepack ({corepack})" if corepack else "not found"),
        hint=install_hint("pnpm", system),
    )
    row(
        bool(shutil.which("ffmpeg")),
        "ffmpeg",
        shutil.which("ffmpeg") or "not found (voice notes only)",
        required=False,
        hint=install_hint("ffmpeg", system),
    )
    row(
        bool(shutil.which("tailscale")),
        "tailscale",
        shutil.which("tailscale") or "not found (remote access only)",
        required=False,
        hint=install_hint("tailscale", system),
    )

    env_file = ctx.repo / ".env"
    key = ctx.env.get("API_KEY", "")
    key_ok = env_file.is_file() and key not in PLACEHOLDER_KEYS and not key.startswith("CHANGEME")
    row(
        key_ok,
        ".env",
        "API_KEY=<set>" if key_ok else ("API_KEY=<missing>" if env_file.is_file() else "not found"),
        hint="run `wactl setup`",
    )

    for name, port, url in (
        ("bridge port", bridge_port(ctx.env), f"http://127.0.0.1:{bridge_port(ctx.env)}/api/health"),
        ("app port", app_port(ctx.env), f"http://127.0.0.1:{app_port(ctx.env)}/api/health"),
    ):
        if not port_in_use(port):
            row(True, name, f"{port} free")
        elif http_ok(url):
            row(True, name, f"{port} in use by a running component")
        else:
            var = "API_PORT" if name.startswith("bridge") else "WA_APP_PORT"
            row(False, name, f"{port} is in use by something else", hint=f"set {var} in .env to a free port")
    say("")
    say("doctor: all required checks passed" if not failed else f"doctor: {failed} required check(s) failed")
    return 1 if failed else 0


# ===================================================================== setup


class StepError(RuntimeError):
    pass


def resolve_exe(name: str) -> str:
    return shutil.which(name) or name


def tool_env() -> dict[str, str]:
    """Environment for build tools. uv sets VIRTUAL_ENV for wactl itself; child uv calls must not see it."""
    return {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}


def run_step(label: str, cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    say(f"==> {label}")
    say(f"    {' '.join(shlex.quote(c) for c in cmd)}   (in {cwd})")
    cmd = [resolve_exe(cmd[0]), *cmd[1:]]
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env or tool_env())
    except OSError as exc:
        raise StepError(f"{label} failed: {exc}") from exc
    if r.returncode != 0:
        raise StepError(f"{label} failed (exit {r.returncode})")


def pnpm_cmd() -> list[str]:
    if shutil.which("pnpm"):
        return ["pnpm"]
    if shutil.which("corepack"):
        with contextlib.suppress(OSError):
            subprocess.run([resolve_exe("corepack"), "enable"], capture_output=True)
        if shutil.which("pnpm"):
            return ["pnpm"]
        return ["corepack", "pnpm"]
    raise StepError("pnpm and corepack are both missing. " + install_hint("pnpm", platform.system()))


def mcp_extra() -> str:
    return "pro" if IS_MAC and platform.machine() == "arm64" else "pro-cpu"


def build_steps(
    ctx: Context, minimal: bool, bridge_out: str | None = None, inexact: bool = False
) -> list[tuple[str, list[str], Path]]:
    uv = "uv"
    extra_flag = ["--inexact"] if inexact else []
    mcp = [uv, "sync", *extra_flag, "--project", str(ctx.repo / "whatsapp-mcp-server")]
    if not minimal:
        mcp += ["--extra", mcp_extra()]
    pm = pnpm_cmd()
    web = ctx.repo / "wa-app" / "web"
    return [
        (
            "Build the bridge",
            ["go", "build", "-o", bridge_out or f"whatsapp-bridge{EXE}", "."],
            ctx.repo / "whatsapp-bridge",
        ),
        ("Install MCP server dependencies" + ("" if minimal else f" (extra {mcp_extra()})"), mcp, ctx.repo),
        (
            "Install wa-app server dependencies",
            [uv, "sync", *extra_flag, "--project", str(ctx.repo / "wa-app" / "server")],
            ctx.repo,
        ),
        ("Install web dependencies", [*pm, "install", "--frozen-lockfile"], web),
        ("Build the web app", [*pm, "build"], web),
    ]


def cmd_setup(ctx: Context, args: argparse.Namespace) -> int:
    example, env_file = ctx.repo / ".env.example", ctx.repo / ".env"
    if ensure_env(env_file, example):
        say(f"Created {env_file} with a new random API_KEY (mode 600). Its value is never printed.")
    else:
        say(f"Kept your existing {env_file}.")
    ctx.env = load_env(ctx.repo, ctx.data)
    if not ctx.env.get("WA_APP_PUSH_SUBJECT"):
        if can_prompt(args.yes):
            answer = input(
                "Contact for Web Push notifications (mailto:you@example.com or an https URL), Enter to skip: "
            ).strip()
            if answer and re.match(r"(mailto:|https://)", answer):
                env_file.write_text(set_env_value(env_file.read_text(), "WA_APP_PUSH_SUBJECT", answer))
            elif answer:
                say("Not saved: it must start with mailto: or https://")
        else:
            say(
                "Push notifications are off. To turn them on later, set WA_APP_PUSH_SUBJECT=mailto:you@example.com in .env."  # noqa: E501
            )
    ctx.data.mkdir(parents=True, exist_ok=True)
    try:
        for label, cmd, cwd in build_steps(ctx, minimal=args.minimal):
            run_step(label, cmd, cwd)
    except StepError as exc:
        say(f"\nsetup stopped: {exc}\nRun `wactl doctor` to see what is missing.")
        return 1
    say(f"\nSetup done. Data directory: {ctx.data}")
    say("Next: run `wactl pair` and scan the QR code with WhatsApp on your phone.")
    return 0


# ============================================================ run and supervise


def next_delay(prev: float | None, uptime: float) -> float:
    """Backoff 2s doubling to 60s; resets to 2s once the child stayed up 10 minutes."""
    if prev is None or uptime >= BACKOFF_RESET_AFTER:
        return BACKOFF_START
    return min(prev * 2, BACKOFF_MAX)


def supervise(
    spawn: Callable[[], object],
    *,
    sleep: Callable[[float], bool],
    clock: Callable[[], float],
    stop: threading.Event,
    log: Callable[[str], None] = say,
    before_restart: Callable[[], int | None] | None = None,
) -> int:
    """Run the child until it exits 0, exits 3 or 4, or a stop is requested; restart on other exits.

    `before_restart()` runs before every restart; a non-None result stops the loop with that code.

    `sleep(seconds)` returns True when a stop was requested during the wait.
    """
    delay: float | None = None
    while True:
        started = clock()
        child = spawn()
        code = child.wait()  # type: ignore[attr-defined]
        uptime = clock() - started
        if stop.is_set():
            return 0
        if code == EXIT_STREAM_REPLACED:
            log(
                "wactl: exit code 3 (stream replaced): another process took this WhatsApp session. "
                "Not restarting; restarting would start a ping-pong. Stop the other client, then start again."
            )
            return EXIT_STREAM_REPLACED
        if code == EXIT_UNPAIRED:
            log("wactl: the bridge reports no linked device. Not restarting. Run `wactl pair`.")
            return EXIT_UNPAIRED
        if code == 0:
            log("wactl: component exited cleanly; not restarting")
            return 0
        delay = next_delay(delay, uptime)
        log(f"wactl: component exited with code {code} after {int(uptime)}s; restarting in {int(delay)}s")
        if sleep(delay):
            return 0
        if before_restart:
            verdict = before_restart()
            if verdict is not None:
                return verdict


def component_command(ctx: Context, component: str) -> list[str]:
    if component == "bridge":
        return [str(ctx.bridge_bin)]
    if component == "app":
        return ["uv", "run", "--no-sync", "--project", str(ctx.repo / "wa-app" / "server"), "wa-app"]
    return [
        "uv",
        "run",
        "--no-sync",
        "--project",
        str(ctx.repo / "whatsapp-mcp-server"),
        "python",
        str(ctx.repo / "whatsapp-mcp-server" / "serve_http.py"),
    ]


def check_runnable(ctx: Context, component: str) -> str | None:
    if component == "bridge" and not ctx.bridge_bin.is_file():
        return f"{ctx.bridge_bin} not found. Run `wactl setup` first."
    if component != "bridge":
        proj = "wa-app/server" if component == "app" else "whatsapp-mcp-server"
        if not (ctx.repo / proj / ".venv").is_dir():
            return f"{proj} is not installed. Run `wactl setup` first."
    if component == "bridge" and not ctx.env.get("API_KEY") and not os.environ.get("API_KEY"):
        return "API_KEY is missing. Run `wactl setup` first."
    return None


def is_paired(store: Path) -> bool:
    """True if whatsapp.db holds a linked device (checked read-only)."""
    db = store / "whatsapp.db"
    if not db.is_file():
        return False
    try:
        with contextlib.closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            return con.execute("SELECT 1 FROM whatsmeow_device LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


UNPAIRED_MSG = "this data directory has no linked WhatsApp device ({store}). Run `wactl pair` first."


def bridge_conflict(ctx: Context, as_service: bool) -> str | None:
    """Why a second bridge must not start on this store, or None."""
    port = bridge_port(ctx.env)
    if port_in_use(port):
        return f"port {port} is already in use (a bridge may be running). Stop it first: `wactl stop bridge`."
    pid = read_pid(ctx, "bridge")
    if pid and pid != os.getpid():
        return f"a bridge is already running for this data directory (pid {pid}). Stop it first."
    if (
        not as_service
        and "bridge" in installed_services(ctx)
        and service_state(ctx, "bridge") in ("active", "running")
    ):
        return "the bridge service is running. Stop it first with `wactl stop bridge`."
    return None


def bridge_start_problem(
    ctx: Context, as_service: bool = False, need_paired: bool = True
) -> tuple[int, str] | None:
    """(exit code, message) if the bridge must not be started now."""
    if need_paired and not is_paired(ctx.store):
        return EXIT_UNPAIRED, UNPAIRED_MSG.format(store=ctx.store)
    conflict = bridge_conflict(ctx, as_service)
    if conflict:
        return 1, conflict
    return None


def popen_component(
    ctx: Context, component: str, log_file=None, new_session: bool = False
) -> subprocess.Popen:
    cmd = component_command(ctx, component)
    cmd[0] = resolve_exe(cmd[0])
    ctx.data.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {"cwd": ctx.data, "env": component_env(component, ctx.repo, ctx.data, ctx.env)}
    if log_file is not None:
        kwargs.update(stdout=log_file, stderr=subprocess.STDOUT)
    if IS_WINDOWS:
        kwargs["creationflags"] = WIN_NEW_GROUP | (WIN_NO_WINDOW if log_file is not None else 0)
    elif new_session:
        # Own session: a terminal Ctrl-C reaches wactl only, which forwards it once.
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def wait_port_free(port: int, stop: threading.Event, seconds: int = 30) -> None:
    for _ in range(seconds):
        if not port_in_use(port) or stop.is_set():
            return
        say(f"wactl: port {port} still held by the previous instance, waiting")
        stop.wait(1)


def cmd_run(ctx: Context, args: argparse.Namespace) -> int:
    component = args.component
    problem = check_runnable(ctx, component)
    if problem:
        say(f"wactl: {problem}")
        return 1
    if component == "bridge":
        if args.service:
            wait_port_free(bridge_port(ctx.env), threading.Event(), 10)
        guard = bridge_start_problem(ctx, as_service=args.service)
        if guard:
            say(f"wactl: refusing to start the bridge: {guard[1]}")
            return guard[0]
    # A scheduled task's hidden console can report a TTY, so --service always logs to the file.
    default_log = IS_WINDOWS and (args.service or not sys.stdout.isatty())
    log_path = args.log or (str(ctx.logs() / f"{component}.log") if default_log else None)
    if not args.log:
        ensure_private_dir(ctx.logs())
    ensure_private_dir(ctx.pidfile(component).parent)
    other = read_pid(ctx, component)
    if other and other != os.getpid():
        say(f"wactl: {component} is already running for this data directory (pid {other}).")
        return 1
    childfile(ctx, component).unlink(missing_ok=True)  # stale from a crashed earlier supervisor
    log_file = open_private_log(Path(log_path)) if log_path else None
    ctx.pidfile(component).write_text(str(os.getpid()))
    stop = threading.Event()
    current: dict[str, subprocess.Popen | None] = {"p": None}

    def forward(signum: int, _frame=None) -> None:
        stop.set()
        p = current["p"]
        if p and p.poll() is None:
            try:
                if IS_WINDOWS:
                    p.send_signal(WIN_CTRL_BREAK)
                else:
                    p.send_signal(signal.SIGTERM if signum == signal.SIGHUP else signum)
            except (OSError, ValueError):
                p.terminate()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, forward)

    class Child:
        def __init__(self) -> None:
            self.p = popen_component(ctx, component, log_file, new_session=True)
            current["p"] = self.p
            ensure_private_dir(childfile(ctx, component).parent)
            childfile(ctx, component).write_text(str(self.p.pid))

        def wait(self) -> int:
            while True:
                try:
                    code = self.p.wait(timeout=1 if not stop.is_set() else 20)
                except subprocess.TimeoutExpired:
                    if stop.is_set():
                        self.p.kill()
                    continue
                # The pid is free for reuse now; never leave it recorded during restart backoff.
                childfile(ctx, component).unlink(missing_ok=True)
                return code

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        if log_file:
            log_file.write((line + "\n").encode())
        else:
            say(line)

    def before_restart() -> int | None:
        wait_port_free(bridge_port(ctx.env), stop)
        if not is_paired(ctx.store):
            log("wactl: " + UNPAIRED_MSG.format(store=ctx.store) + " Not restarting.")
            return EXIT_UNPAIRED
        return None

    log(f"wactl: running {component} in {ctx.data}")
    try:
        return supervise(
            Child,
            sleep=stop.wait,
            clock=time.monotonic,
            stop=stop,
            log=log,
            before_restart=before_restart if component == "bridge" else None,
        )
    finally:
        with contextlib.suppress(OSError, ValueError):
            if ctx.pidfile(component).read_text().strip() == str(os.getpid()):
                ctx.pidfile(component).unlink()
                childfile(ctx, component).unlink(missing_ok=True)
        if log_file:
            log_file.close()


def cmd_pair(ctx: Context, args: argparse.Namespace) -> int:
    problem = check_runnable(ctx, "bridge")
    if problem:
        say(f"wactl: {problem}")
        return 1
    guard = bridge_start_problem(ctx, need_paired=False)
    if guard:
        say(f"wactl: refusing to pair: {guard[1]}")
        return guard[0]
    say(f"Starting the bridge in the foreground in {ctx.data}.")
    say("Scan the QR code with WhatsApp: Settings, Linked devices, Link a device.")
    say("When the bridge says it is connected, press Ctrl-C, then run `wactl service install`.\n")
    ctx.data.mkdir(parents=True, exist_ok=True)
    p = popen_component(ctx, "bridge")
    try:
        code = p.wait()
    except KeyboardInterrupt:
        # On POSIX the terminal already sent Ctrl-C to the bridge; do not send it twice.
        if IS_WINDOWS:
            with contextlib.suppress(OSError):
                p.send_signal(WIN_CTRL_BREAK)
        code = p.wait()
    say("\nBridge stopped. Next: `wactl service install` to keep it running, or `wactl start`.")
    return 0 if code in (0, 130, -signal.SIGINT) else code


# ============================================================== process control


def parse_tasklist_pid(output: str, pid: int) -> bool:
    """True if a `tasklist /FO CSV /NH` row has exactly this PID."""
    for line in output.splitlines():
        fields = [f.strip('"') for f in line.strip().split('","')]
        if len(fields) > 1 and fields[1].strip('"') == str(pid):
            return True
    return False


def pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        return parse_tasklist_pid(tool_output(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"]), pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid(ctx: Context, c: str) -> int | None:
    try:
        pid = int(ctx.pidfile(c).read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def spawn_detached(ctx: Context, c: str) -> int:
    ensure_private_dir(ctx.logs())
    ensure_private_dir(ctx.pidfile(c).parent)
    cmd = [
        sys.executable,
        str(ctx.script),
        "run",
        c,
        "--data",
        str(ctx.data),
        "--log",
        str(ctx.logs() / f"{c}.log"),
    ]
    kw: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": ctx.repo,
    }
    if IS_WINDOWS:
        kw["creationflags"] = WIN_DETACHED | WIN_NEW_GROUP | WIN_NO_WINDOW
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen(cmd, **kw)
    return p.pid


def childfile(ctx: Context, c: str) -> Path:
    """Where `run` records its child's pid (the child lives in its own session on POSIX)."""
    return ctx.data / "run" / f"{c}.child"


def stop_pid(pid: int) -> bool:
    """Stop a process. Returns True if it had to be SIGKILLed (POSIX), i.e. it could not clean up."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return False
    os.kill(pid, signal.SIGTERM)
    for _ in range(60):  # longer than the supervisor's own 20 s wait for its child
        if not pid_alive(pid):
            return False
        time.sleep(0.5)
    os.kill(pid, signal.SIGKILL)
    return True


def is_own_child_group(child: int) -> bool:
    """True if `child` is still a session and group leader, as `run` starts its children.

    A pid reused by an unrelated process is almost never both; this is a guard, not proof.
    """
    try:
        return os.getpgid(child) == child and os.getsid(child) == child
    except OSError:
        return False


def stop_run(ctx: Context, c: str, pid: int) -> None:
    """Stop a `wactl run` supervisor and make sure its child is gone too."""
    child: int | None = None
    with contextlib.suppress(OSError, ValueError):
        child = int(childfile(ctx, c).read_text().strip())
    killed = stop_pid(pid)
    # Only a SIGKILLed supervisor can leave its child behind (a clean exit stops the child first),
    # and only a pid that still looks like our child (own session and group) may be signalled.
    if killed and child and not IS_WINDOWS and pid_alive(child) and is_own_child_group(child):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(OSError):
                os.killpg(child, sig)
            for _ in range(20):
                if not pid_alive(child):
                    break
                time.sleep(0.25)
    ctx.pidfile(c).unlink(missing_ok=True)
    childfile(ctx, c).unlink(missing_ok=True)


def targets(args: argparse.Namespace, ctx: Context) -> list[str]:
    if getattr(args, "component", None):
        return [args.component]
    wanted: list[str] = list(DEFAULT_COMPONENTS)
    if "mcp" in installed_services(ctx) or read_pid(ctx, "mcp"):
        wanted.append("mcp")
    return wanted


def cmd_control(action: str) -> Callable[[Context, argparse.Namespace], int]:
    def run(ctx: Context, args: argparse.Namespace) -> int:
        rc = 0
        svc = installed_services(ctx)
        for c in targets(args, ctx):
            if c in svc:
                rc |= service_control(ctx, action, c)
                continue
            pid = read_pid(ctx, c)
            if action in ("stop", "restart") and pid:
                stop_run(ctx, c, pid)
                say(f"{c}: stopped (pid {pid})")
            elif action == "stop":
                say(f"{c}: not running")
            if action in ("start", "restart"):
                if action == "start" and pid:
                    say(f"{c}: already running (pid {pid})")
                    continue
                problem = check_runnable(ctx, c)
                if problem:
                    say(f"{c}: {problem}")
                    rc = 1
                    continue
                if c == "bridge":
                    if action == "restart":
                        wait_port_free(bridge_port(ctx.env), threading.Event(), 15)
                    guard = bridge_start_problem(ctx, as_service=True)
                    if guard:
                        say(f"bridge: not started: {guard[1]}")
                        rc = 1
                        continue
                say(f"{c}: started (pid {spawn_detached(ctx, c)}), log {ctx.logs() / (c + '.log')}")
        return rc

    return run


def component_health(ctx: Context, c: str) -> str:
    if c == "bridge":
        return "healthy" if http_ok(f"http://127.0.0.1:{bridge_port(ctx.env)}/api/health") else "no answer"
    if c == "app":
        return "healthy" if http_ok(f"http://127.0.0.1:{app_port(ctx.env)}/api/health") else "no answer"
    host, port = "127.0.0.1", ctx.env.get("MCP_PORT", "8082")
    return "listening" if port_in_use(int(port), host) else "not listening"


def cmd_status(ctx: Context, args: argparse.Namespace) -> int:
    say(f"data: {ctx.data}")
    print_env_summary(ctx)
    svc = installed_services(ctx)
    comps = [args.component] if getattr(args, "component", None) else list(COMPONENTS)
    for c in comps:
        if c in svc:
            state = f"service {service_state(ctx, c)}"
        else:
            pid = read_pid(ctx, c)
            state = f"pid {pid}" if pid else "not running"
        health = component_health(ctx, c)
        if c == "mcp" and state == "not running" and health == "not listening":
            continue
        say(f"{c:7} {state:22} {health}")
    return 0


def cmd_logs(ctx: Context, args: argparse.Namespace) -> int:
    c = args.component or "bridge"
    if c in installed_services(ctx) and sys.platform.startswith("linux"):
        cmd = ["journalctl", "--user", "-u", f"wa-{c}", "-n", str(args.lines), "--no-pager"]
        if args.follow:
            cmd.append("-f")
        return subprocess.call(cmd)
    path = ctx.logs() / f"{c}.log"
    if not path.is_file():
        say(f"No log file at {path}. Components started with `wactl run` in a terminal print there instead.")
        return 1
    lines = path.read_text(errors="replace").splitlines()[-args.lines :]
    say("\n".join(lines))
    if args.follow:
        with open(path, errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            try:
                while True:
                    chunk = fh.read()
                    if chunk:
                        print(chunk, end="", flush=True)
                    else:
                        time.sleep(0.5)
            except KeyboardInterrupt:
                pass
    return 0


# ======================================================== services per OS


def service_argv(component: str, uv: str, script: Path, data: Path) -> list[str]:
    return [
        uv,
        "run",
        "--quiet",
        "--no-project",
        str(script),
        "run",
        component,
        "--data",
        str(data),
        "--service",
    ]


def systemd_quote(arg: str) -> str:
    arg = arg.replace("%", "%%").replace("$", "$$")
    if re.search(r'[\s"\'\\;]', arg):
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return arg


def systemd_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "systemd" / "user"


def render_systemd_unit(component: str, argv: list[str], data: Path) -> str:
    after = "network-online.target" + (" wa-bridge.service" if component != "bridge" else "")
    desc = {
        "bridge": "WhatsApp bridge",
        "app": "wa-app chat PWA",
        "mcp": "WhatsApp MCP server (shared HTTP)",
    }[component]
    return f"""{MARKER}
[Unit]
Description=wactl {desc}
Wants=network-online.target
After={after}

[Service]
Type=simple
WorkingDirectory={str(data).replace("%", "%%")}
ExecStart={" ".join(systemd_quote(a) for a in argv)}
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
# Exit code 3 means "stream replaced": another process took the WhatsApp session.
# Restarting would steal it back and start a ping-pong, so never restart on it.
# Exit code 4 means "no linked device": restarting would only start a new pairing each time.
RestartPreventExitStatus=3 4

[Install]
WantedBy=default.target
"""


def foreign_unit_conflict(unit_dir: Path, components: list[str]) -> str | None:
    for c in components:
        p = unit_dir / f"wa-{c}.service"
        if p.is_file() and MARKER not in p.read_text(errors="replace"):
            return (
                f"{p} exists and was not written by wactl, so wactl will not replace it. "
                "Use the existing unit, or move it aside, then run `wactl service install` again."
            )
    return None


def launchd_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def render_launchd_plist(component: str, argv: list[str], data: Path, path_env: str) -> bytes:
    return plistlib.dumps(
        {
            "Label": f"com.wactl.{component}",
            "ProgramArguments": argv,
            "WorkingDirectory": str(data),
            "EnvironmentVariables": {"PATH": path_env},
            "Umask": 0o077,
            "RunAtLoad": True,
            # wactl run supervises and honours exit code 3, so launchd must not restart it.
            "KeepAlive": False,
            "StandardOutPath": str(data / "logs" / f"{component}.log"),
            "StandardErrorPath": str(data / "logs" / f"{component}.log"),
        }
    )


def windows_task_command(launcher: str, component: str, data: str) -> str:
    """The /TR command. No --log: `run` logs to <data>\\logs\\<component>.log itself on Windows."""

    def q(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"

    inner = f"& {q(launcher)} run {component} --data {q(data)} --service"
    return f'powershell.exe -NoProfile -WindowStyle Hidden -Command "{inner}"'


def schtasks_create_args(component: str, launcher: str, data: str) -> list[str]:
    return [
        "schtasks",
        "/Create",
        "/SC",
        "ONLOGON",
        "/TN",
        f"wactl-{component}",
        "/TR",
        windows_task_command(launcher, component, data),
        "/RL",
        "LIMITED",
        "/F",
    ]


def service_names(args: argparse.Namespace) -> list[str]:
    return [*DEFAULT_COMPONENTS, *(["mcp"] if getattr(args, "with_mcp", False) else [])]


def installed_services(ctx: Context) -> list[str]:
    found = []
    for c in COMPONENTS:
        if IS_WINDOWS:
            ok = (
                subprocess.run(["schtasks", "/Query", "/TN", f"wactl-{c}"], capture_output=True).returncode
                == 0
            )
        elif IS_MAC:
            ok = (launchd_dir() / f"com.wactl.{c}.plist").is_file()
        else:
            p = systemd_dir() / f"wa-{c}.service"
            ok = p.is_file() and MARKER in p.read_text(errors="replace")
        if ok:
            found.append(c)
    return found


def uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def service_state(ctx: Context, c: str) -> str:
    if IS_WINDOWS:
        out = tool_output(["schtasks", "/Query", "/TN", f"wactl-{c}", "/FO", "LIST"])
        m = re.search(r"Status:\s*(\w+)", out)
        return m.group(1).lower() if m else "unknown"
    if IS_MAC:
        return (
            "loaded"
            if subprocess.run(
                ["launchctl", "print", f"gui/{uid()}/com.wactl.{c}"], capture_output=True
            ).returncode
            == 0
            else "not loaded"
        )
    return tool_output(["systemctl", "--user", "is-active", f"wa-{c}"]) or "unknown"


def service_control(ctx: Context, action: str, c: str) -> int:
    if IS_WINDOWS:
        # /End only stops the hidden PowerShell wrapper; kill the whole tree from the pid file.
        if action in ("stop", "restart"):
            pid = read_pid(ctx, c)
            if pid:
                stop_run(ctx, c, pid)
            ctx.pidfile(c).unlink(missing_ok=True)
        cmds = {
            "start": [["schtasks", "/Run", "/TN", f"wactl-{c}"]],
            "stop": [["schtasks", "/End", "/TN", f"wactl-{c}"]],
            "restart": [["schtasks", "/Run", "/TN", f"wactl-{c}"]],
        }[action]
    elif IS_MAC:
        target = f"gui/{uid()}/com.wactl.{c}"
        cmds = {
            "start": [["launchctl", "kickstart", target]],
            "stop": [["launchctl", "kill", "SIGTERM", target]],
            "restart": [["launchctl", "kickstart", "-k", target]],
        }[action]
    else:
        cmds = [["systemctl", "--user", action, f"wa-{c}"]]
    rc = 0
    for cmd in cmds:
        rc |= subprocess.run(cmd, capture_output=True).returncode
    say(f"{c}: {action} {'ok' if rc == 0 else 'failed'} (service manager)")
    return 1 if rc else 0


def uv_path() -> str:
    return shutil.which("uv") or "uv"


def plan_service_install(
    ctx: Context, comps: list[str]
) -> tuple[list[tuple[Path, bytes | str, bool]], list[tuple[list[str], bool]]]:
    """Return (files to write as (path, content, private), commands as (argv, must_succeed))."""
    files: list[tuple[Path, bytes | str, bool]] = []
    cmds: list[tuple[list[str], bool]] = []
    if IS_WINDOWS:
        for c in comps:
            cmds.append((schtasks_create_args(c, str(ctx.launcher), str(ctx.data)), True))
    elif IS_MAC:
        for c in comps:
            p = launchd_dir() / f"com.wactl.{c}.plist"
            argv = service_argv(c, uv_path(), ctx.script, ctx.data)
            path_env = os.pathsep.join(
                dict.fromkeys(
                    [str(Path(uv_path()).parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
                )
            )
            files.append((p, render_launchd_plist(c, argv, ctx.data, path_env), True))
            # bootout first so a second install replaces the loaded job instead of failing
            cmds.append((["launchctl", "bootout", f"gui/{uid()}/com.wactl.{c}"], False))
            cmds.append((["launchctl", "bootstrap", f"gui/{uid()}", str(p)], True))
    else:
        for c in comps:
            argv = service_argv(c, uv_path(), ctx.script, ctx.data)
            files.append((systemd_dir() / f"wa-{c}.service", render_systemd_unit(c, argv, ctx.data), False))
        cmds.append((["systemctl", "--user", "daemon-reload"], True))
        for c in comps:
            cmds.append((["systemctl", "--user", "enable", "--now", f"wa-{c}"], True))
    return files, cmds


def cmd_service(ctx: Context, args: argparse.Namespace) -> int:
    action = args.action
    comps = service_names(args)
    if action == "status":
        svc = installed_services(ctx)
        if not svc:
            say("No wactl services installed. Run `wactl service install`.")
        for c in svc:
            say(f"{c:7} {service_state(ctx, c)}")
        return 0
    dry = args.dry_run
    prefix = "[dry run] would " if dry else ""
    if action == "install":
        if not IS_WINDOWS and not IS_MAC:
            conflict = foreign_unit_conflict(systemd_dir(), comps)
            if conflict:
                say(f"wactl: {conflict}")
                return 1
        if IS_WINDOWS:
            for c in comps:
                tr = windows_task_command(str(ctx.launcher), c, str(ctx.data))
                if len(tr) > TR_MAX:
                    say(
                        f"wactl: the task command for {c} is {len(tr)} characters; Windows allows {TR_MAX}. "
                        "Move the repo or the data directory to a shorter path (for example C:\\wa)."
                    )
                    return 1
        files, cmds = plan_service_install(ctx, comps)
        for path, content, private in files:
            say(f"{prefix}write {path}" + (" (mode 600)" if private else ""))
            if dry:
                say(content.decode() if isinstance(content, bytes) else content)
            elif private:
                write_private(path, content)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content if isinstance(content, str) else content.decode())
        if not dry:
            ensure_private_dir(ctx.data / "logs")
            ensure_private_dir(ctx.data / "run")
            if IS_MAC:
                for c in comps:
                    open_private_log(ctx.logs() / f"{c}.log").close()
        for cmd, required in cmds:
            say(f"{prefix}run {subprocess.list2cmdline(cmd) if IS_WINDOWS else shlex.join(cmd)}")
            if not dry:
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode != 0 and required:
                    say(f"  failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()}")
                    return 1
        if sys.platform.startswith("linux"):
            say("To keep the services running when you are logged out: `loginctl enable-linger $USER`")
        say(
            "[dry run] nothing was changed." if dry else "Services installed. Check them with `wactl status`."
        )
        return 0
    # uninstall
    for c in comps if not args.all_installed else COMPONENTS:
        if IS_WINDOWS:
            ucmds = [
                ["schtasks", "/End", "/TN", f"wactl-{c}"],
                ["schtasks", "/Delete", "/TN", f"wactl-{c}", "/F"],
            ]
            paths: list[Path] = []
        elif IS_MAC:
            p = launchd_dir() / f"com.wactl.{c}.plist"
            ucmds, paths = [["launchctl", "bootout", f"gui/{uid()}/com.wactl.{c}"]], [p]
        else:
            p = systemd_dir() / f"wa-{c}.service"
            if p.is_file() and MARKER not in p.read_text(errors="replace"):
                say(f"{c}: {p} is not a wactl unit, leaving it alone")
                continue
            ucmds, paths = [["systemctl", "--user", "disable", "--now", f"wa-{c}"]], [p]
        if not dry and not any(c == x for x in installed_services(ctx)):
            continue
        for cmd in ucmds:
            say(f"{prefix}run {shlex.join(cmd)}")
            if not dry:
                subprocess.run(cmd, capture_output=True)
        for p in paths:
            say(f"{prefix}delete {p}")
            if not dry:
                p.unlink(missing_ok=True)
    if not dry and sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    say("[dry run] nothing was changed." if dry else "Services removed. Your data was not touched.")
    return 0


# ===================================================================== update


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def check_update_ready(repo: Path) -> str | None:
    """None if `git pull --ff-only` is safe, else the reason it is not."""
    # Untracked files (data dir run/, logs/, backups/) never block a fast-forward pull.
    if git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return (
            "The working tree has uncommitted changes. Commit or stash them, then run `wactl update` again."
        )
    upstream = git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.returncode != 0:
        return "This branch has no upstream to update from."
    fetch = git(repo, "fetch", "--quiet")
    if fetch.returncode != 0:
        return f"git fetch failed: {fetch.stderr.strip()}"
    if git(repo, "merge-base", "--is-ancestor", "HEAD", "@{u}").returncode != 0:
        return (
            "This branch cannot fast-forward to its upstream (local commits or diverged). Update it by hand."
        )
    return None


def backup_store(ctx: Context) -> Path:
    """Copy the databases (sqlite backup API) and the bridge binary; all private to this user."""
    root = ensure_private_dir(ctx.data / "backups")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest, n = root / stamp, 0
    while True:
        try:
            dest.mkdir()
            break
        except FileExistsError:
            n += 1
            dest = root / f"{stamp}-{n}"
    if not IS_WINDOWS:
        os.chmod(dest, 0o700)
    for name in ("messages.db", "whatsapp.db"):
        src = ctx.store / name
        if not src.is_file():
            continue
        target = dest / name
        os.close(os.open(target, os.O_WRONLY | os.O_CREAT, 0o600))  # exists at 600 before sqlite opens it
        # sqlite3 backup API: a consistent copy even while the bridge is writing.
        with (
            contextlib.closing(sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)) as s_con,
            contextlib.closing(sqlite3.connect(target)) as d_con,
        ):
            s_con.backup(d_con)
        if not IS_WINDOWS:
            os.chmod(target, 0o600)
    if ctx.bridge_bin.is_file():
        copy = dest / f"whatsapp-bridge{EXE}"
        shutil.copy2(ctx.bridge_bin, copy)
        if not IS_WINDOWS:
            os.chmod(copy, 0o700)
    return dest


def wait_healthy(url: str, seconds: int = 60) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if http_ok(url):
            return True
        time.sleep(2)
    return False


def recall_installed(ctx: Context) -> bool:
    """True if the MCP server venv has the recall extras, so an update keeps them."""
    venv = ctx.repo / "whatsapp-mcp-server" / ".venv"
    return (
        any(venv.glob("lib/python*/site-packages/sentence_transformers"))
        or (venv / "Lib" / "site-packages" / "sentence_transformers").is_dir()
    )


@dataclass
class UpdateOps:
    """Side effects of an update, injectable so the sequence is testable with fakes."""

    build: Callable[[Context], None]
    stop: Callable[[Context, str], None]
    start: Callable[[Context, str], None]
    healthy: Callable[[str], bool]
    replace: Callable[[Path, Path], object] = os.replace
    is_active: Callable[[Context, str], bool] = lambda ctx, c: False


def stop_component(ctx: Context, c: str) -> None:
    if c in installed_services(ctx):
        service_control(ctx, "stop", c)
    pid = read_pid(ctx, c)
    if pid:
        stop_run(ctx, c, pid)
    if c == "bridge":
        wait_port_free(bridge_port(ctx.env), threading.Event(), 30)


def start_component(ctx: Context, c: str) -> None:
    if c in installed_services(ctx):
        service_control(ctx, "start", c)
    elif check_runnable(ctx, c) is None:
        spawn_detached(ctx, c)


def component_active(ctx: Context, c: str) -> bool:
    return c in installed_services(ctx) or read_pid(ctx, c) is not None


def real_update_ops() -> UpdateOps:
    def build(ctx: Context) -> None:
        for label, cmd, cwd in build_steps(
            ctx, minimal=not recall_installed(ctx), bridge_out=f"whatsapp-bridge.new{EXE}", inexact=True
        ):
            run_step(label, cmd, cwd)

    return UpdateOps(
        build=build,
        stop=stop_component,
        start=start_component,
        healthy=wait_healthy,
        is_active=component_active,
    )


def apply_update(ctx: Context, ops: UpdateOps, backup: Path, say_: Callable[[str], None] = say) -> bool:
    """Build, swap the bridge binary, restart, verify. Roll the binary back on failure.

    Windows cannot overwrite a running .exe, so the order is the same everywhere: build next to the
    old binary, stop the bridge, rename the old one aside, rename the new one in, start.
    """
    bridge_url = f"http://127.0.0.1:{bridge_port(ctx.env)}/api/health"
    app_url = f"http://127.0.0.1:{app_port(ctx.env)}/api/health"
    old = ctx.bridge_bin.with_name(f"whatsapp-bridge.old{EXE}")
    new = ctx.bridge_bin.with_name(f"whatsapp-bridge.new{EXE}")
    swapped = stopped = False
    touched: list[str] = []  # components update stopped or restarted, to bring back after a rollback
    failure = ""
    try:
        ops.build(ctx)
        ops.stop(ctx, "bridge")
        stopped = True
        try:
            if ctx.bridge_bin.exists():
                ops.replace(ctx.bridge_bin, old)
            ops.replace(new, ctx.bridge_bin)
            swapped = True
        except OSError as exc:
            if old.exists() and not ctx.bridge_bin.exists():
                with contextlib.suppress(OSError):
                    ops.replace(old, ctx.bridge_bin)
            raise StepError(f"could not swap in the new bridge binary: {exc}") from exc
        ops.start(ctx, "bridge")
        if not ops.healthy(bridge_url):
            raise StepError("the bridge did not become healthy within 60 s")
        touched.append("app")
        ops.stop(ctx, "app")
        ops.start(ctx, "app")
        if not ops.healthy(app_url):
            raise StepError("wa-app did not become healthy within 60 s")
        if ops.is_active(ctx, "mcp"):
            touched.append("mcp")
            ops.stop(ctx, "mcp")
            ops.start(ctx, "mcp")
    except Exception as exc:  # any failure after the backup must reach the rollback below
        failure = str(exc) or type(exc).__name__
    if not failure:
        with contextlib.suppress(OSError):
            old.unlink(missing_ok=True)
        say_("Update finished; bridge and app are healthy.")
        return True
    say_(f"\nUpdate failed: {failure}")
    if swapped:
        restored = False
        try:
            ops.stop(ctx, "bridge")
            ops.replace(old, ctx.bridge_bin)
            restored = True
        except Exception as exc:
            say_(f"Could not put the previous bridge binary back: {exc}")
        if restored:
            try:
                ops.start(ctx, "bridge")
                if ops.healthy(bridge_url):
                    say_("The previous bridge binary was put back and the bridge is healthy again.")
                else:
                    say_("The previous bridge binary was put back but the bridge did not become healthy.")
            except Exception as exc:
                say_(f"The previous bridge binary was put back but starting it failed: {exc}")
        else:
            say_(f"The previous bridge was NOT restored. A copy is at {backup / ('whatsapp-bridge' + EXE)}.")
    elif stopped:
        try:
            ops.start(ctx, "bridge")
            say_("The bridge binary was not replaced; the old bridge was started again.")
        except Exception as exc:
            say_(f"The bridge binary was not replaced, but starting the old bridge failed: {exc}")
    for c in touched:
        try:
            ops.stop(ctx, c)
            ops.start(ctx, c)
            say_(f"{c} was restarted.")
        except Exception as exc:
            say_(f"Could not restart {c}: {exc}. Start it with `wactl start {c}`.")
    say_(
        f"The backup in {backup} is the pre-update state of your databases. The new bridge may already "
        "have changed them; restore the backup by hand if the old bridge misbehaves."
    )
    return False


def cmd_update(ctx: Context, args: argparse.Namespace) -> int:
    problem = check_update_ready(ctx.repo)
    if problem:
        say(f"wactl update: {problem}")
        return 1
    old = git(ctx.repo, "rev-parse", "HEAD").stdout.strip()
    if (
        can_prompt(args.yes)
        and input("Pull the latest code, rebuild and restart? [y/N] ").strip().lower() != "y"
    ):
        say("Cancelled.")
        return 1
    backup = backup_store(ctx)
    say(f"Backed up the databases and the bridge binary to {backup}")
    pull = git(ctx.repo, "pull", "--ff-only", "--quiet")
    if pull.returncode != 0:
        say(f"git pull failed, nothing was changed except the backup: {pull.stderr.strip()}")
        return 1
    new = git(ctx.repo, "rev-parse", "HEAD").stdout.strip()
    if old == new:
        say("Already up to date.")
    else:
        log = git(ctx.repo, "log", "--oneline", f"{old}..{new}").stdout.splitlines()
        say(f"Updated {len(log)} commit(s):")
        say("\n".join("  " + line for line in log[:10]))
    if apply_update(ctx, real_update_ops(), backup):
        return 0
    say(f"The code is still at the new commit; to go back: git checkout {old}")
    return 1


# ======================================================================== cli


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data", help="data directory (default: WA_DATA_DIR from .env, else whatsapp-bridge/)"
    )
    common.add_argument("--yes", "-y", action="store_true", help="never prompt")
    p = argparse.ArgumentParser(
        prog="wactl", description="Control whatsapp-mcp-pro on Linux, macOS and Windows."
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", parents=[common], help="check this machine")
    s = sub.add_parser("setup", parents=[common], help="create .env, build everything")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--minimal", action="store_true", help="no recall or transcription extras")
    g.add_argument("--recall", action="store_true", help="include recall and transcription extras (default)")
    sub.add_parser("pair", parents=[common], help="run the bridge in the foreground to scan the QR code")
    r = sub.add_parser("run", parents=[common], help="foreground supervisor for one component")
    r.add_argument("component", choices=COMPONENTS)
    r.add_argument("--log", help="append output to this file")
    r.add_argument(
        "--service", action="store_true", help="started by a service manager (set by wactl service install)"
    )
    for name in ("start", "stop", "restart", "status"):
        c = sub.add_parser(name, parents=[common], help=f"{name} components")
        c.add_argument("component", nargs="?", choices=COMPONENTS)
    sv = sub.add_parser("service", parents=[common], help="install or remove login services")
    sv.add_argument("action", choices=("install", "uninstall", "status"))
    sv.add_argument("--with-mcp", action="store_true", help="also manage the shared MCP HTTP server")
    sv.add_argument("--dry-run", action="store_true", help="print what would be written and run")
    sv.add_argument(
        "--all", dest="all_installed", action="store_true", help="uninstall: all three components"
    )
    sub.add_parser("update", parents=[common], help="pull, back up, rebuild, restart")
    lg = sub.add_parser("logs", parents=[common], help="show a component log")
    lg.add_argument("component", nargs="?", choices=COMPONENTS)
    lg.add_argument("-n", "--lines", type=int, default=100)
    lg.add_argument("-f", "--follow", action="store_true")
    return p


def find_repo() -> Path:
    return Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = find_repo()
    base_env = load_env(repo, repo / "whatsapp-bridge")
    data = resolve_data_dir(args.data, base_env, repo)
    ctx = Context(repo=repo, data=data, env=load_env(repo, data), yes=args.yes)
    handlers: dict[str, Callable[[Context, argparse.Namespace], int]] = {
        "doctor": cmd_doctor,
        "setup": cmd_setup,
        "pair": cmd_pair,
        "run": cmd_run,
        "start": cmd_control("start"),
        "stop": cmd_control("stop"),
        "restart": cmd_control("restart"),
        "status": cmd_status,
        "service": cmd_service,
        "update": cmd_update,
        "logs": cmd_logs,
    }
    return handlers[args.cmd](ctx, args)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Shared session plumbing for wa-dispatch and meet-dispatch.

Everything here is about *delivering work to a Claude session in a project
directory* — finding a live one, opening a visible tmux tab, typing into it,
and not doing any of that twice. Nothing here knows what WhatsApp is, which is
the point: the meeting pipeline reuses all of it.
"""
import json
import os
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wa_config as cfg  # noqa: E402


# Defined here, not imported from a caller: spawn_visible needs it, and when
# this module was split out of wa-dispatch.py the constant stayed behind —
# every cold spawn NameError'd for hours while warm paths kept working, so
# nothing looked broken.
HERE = os.path.dirname(os.path.abspath(__file__))
DENY_SETTINGS = os.path.join(HERE, "spawn-settings.json")


def log(msg: str) -> None:
    print("[wa-session] %s" % msg, flush=True)


def _process_tree():
    """(children-by-ppid, command-by-pid) from a single ps call."""
    kids, cmd = {}, {}
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return kids, cmd
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        kids.setdefault(ppid, []).append(pid)
        cmd[pid] = parts[2]
    return kids, cmd


def _runs_claude(pane_pid, kids, cmd, depth=4):
    """Whether a live claude process exists under this pane's shell.

    This replaced matching the pane's on-screen text for 'bypass permissions'.
    That text stays in the scrollback after Claude exits, so a pane where a
    session had merely once run looked warm forever — and prompts meant for
    Claude were typed into a bare zsh prompt and executed as shell commands.
    Observed live: 'zsh: parse error near `do`'.
    """
    stack = [(pane_pid, 0)]
    while stack:
        pid, d = stack.pop()
        c = (cmd.get(pid) or "").lower()
        if "claude" in c and "grep" not in c:
            return True
        if d < depth:
            for k in kids.get(pid, []):
                stack.append((k, d + 1))
    return False


def find_claude_pane(proj: str):
    """The tmux pane with a LIVE Claude running in proj, else None.

    Matches on pane path plus a real process in the pane's tree — not the
    pane's title or its scrollback, both of which outlive the session.
    """
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_id}\t#{pane_pid}\t#{pane_current_path}"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    try:
        target = os.path.realpath(os.path.expanduser(proj))
    except Exception:
        return None
    kids, cmd = _process_tree()
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pane, pane_pid, path = parts
        try:
            if os.path.realpath(path) != target:
                continue
            if _runs_claude(int(pane_pid), kids, cmd):
                return pane
        except Exception:
            continue
    return None


def _touch_marker(proj: str, name: str) -> None:
    """Create/refresh a timestamp marker under the project's .wa-dispatch dir."""
    try:
        rundir = os.path.join(proj, ".wa-dispatch")
        os.makedirs(rundir, exist_ok=True)
        open(os.path.join(rundir, name), "w").close()
    except Exception:
        pass



def ensure_trusted(proj: str) -> None:
    """Pre-accept the workspace trust dialog for a routed project.

    Without this an interactive spawn blocks on "Is this a project you trust?"
    and the session sits there all night doing nothing. Written atomically and
    only when not already set, to minimise clobbering Claude's own writes.
    """
    cfgp = os.path.expanduser("~/.claude.json")
    try:
        with open(cfgp) as f:
            cfg = json.load(f)
        key = os.path.realpath(os.path.expanduser(proj))
        entry = cfg.setdefault("projects", {}).setdefault(key, {})
        if entry.get("hasTrustDialogAccepted"):
            return
        entry["hasTrustDialogAccepted"] = True
        tmp = cfgp + ".wa-dispatch-tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, cfgp)
        log("pre-trusted %s" % key)
    except Exception as e:
        log("could not pre-trust %s: %s" % (proj, e))



def tmux_target_session() -> str:
    """The tmux session to open new project windows in — the one you're attached
    to, so the window shows up where you're actually looking."""
    try:
        out = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}\t#{session_attached}"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return "main"
    names, attached = [], None
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            names.append(parts[0])
            if parts[1].strip() not in ("0", ""):
                attached = parts[0]
    if attached:
        return attached
    return "main" if "main" in names else (names[0] if names else "main")



def wait_for_claude_ready(pane: str, timeout_sec: int = 75) -> bool:
    """Block until the freshly-launched Claude is accepting input.

    Keystrokes sent before the TUI is up are simply lost, which would leave a
    session open but never told about the message.
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            out = subprocess.run(
                ["tmux", "capture-pane", "-t", pane, "-p"],
                capture_output=True, text=True, timeout=10,
            )
            txt = out.stdout
            if "bypass permissions" in txt or "❯" in txt:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False



def ensure_project_hook(proj: str) -> None:
    """Install the deny hook into the project's own local settings.

    Passing --settings alongside -c makes Claude exit with "Execution error",
    so the launcher could either resume the project's conversation or load the
    safety hook, never both. Project-level settings need no flag, so they
    compose with -c — and they also apply to sessions Simon starts by hand,
    which the --settings approach never could.

    Written to settings.local.json, which is the conventional gitignored
    override, so this never lands in a client repo.
    """
    try:
        d = os.path.join(os.path.expanduser(proj), ".claude")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "settings.local.json")
        try:
            cfgj = json.load(open(path))
        except Exception:
            cfgj = {}
        hooks = cfgj.setdefault("hooks", {}).setdefault("PreToolUse", [])
        deny = os.path.join(HERE, "hooks", "deny.py")
        if any(deny in json.dumps(h) for h in hooks):
            return
        hooks.append({"matcher": "*",
                      "hooks": [{"type": "command", "command": deny}]})
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfgj, f, indent=2)
        os.replace(tmp, path)
        log("installed deny hook in %s/.claude/settings.local.json"
            % os.path.basename(os.path.expanduser(proj)))
    except Exception as e:
        log("could not install project hook for %s: %s" % (proj, e))


def find_idle_pane(proj: str):
    """An existing pane sitting at a shell in proj, if there is one.

    Simon keeps one pane per project inside a single window rather than a
    window per project. Opening a new window for a project that already has a
    pane duplicates his layout and leaves the original sitting idle beside it,
    so prefer starting Claude where the pane already is.
    """
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_id}\t#{pane_pid}\t#{pane_current_path}"],
            capture_output=True, text=True, timeout=10)
        target = os.path.realpath(os.path.expanduser(proj))
    except Exception:
        return None
    kids, cmd = _process_tree()
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pane, pane_pid, path = parts
        try:
            if os.path.realpath(path) != target:
                continue
            if not _runs_claude(int(pane_pid), kids, cmd):
                return pane          # right directory, nothing running in it
        except Exception:
            continue
    return None


def spawn_visible(proj: str, label: str, prompt: str):
    """Open a real, visible tmux window running Claude in the project.

    The message is passed as the launch prompt rather than typed in afterwards:
    `claude -c "<prompt>"` resumes the project's previous conversation AND
    delivers the message in one shot, so there's no wait-for-TUI-then-send-keys
    race. The session stays interactive, so Simon can attach and keep going.

    Two quirks handled here, both found the hard way:
      * `-c` with no prompt aborts ("provide a prompt to continue").
      * `-c` in a project with no prior conversation aborts ("no conversation
        found") instead of starting fresh — hence the `||` fallback.
      * `exec zsh` keeps the window alive if Claude ever exits, so a failed
        spawn leaves a visible shell in the project instead of vanishing.
    """
    ensure_trusted(proj)
    ensure_project_hook(proj)

    # Reuse an idle pane already sitting in this project before making a window.
    idle = find_idle_pane(proj)
    if idle:
        cb = shlex.quote(cfg.path("CLAUDE_BIN") or "claude")
        fl = cfg.get("CLAUDE_FLAGS")
        q = shlex.quote(prompt)
        launch = "%s %s -c %s || %s %s %s" % (cb, fl, q, cb, fl, q)
        subprocess.run(["tmux", "send-keys", "-t", idle, "-l", launch],
                       capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", idle, "Enter"], capture_output=True)
        deadline = time.time() + 40
        while time.time() < deadline:
            if find_claude_pane(proj):
                log("started Claude in existing pane %s" % idle)
                return idle
            time.sleep(2)
        return None

    target = tmux_target_session() + ":"
    base = "%s %s" % (
        shlex.quote(cfg.path("CLAUDE_BIN") or "claude"),
        cfg.get("CLAUDE_FLAGS"),
    )
    p = shlex.quote(prompt)
    inner = "%s -c %s || %s %s; exec zsh" % (base, p, base, p)
    cmd = "sh -c %s" % shlex.quote(inner)
    try:
        subprocess.run(
            ["tmux", "new-window", "-d", "-t", target, "-n", label, "-c", proj, cmd],
            capture_output=True, timeout=15,
        )
    except Exception as e:
        log("tmux new-window failed: %s" % e)
        return None
    deadline = time.time() + 40
    while time.time() < deadline:
        pane = find_claude_pane(proj)
        if pane:
            announce_window(target, label)
            return pane
        time.sleep(2)
    return None



def nudge_recently(proj: str, window_sec: int = 90) -> bool:
    """True if we nudged this project's live session very recently.

    Keeps a burst of messages from typing five separate prompts into your
    session — the first nudge makes it read every new inbox line anyway.
    """
    marker = os.path.join(proj, ".wa-dispatch", "last-nudge")
    try:
        return (time.time() - os.path.getmtime(marker)) < window_sec
    except OSError:
        return False



def nudge_pane(pane: str, proj: str, msg: str) -> None:
    """Type a prompt into the live session so it acts now, not at the next turn.

    SECURITY: a literal newline inside `send-keys -l` is a carriage return, not
    text. Any newline in the payload therefore SUBMITS the line before it as an
    independent turn. Since payloads can carry attacker-controlled content
    (message bodies, chat names, push names), an unsanitised nudge let anyone
    who could message a routed chat inject a prompt into a live session — one
    started by the user, so without the deny hook. Verified exploitable.

    Everything typed here is collapsed to a single line. Untrusted content
    belongs in the inbox file, never in the keystroke stream.
    """
    flat = " ".join(str(msg).split())  # collapses \r, \n, \t and runs of space
    subprocess.run(["tmux", "send-keys", "-t", pane, "-l", flat], capture_output=True)
    time.sleep(0.4)
    subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], capture_output=True)
    _touch_marker(proj, "last-nudge")
    # Claim these lines for the nudge. The Stop hook uses the same cursor, so
    # without this it would re-surface the very items we just handed over when
    # the nudged turn ends — a guaranteed duplicate.
    _claim_inbox_lines(proj)



def _claim_inbox_lines(proj: str) -> None:
    """Advance the shared inbox cursor to the current line count."""
    inbox = os.path.join(proj, ".wa-inbox.jsonl")
    cursor = os.path.join(proj, ".wa-dispatch", "inbox.cursor")
    try:
        with open(inbox, encoding="utf-8") as f:
            n = len([ln for ln in f.read().splitlines() if ln.strip()])
        os.makedirs(os.path.dirname(cursor), exist_ok=True)
        with open(cursor, "w") as f:
            f.write(str(n))
    except Exception:
        pass  # cursor is an optimisation; never break dispatch over it



def in_cooldown(proj: str, cooldown_min: int):
    """Seconds remaining before another prepare-run may start, else 0.

    Batches bursts: a chatty thread appends to the inbox instead of starting a
    run per message. The next run picks up everything accumulated.
    """
    if cooldown_min <= 0:
        return 0
    marker = os.path.join(proj, ".wa-dispatch", "last-run")
    try:
        last = os.path.getmtime(marker)
    except OSError:
        return 0
    elapsed = time.time() - last
    remaining = cooldown_min * 60 - elapsed
    return int(remaining) if remaining > 0 else 0


# ---------- M1 DB poll ----------

def prepare_running(proj: str) -> bool:
    """True if a headless prepare-run is already in flight for this project."""
    lock = os.path.join(proj, ".wa-dispatch", "run.lock")
    try:
        pid = int(open(lock).read().strip())
        os.kill(pid, 0)  # signal 0 = existence check
        return True
    except Exception:
        return False

def announce_window(target: str, label: str) -> None:
    """Make a newly opened tab noticeable without stealing focus.

    The window is created detached on purpose — nothing should hijack the pane
    you're typing in. But a tab that appears silently gets missed, so flag it in
    the status bar and print a one-off message to the attached client.
    """
    win = "%s%s" % (target, label)
    for args in (
        ["tmux", "set-option", "-w", "-t", win, "monitor-activity", "on"],
        ["tmux", "display-message", "-t", target,
         "wa-dispatch: new WhatsApp message -> window '%s'" % label],
    ):
        try:
            subprocess.run(args, capture_output=True, timeout=10)
        except Exception:
            pass

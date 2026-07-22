#!/usr/bin/env python3
"""Shared config loader for wa-dispatch and wa-approve.

Reads `config.env` sitting next to this file (copy of config.example.env).
Real environment variables win over the file, so a launchd plist or a one-off
shell override can change behaviour without editing config.

Deliberately dependency-free: this runs from launchd against the system
python3, where nothing is pip-installed.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("WA_DISPATCH_CONFIG", os.path.join(HERE, "config.env"))

DEFAULTS = {
    "BRIDGE_MODE": "local",
    "BRIDGE_SSH_HOST": "",
    "BRIDGE_DB": "~/whatsapp-mcp-pro/whatsapp-bridge/store/messages.db",
    "BRIDGE_API": "http://127.0.0.1:8080",
    "BRIDGE_ENV_FILE": "~/whatsapp-mcp-pro/.env",
    "POLL_SECONDS": "20",
    "DEFAULT_COOLDOWN_MIN": "10",
    "CLAUDE_BIN": "claude",
    "CLAUDE_FLAGS": "--dangerously-skip-permissions",
    "APPROVE_ENABLED": "true",
    "APPROVE_BIND": "127.0.0.1",
    "APPROVE_PORT": "8086",
    "APPROVE_PUBLIC_URL": "",
    "APPROVE_TTL_HOURS": "12",
    "NTFY_URL": "",
    "NTFY_TOPIC_FILE": "",
    "FATHOM_KEY_FILE": "",
    "NTFY_TOKEN_FILE": "",
}


def _parse(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k.strip()] = v
    except FileNotFoundError:
        pass
    return out


_file = _parse(CONFIG_FILE)


def get(key, default=None):
    """Env var > config.env > built-in default."""
    if key in os.environ:
        return os.environ[key]
    if key in _file:
        return _file[key]
    if default is not None:
        return default
    return DEFAULTS.get(key, "")


def path(key, default=None):
    v = get(key, default)
    return os.path.expanduser(v) if v else ""


def flag(key, default=None):
    return str(get(key, default)).strip().lower() in ("1", "true", "yes", "on")


def num(key, default=None):
    try:
        return int(str(get(key, default)).strip())
    except (TypeError, ValueError):
        return int(DEFAULTS.get(key, 0) or 0)


def read_secret(key):
    """Read a secret out of the file named by <key>, stripped. '' if unset."""
    p = path(key)
    if not p:
        return ""
    try:
        return open(p).read().strip()
    except OSError:
        return ""


def bridge_api_key():
    """The bridge's API_KEY.

    BRIDGE_ENV_FILE may be either a file containing just the key, or the
    bridge's own .env — in which case the API_KEY= line is picked out.
    """
    raw = read_secret("BRIDGE_ENV_FILE")
    if not raw:
        return ""
    if "API_KEY=" in raw:
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("API_KEY="):
                return line.split("=", 1)[1].strip()
    return raw.splitlines()[0].strip() if raw else ""


# Fail fast on a flaky link rather than hanging a poll loop.
SSH_OPTS = ["-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]


def ssh_prefix():
    """argv prefix for running a command on the bridge host, or [] if local."""
    if get("BRIDGE_MODE") != "ssh":
        return []
    host = get("BRIDGE_SSH_HOST")
    if not host:
        raise RuntimeError("BRIDGE_MODE=ssh but BRIDGE_SSH_HOST is empty")
    return ["ssh"] + SSH_OPTS + [host]


def db_command(sql, json_out=True):
    """argv that runs `sql` against the bridge DB, local or over SSH.

    The read-only flag is not optional: this tool must never be the reason a
    message store gets written to.
    """
    import shlex
    remote = "sqlite3 -readonly %s %s %s" % (
        "-json" if json_out else "", get("BRIDGE_DB"), shlex.quote(sql),
    )
    if get("BRIDGE_MODE") == "ssh":
        host = get("BRIDGE_SSH_HOST")
        if not host:
            raise RuntimeError("BRIDGE_MODE=ssh but BRIDGE_SSH_HOST is empty")
        return ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", host, remote]
    args = ["sqlite3", "-readonly"]
    if json_out:
        args.append("-json")
    return args + [path("BRIDGE_DB"), sql]

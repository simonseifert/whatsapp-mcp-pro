#!/usr/bin/env python3
"""wa-assistant — message yourself on WhatsApp, get Claude Code back.

Watches the messages you send to your own chat (Note to Self) and answers them
with a persistent Claude Code session. Voice notes in, voice notes out, images
handed to a deeper model. Your phone becomes a terminal you can use from a bus.

Distinct from wa-dispatch, which routes *other people's* messages into project
sessions. This one is a conversation with yourself.

Deliberately dependency-free: runs from launchd against the system python3,
where nothing is pip-installed. Optional features (speech in, speech out) shell
out to binaries and degrade gracefully when those are missing.

SECURITY: message content is untrusted input that reaches a Claude Code session
running with --dangerously-skip-permissions. Only messages *you* send in *your
own* chat are read (is_from_me=1 AND chat_jid in SELF_JIDS), so an attacker
needs your account to reach it. Point CLAUDE_FLAGS at wa-dispatch/hooks/deny.py
to restrict what the session may run. See README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("WA_ASSISTANT_CONFIG", os.path.join(HERE, "config.env"))

DEFAULTS = {
    # --- bridge ---
    "BRIDGE_API": "http://127.0.0.1:8080",
    "BRIDGE_DB": "../whatsapp-bridge/store/messages.db",
    "BRIDGE_STORE": "../whatsapp-bridge/store",
    "BRIDGE_ENV_FILE": "../.env",
    "BRIDGE_SESSION_DB": "../whatsapp-bridge/store/whatsapp.db",
    # Blank = ask the bridge who you are via /api/connection. Set it only for
    # extra JIDs the bridge cannot know about, e.g. your @lid alias.
    "SELF_JIDS": "",
    # --- session ---
    "CLAUDE_BIN": "claude",
    "CLAUDE_MODEL": "claude-sonnet-5",
    "CLAUDE_FLAGS": "--dangerously-skip-permissions --strict-mcp-config",
    "WORKDIR": "~/wa-assistant-workdir",
    "STATE_FILE": "",  # default: <HERE>/.state
    "POLL_SECONDS": "2",
    "REPLY_TIMEOUT": "180",
    "MAX_REPLY_CHARS": "1500",
    "ACK_AFTER_SECONDS": "4",
    "ACK_TEXT": "\U0001f440 on it…",
    # --- heavy delegation (optional) ---
    "HEAVY_ENABLED": "true",
    "HEAVY_MODEL": "claude-opus-5",
    "HEAVY_WORKDIR": "",  # default: WORKDIR
    "HEAVY_TIMEOUT": "600",
    "HEAVY_KEYWORDS": ("deep,brain,build,research,analyze,plan,design,investigate,"
                       "refactor,draft,think,search,fix,process,remember,status"),
    "HEAVY_TRIGGER_CHARS": "\U0001f9e0",
    "HEAVY_LENGTH": "280",
    "HEAVY_NOTICE": "\U0001f9e0 On it — bringing in the deeper model…",
    # --- voice in (optional) ---
    "STT_ENABLED": "false",
    "STT_CMD": "",  # receives the wav path as $1; must print the transcript
    "FFMPEG_BIN": "ffmpeg",
    # --- voice out (optional) ---
    "TTS_ENABLED": "false",
    "TTS_CMD": "",  # receives text on stdin and an output path as $1
    "TTS_PREFIX": "\U0001f50a",  # prefix a text message with this to get a spoken reply
    # --- images (optional) ---
    "IMAGE_ENABLED": "false",
    "IMAGE_PROMPT": "",
    "IMAGE_DEBOUNCE": "25",
}

# Zero-width joiner pair prefixed to everything we send, so the daemon can
# recognise and skip its own messages. Without it, every reply is read back as
# a new prompt and the assistant talks to itself forever.
MARKER = "​​"


# --------------------------------------------------------------------------- config


def _parse_env_file(path: str) -> dict:
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


_file_cfg = _parse_env_file(CONFIG_FILE)


def cfg(key: str, default=None):
    """Env var > config.env > built-in default."""
    if key in os.environ:
        return os.environ[key]
    if key in _file_cfg:
        return _file_cfg[key]
    return DEFAULTS.get(key, default)


def cfg_path(key: str) -> str:
    v = cfg(key) or ""
    if not v:
        return ""
    v = os.path.expanduser(v)
    return v if os.path.isabs(v) else os.path.normpath(os.path.join(HERE, v))


def cfg_bool(key: str) -> bool:
    return str(cfg(key)).strip().lower() in ("1", "true", "yes", "on")


def cfg_int(key: str) -> int:
    try:
        return int(str(cfg(key)).strip())
    except (TypeError, ValueError):
        return int(DEFAULTS[key])


def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def api_key() -> str:
    env_file = cfg_path("BRIDGE_ENV_FILE")
    if env_file:
        for k, v in _parse_env_file(env_file).items():
            if k == "API_KEY":
                return v
    return os.environ.get("API_KEY", "")


# --------------------------------------------------------------------------- bridge


def _post(path: str, payload: dict, timeout: int = 20) -> dict:
    url = cfg("BRIDGE_API").rstrip("/") + path
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": api_key()},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or "{}")


def _get(path: str, timeout: int = 10) -> dict:
    url = cfg("BRIDGE_API").rstrip("/") + path
    req = urllib.request.Request(url, headers={"X-API-Key": api_key()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or "{}")


def _bare(jid: str) -> str:
    """Strip the device suffix: 4915112345678:10@s.whatsapp.net -> ...@s.whatsapp.net.

    The device number identifies which linked device is speaking; the chat with
    yourself is stored without it.
    """
    return re.sub(r":\d+(?=@)", "", jid or "")


def self_jids() -> list[str]:
    """Whose messages count as 'me'. Zero config on a normal install.

    You have TWO identities and which one your self-chat is filed under depends
    on the device: the phone-number jid (`…@s.whatsapp.net`) and the LID
    (`…@lid`). Measured on one account, 2026-08-04 — the same self-chat, same
    final timestamp, filed differently per linked device:

        laptop (device :10)  ->  123456789012345@lid           264 messages
        server (device :12)  ->  4915112345678@s.whatsapp.net  367 messages

    So watching only the jid the bridge reports finds nothing at all on some
    installs. Both come from whatsmeow's own device row; /api/connection
    exposes the jid but not the lid, hence reading the session DB directly.
    """
    jids = [j.strip() for j in (cfg("SELF_JIDS") or "").split(",") if j.strip()]

    session_db = cfg_path("BRIDGE_SESSION_DB")
    if session_db and os.path.exists(session_db):
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % session_db, uri=True)
            row = conn.execute("SELECT jid, lid FROM whatsmeow_device LIMIT 1").fetchone()
            conn.close()
            for value in row or ():
                bare = _bare(value)
                if bare and bare not in jids:
                    jids.append(bare)
        except Exception as e:  # noqa: BLE001 - fall through to the API
            log("could not read identities from %s (%s)" % (session_db, e))

    if not jids:
        try:
            own = _bare((_get("/api/connection") or {}).get("jid") or "")
            if own:
                jids.append(own)
        except Exception as e:  # noqa: BLE001 - fall back to configured list
            log("could not read own jid from bridge (%s)" % e)
    return jids


def send_text(text: str, recipient: str) -> None:
    try:
        _post("/api/send", {"recipient": recipient, "message": MARKER + text})
    except Exception as e:  # noqa: BLE001 - a failed send must not kill the loop
        log("send failed: %s" % e)


# --------------------------------------------------------------------------- session


class WarmSession:
    """A long-lived `claude` process spoken to over stream-json.

    Kept warm on purpose: a cold `claude -p` per message costs seconds of
    startup and, worse, loses the thread of the conversation. Restarts itself
    whenever the pipe dies.
    """

    def __init__(self):
        self.proc = None
        self.start()

    def start(self) -> None:
        cmd = [
            cfg("CLAUDE_BIN"), "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--model", cfg("CLAUDE_MODEL"),
        ] + (cfg("CLAUDE_FLAGS") or "").split()
        workdir = cfg_path("WORKDIR")
        os.makedirs(workdir, exist_ok=True)
        self.proc = subprocess.Popen(
            cmd, cwd=workdir,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        log("session up (pid %s, model %s)" % (self.proc.pid, cfg("CLAUDE_MODEL")))

    def ask(self, text: str) -> str | None:
        if self.proc is None or self.proc.poll() is not None:
            log("session dead, restarting")
            self.start()
        frame = {"type": "user",
                 "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        try:
            self.proc.stdin.write(json.dumps(frame) + "\n")
            self.proc.stdin.flush()
        except Exception as e:  # noqa: BLE001
            log("write failed (%s), restarting" % e)
            self.start()
            return None

        reply = None
        deadline = time.time() + cfg_int("REPLY_TIMEOUT")
        while time.time() < deadline:
            ready, _, _ = select.select([self.proc.stdout], [], [],
                                        max(0.1, deadline - time.time()))
            if not ready:
                log("reply timed out, restarting")
                self.start()
                return reply
            line = self.proc.stdout.readline()
            if not line:
                log("session EOF, restarting")
                self.start()
                return reply
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "assistant":
                for c in ev.get("message", {}).get("content", []):
                    if c.get("type") == "text" and c.get("text"):
                        reply = c["text"]
            elif ev.get("type") == "result":
                return ev.get("result") or reply
        return reply


def run_heavy(task: str, chat_jid: str, speak: bool) -> None:
    """One-shot run on a stronger model, in its own process.

    Threaded by the caller so a ten-minute research task does not block the
    next 'what time is my meeting'.
    """
    send_text(cfg("HEAVY_NOTICE"), chat_jid)
    workdir = cfg_path("HEAVY_WORKDIR") or cfg_path("WORKDIR")
    out = ""
    try:
        cmd = ([cfg("CLAUDE_BIN"), "-p", "--model", cfg("HEAVY_MODEL")]
               + (cfg("CLAUDE_FLAGS") or "").split() + [task])
        r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                           timeout=cfg_int("HEAVY_TIMEOUT"))
        out = (r.stdout or "").strip()
        if not out:
            log("heavy run empty (rc=%s) %s" % (r.returncode, (r.stderr or "")[-180:]))
    except subprocess.TimeoutExpired:
        out = "(still working on that one — it outran the timeout)"
    except Exception as e:  # noqa: BLE001
        log("heavy run failed: %s" % e)
        out = "(the deeper model hit an error)"
    deliver(out or "(came back empty — try rephrasing?)", chat_jid, speak)


def is_heavy(text: str) -> bool:
    if not cfg_bool("HEAVY_ENABLED"):
        return False
    if any(ch and ch in text for ch in (cfg("HEAVY_TRIGGER_CHARS") or "")):
        return True
    if len(text) > cfg_int("HEAVY_LENGTH"):
        return True
    first = ""
    for ch in text.lower().strip():
        if ch.isalpha():
            first += ch
        else:
            break
    return first in [k.strip() for k in (cfg("HEAVY_KEYWORDS") or "").split(",") if k.strip()]


# --------------------------------------------------------------------------- media

SENT_AUDIO: set[str] = set()  # ids of voice notes we sent; never read them back


def find_media(filename: str, chat_jid: str) -> str:
    store = cfg_path("BRIDGE_STORE")
    direct = os.path.join(store, chat_jid, filename)
    if os.path.exists(direct):
        return direct
    try:
        found = subprocess.check_output(["find", store, "-name", filename],
                                        text=True, timeout=20).splitlines()
        return found[0] if found else ""
    except Exception:  # noqa: BLE001
        return ""


def transcribe(filename: str, chat_jid: str, message_id: str) -> str:
    """Voice note -> text via STT_CMD. Blank when disabled or unavailable."""
    if not cfg_bool("STT_ENABLED") or not cfg("STT_CMD"):
        return ""
    path = ""
    try:
        resp = _post("/api/download", {"message_id": message_id, "chat_jid": chat_jid},
                     timeout=45)
        if resp.get("success"):
            path = resp.get("path") or ""
    except Exception as e:  # noqa: BLE001
        log("media download failed: %s" % e)
    if not path or not os.path.exists(path):
        path = find_media(filename, chat_jid)
    if not path:
        return ""
    wav = os.path.join("/tmp", "wa_assistant_stt.wav")
    try:
        subprocess.run([cfg("FFMPEG_BIN"), "-y", "-i", path, "-ar", "16000", "-ac", "1", wav],
                       capture_output=True, timeout=60)
        r = subprocess.run(["/bin/sh", "-c", cfg("STT_CMD") + ' "$1"', "sh", wav],
                           capture_output=True, text=True, timeout=180)
        return (r.stdout or "").strip()
    except Exception as e:  # noqa: BLE001
        log("stt failed: %s" % e)
        return ""
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


def send_voice(text: str, recipient: str) -> None:
    """Speak a reply. Falls back to text on any failure — never drops a reply."""
    if not cfg_bool("TTS_ENABLED") or not cfg("TTS_CMD"):
        return send_text(text, recipient)
    raw = "/tmp/wa_assistant_tts.out"
    ogg = "/tmp/wa_assistant_tts.ogg"
    try:
        r = subprocess.run(["/bin/sh", "-c", cfg("TTS_CMD") + ' "$1"', "sh", raw],
                           input=text[:900], capture_output=True, text=True, timeout=90)
        if r.returncode != 0 or not os.path.exists(raw):
            log("tts produced nothing, sending text")
            return send_text(text, recipient)
        subprocess.run([cfg("FFMPEG_BIN"), "-y", "-i", raw,
                        "-c:a", "libopus", "-b:a", "32k", "-ar", "24000", ogg],
                       capture_output=True, timeout=60)
        if not os.path.exists(ogg):
            return send_text(text, recipient)
        resp = _post("/api/send", {"recipient": recipient, "media_path": ogg}, timeout=30)
        mid = resp.get("message_id") or resp.get("id")
        if mid:
            SENT_AUDIO.add(str(mid))
        log("sent voice reply (%s)" % mid)
    except Exception as e:  # noqa: BLE001
        log("voice send failed (%s), sending text" % e)
        send_text(text, recipient)


def deliver(text: str, chat_jid: str, speak: bool) -> None:
    limit = cfg_int("MAX_REPLY_CHARS")
    if len(text) > limit:
        text = text[:limit] + "…"
    (send_voice if speak else send_text)(text, chat_jid)


# --------------------------------------------------------------------------- state


def state_file() -> str:
    return cfg_path("STATE_FILE") or os.path.join(HERE, ".state")


def get_last() -> int:
    try:
        with open(state_file()) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def set_last(rowid: int) -> None:
    with open(state_file(), "w") as f:
        f.write(str(rowid))


# --------------------------------------------------------------------------- main loop


def preflight() -> int:
    """Validate a fresh install without sending anything. `--check`."""
    ok = True

    def line(good, label, detail=""):
        nonlocal ok
        if not good:
            ok = False
        print("%s %s%s" % ("PASS" if good else "FAIL", label,
                           ("  — " + detail) if detail else ""))

    db = cfg_path("BRIDGE_DB")
    line(os.path.exists(db), "messages.db", db)
    line(bool(api_key()), "API_KEY", "from " + (cfg_path("BRIDGE_ENV_FILE") or "env"))
    try:
        conn = _get("/api/connection")
        line(bool(conn.get("connected")), "bridge connected",
             "jid=%s" % conn.get("jid", "?"))
    except Exception as e:  # noqa: BLE001
        line(False, "bridge reachable", str(e))
    jids = self_jids()
    line(bool(jids), "self jids", ", ".join(jids) or "none — set SELF_JIDS")
    claude = cfg("CLAUDE_BIN")
    which = subprocess.run(["/bin/sh", "-c", "command -v %s" % claude],
                           capture_output=True, text=True)
    line(which.returncode == 0, "claude binary", which.stdout.strip() or claude)
    line(True, "workdir", cfg_path("WORKDIR"))
    for feature, key in (("speech in", "STT_ENABLED"), ("speech out", "TTS_ENABLED"),
                         ("images", "IMAGE_ENABLED")):
        print("     %s: %s" % (feature, "on" if cfg_bool(key) else "off"))
    print("\n%s" % ("Ready." if ok else "Fix the FAIL lines above, then re-run --check."))
    return 0 if ok else 1


def handle(row, session: WarmSession, last_image: list) -> None:
    rowid, mid, chat_jid, mtype, filename, content = row
    speak = False

    if mtype == "audio":
        if str(mid) in SENT_AUDIO:
            return
        content = transcribe(filename, chat_jid, str(mid))
        if not content:
            return
        speak = True
        log("voice: %s" % content[:60])
    elif mtype == "image":
        if not cfg_bool("IMAGE_ENABLED") or not cfg("IMAGE_PROMPT"):
            return
        # Photos arrive one message per image; debounce so a burst of six is
        # one task, not six.
        now = time.time()
        if now - last_image[0] < cfg_int("IMAGE_DEBOUNCE"):
            last_image[0] = now
            return
        last_image[0] = now
        threading.Thread(target=run_heavy,
                         args=(cfg("IMAGE_PROMPT"), chat_jid, False), daemon=True).start()
        return
    elif content.startswith(MARKER):
        return  # our own reply
    else:
        prefix = cfg("TTS_PREFIX")
        if prefix and content.startswith(prefix):
            speak, content = True, content[len(prefix):].strip()

    if not content:
        return
    log("in: %s%s" % ("[voice] " if speak else "", content[:60]))

    if is_heavy(content):
        threading.Thread(target=run_heavy, args=(content, chat_jid, speak),
                         daemon=True).start()
        return

    acked = {"done": False}

    def ack():
        if not acked["done"]:
            send_text(cfg("ACK_TEXT"), chat_jid)

    timer = threading.Timer(float(cfg_int("ACK_AFTER_SECONDS")), ack)
    timer.daemon = True
    timer.start()
    reply = session.ask(content) or "(hiccup — try that again?)"
    acked["done"] = True
    timer.cancel()
    deliver(reply, chat_jid, speak)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="validate setup and exit")
    ap.add_argument("--once", action="store_true", help="handle one message and exit")
    args = ap.parse_args(argv)

    if args.check:
        return preflight()

    jids = self_jids()
    if not jids:
        log("no self jids — set SELF_JIDS in config.env or start the bridge")
        return 2
    log("watching %s" % ", ".join(jids))

    db = cfg_path("BRIDGE_DB")
    in_clause = ",".join("?" * len(jids))

    if not os.path.exists(state_file()):
        # Start from now, so a first run does not replay months of your own
        # notes back at you.
        conn = sqlite3.connect(db)
        maxid = conn.execute(
            "SELECT COALESCE(MAX(rowid),0) FROM messages WHERE chat_jid IN (%s)" % in_clause,
            jids).fetchone()[0]
        conn.close()
        set_last(maxid or 0)
        log("starting from rowid %s" % (maxid or 0))

    session = WarmSession()
    last_image = [0.0]
    poll = cfg_int("POLL_SECONDS")

    while True:
        try:
            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT rowid, id, chat_jid, COALESCE(media_type,''), "
                "COALESCE(filename,''), COALESCE(content,'') FROM messages "
                "WHERE is_from_me=1 AND rowid>? AND chat_jid IN (%s) "
                "AND ((content IS NOT NULL AND content<>'') OR media_type IN ('audio','image')) "
                "ORDER BY rowid ASC LIMIT 1" % in_clause,
                [get_last()] + jids).fetchone()
            conn.close()
            if row:
                try:
                    handle(row, session, last_image)
                finally:
                    # Advance regardless: a message that crashes the handler
                    # must not be retried forever.
                    set_last(row[0])
                if args.once:
                    return 0
        except Exception as e:  # noqa: BLE001 - the daemon must outlive any one error
            log("loop error: %s" % e)
        time.sleep(poll)


if __name__ == "__main__":
    sys.exit(main())

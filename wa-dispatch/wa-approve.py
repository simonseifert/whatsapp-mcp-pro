#!/usr/bin/env python3
"""wa-approve — approve drafted WhatsApp replies from your phone.

Runs on M1 (always-on, next to the bridge and ntfy). A Claude session that has
drafted a reply POSTs it here; the service pushes an ntfy notification carrying
the draft text plus Send / Discard action buttons. Tapping Send makes the phone
call back here, and this service relays the message through the local bridge.

Why a service rather than letting the model send directly: the deny-hook blocks
every outbound WhatsApp tool in prepare-ahead mode, deliberately. This keeps
that true — the model can only ever *propose*. The send happens here, after a
human tapped a button, and can only send the exact text that was reviewed to
the exact chat it was drafted for.

Security posture:
  * binds the tailnet address only (never 0.0.0.0, never public)
  * approval token is a 32-byte capability, single-use, expiring
  * chat_jid and text are frozen at draft time — approving cannot redirect the
    message or alter its content
  * the bridge API key never leaves this host

Endpoints:
  POST /draft            {chat_jid, chat_name, text, note?}  -> {token, url}
  POST /approve/<token>  send it (single use)
  POST /discard/<token>  drop it
  GET  /d/<token>        read-only view (browser-friendly)
  GET  /health
"""
import json
import os
import secrets
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wa_config as cfg  # noqa: E402

STORE = os.path.expanduser(
    os.environ.get("WA_APPROVE_STORE", "~/.local/share/wa-dispatch/drafts.json"))
BRIDGE = cfg.get("BRIDGE_API")
NTFY = cfg.get("NTFY_URL")
BIND = cfg.get("APPROVE_BIND")
PORT = cfg.num("APPROVE_PORT")
PUBLIC = cfg.get("APPROVE_PUBLIC_URL") or "http://%s:%d" % (BIND, PORT)
TTL_HOURS = cfg.num("APPROVE_TTL_HOURS")

_lock = threading.Lock()


API_KEY = cfg.bridge_api_key()
NTFY_TOPIC = cfg.read_secret("NTFY_TOPIC_FILE")
NTFY_TOKEN = cfg.read_secret("NTFY_TOKEN_FILE")


def log(msg):
    print("[wa-approve] %s" % msg, flush=True)


def _ascii(s):
    """Header-safe text. HTTP headers are latin-1; anything outside it (emoji,
    arrows, accented names) raises and costs the whole notification."""
    return (s or "").encode("ascii", "replace").decode("ascii")


# ---------- draft store ----------
def _load():
    try:
        with open(STORE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d):
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, STORE)
    os.chmod(STORE, 0o600)


def _prune(d):
    cutoff = time.time() - TTL_HOURS * 3600
    return {k: v for k, v in d.items() if v.get("created", 0) > cutoff}


def put_draft(rec):
    """Store a draft and return (draft_id, approval_token).

    These are deliberately DIFFERENT secrets. The draft_id goes back to whoever
    submitted the draft — i.e. the model — so it can check status. The approval
    token is embedded only in the ntfy action URLs that reach the phone, and is
    never returned over the API. Without this split, a session could submit a
    draft, read the token out of the response, and approve its own send: the
    human gate would be decorative.
    """
    with _lock:
        d = _prune(_load())
        draft_id = secrets.token_urlsafe(12)
        token = secrets.token_urlsafe(32)
        rec["created"] = time.time()
        rec["state"] = "pending"
        rec["draft_id"] = draft_id
        d[token] = rec
        _save(d)
        return draft_id, token


def find_by_draft_id(draft_id):
    with _lock:
        for rec in _load().values():
            if rec.get("draft_id") == draft_id:
                return rec
    return None


def take_draft(token, new_state):
    """Atomically claim a pending draft. Returns the record, or None."""
    with _lock:
        d = _load()
        rec = d.get(token)
        if not rec or rec.get("state") != "pending":
            return None
        if rec.get("created", 0) < time.time() - TTL_HOURS * 3600:
            return None
        rec["state"] = new_state
        rec["acted"] = time.time()
        _save(d)
        return rec


def peek_draft(token):
    with _lock:
        return _load().get(token)


# ---------- outbound ----------
def notify(rec, token):
    """Push the draft to the phone with Send / Discard buttons."""
    if not (NTFY_TOPIC and NTFY_TOKEN):
        log("ntfy not configured — skipping push")
        return
    approve = "%s/approve/%s" % (PUBLIC, token)
    discard = "%s/discard/%s" % (PUBLIC, token)
    # HTTP headers are latin-1 only: arrows, emoji and accented client names all
    # raise UnicodeEncodeError and silently cost you the notification. Body is
    # sent as UTF-8 bytes and is unaffected, so the draft text keeps its emoji.
    actions = "http, Send, %s, method=POST, clear=true; http, Discard, %s, method=POST, clear=true" % (
        approve, discard,
    )
    body = rec["text"]
    if rec.get("note"):
        body += "\n\n-- %s" % rec["note"]
    headers = {
        "Title": _ascii("Draft reply to %s" % (rec.get("chat_name") or "WhatsApp"))[:100],
        "Priority": "default",
        "Tags": "memo",
        "Actions": _ascii(actions),
        "Authorization": "Bearer " + NTFY_TOKEN,
        "Content-Type": "text/plain; charset=utf-8",
    }
    url = "%s/%s" % (NTFY, NTFY_TOPIC)
    try:
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers)
        urllib.request.urlopen(req, timeout=10)
        log("pushed draft %s… to ntfy" % token[:8])
    except Exception as e:
        log("ntfy push failed: %s" % e)


def send_via_bridge(chat_jid, text):
    payload = json.dumps({"recipient": chat_jid, "message": text}).encode("utf-8")
    req = urllib.request.Request(
        BRIDGE + "/api/send",
        data=payload,
        headers={"Content-Type": "application/json", "X-API-Key": API_KEY},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode("utf-8", "replace")[:400]


def ntfy_say(msg, tags="white_check_mark"):
    if not (NTFY_TOPIC and NTFY_TOKEN):
        return
    try:
        req = urllib.request.Request(
            "%s/%s" % (NTFY, NTFY_TOPIC),
            data=msg.encode("utf-8"),
            headers={"Authorization": "Bearer " + NTFY_TOKEN, "Tags": tags,
                     "Priority": "low"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


# ---------- http ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "wa-approve"

    def log_message(self, fmt, *args):
        log("%s %s" % (self.address_string(), fmt % args))

    def _reply(self, code, body, ctype="text/plain; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            return self._reply(200, "ok")
        if self.path.startswith("/d/"):
            # Status lookup by draft_id (safe to hand back to the submitter).
            # Falls back to the approval token so the phone's own link works.
            key = self.path[3:]
            rec = find_by_draft_id(key) or peek_draft(key)
            if not rec:
                return self._reply(404, "no such draft")
            return self._reply(200, "to: %s\nstate: %s\n\n%s\n" % (
                rec.get("chat_name"), rec.get("state"), rec.get("text")))
        return self._reply(404, "not found")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""

        if self.path == "/draft":
            try:
                rec = json.loads(raw)
                assert rec.get("chat_jid") and rec.get("text")
            except Exception:
                return self._reply(400, "need json {chat_jid, text, chat_name?, note?}")
            rec = {k: rec.get(k) for k in ("chat_jid", "chat_name", "text", "note")}
            draft_id, token = put_draft(rec)
            notify(rec, token)
            # Return the draft_id only — never the approval token (see put_draft).
            return self._reply(200, json.dumps({
                "draft_id": draft_id,
                "status": "pending_approval",
                "detail": "pushed to Simon's phone; only he can approve the send",
            }) + "\n", "application/json")

        if self.path.startswith("/approve/"):
            token = self.path[len("/approve/"):]
            rec = take_draft(token, "sent")
            if not rec:
                return self._reply(410, "draft already used, expired, or unknown")
            try:
                code, body = send_via_bridge(rec["chat_jid"], rec["text"])
            except Exception as e:
                log("send failed: %s" % e)
                ntfy_say("Send FAILED to %s: %s" % (rec.get("chat_name"), e), "x")
                return self._reply(502, "send failed: %s" % e)
            log("sent draft %s… -> %s (bridge %s)" % (token[:8], rec["chat_jid"], code))
            ntfy_say("Sent to %s" % (rec.get("chat_name") or rec["chat_jid"]))
            return self._reply(200, "sent\n")

        if self.path.startswith("/discard/"):
            token = self.path[len("/discard/"):]
            rec = take_draft(token, "discarded")
            if not rec:
                return self._reply(410, "draft already used, expired, or unknown")
            log("discarded draft %s…" % token[:8])
            return self._reply(200, "discarded\n")

        return self._reply(404, "not found")


def main():
    if not API_KEY:
        log("FATAL: bridge API key not found")
        sys.exit(1)
    log("bind %s:%d  bridge=%s  ntfy=%s  ttl=%dh"
        % (BIND, PORT, BRIDGE, "on" if NTFY_TOPIC else "off (drafts queue silently)", TTL_HOURS))
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

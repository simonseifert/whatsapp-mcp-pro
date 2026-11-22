"""A tiny stand-in for the bridge's REST API, for e2e only: /api/send and /api/reaction, plus /__stub control routes.

Usage: stub_bridge.py <messages.db> <port file>. Binds a free port and writes it to the port file. A successful send
is written into the fictional store as the real bridge would (so the app's live poller sees it), and a repeated
idempotency key answers with the first message again instead of sending twice. Fictional data only.
"""

import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB = sys.argv[1]
state: dict = {"log": [], "fail": [], "delay": 0.0, "seen": {}, "n": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # quiet
        pass

    def _json(self, status: int, body: object) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/__stub/log":
            self._json(200, state["log"])
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        body = self._body()
        if self.path == "/__stub/reset":
            state.update(log=[], fail=[], delay=0.0, seen={})  # n keeps counting: moved rows keep their ids
            return self._json(200, {"ok": True})
        if self.path == "/__stub/fail":  # {status, code, error, count}: the next `count` sends fail like that
            state["fail"] = [body] * int(body.get("count", 1))
            return self._json(200, {"ok": True})
        if self.path == "/__stub/delay":
            state["delay"] = float(body.get("seconds", 0))
            return self._json(200, {"ok": True})
        if self.path not in ("/api/send", "/api/reaction"):
            return self._json(404, {"error": "not found"})
        state["log"].append({"path": self.path, **body})
        time.sleep(state["delay"])
        if state["fail"]:
            rule = state["fail"].pop(0)
            return self._json(
                int(rule["status"]), {"success": False, "error": rule.get("error", "refused"), "code": rule.get("code")}
            )
        if self.path == "/api/reaction":
            return self._json(200, {"success": True, "message": "ok"})
        key = body.get("idempotency_key")
        if key in state["seen"]:
            return self._json(200, {**state["seen"][key], "idempotent_replay": True})
        state["n"] += 1
        mid = f"STUB{state['n']:04d}"
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S+00:00")
        conn = sqlite3.connect(DB)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, quoted_message_id)"
            " VALUES (?,?,?,?,?,1,?)",
            (mid, body["recipient"], "me", body["message"], now, body.get("quoted_message_id")),
        )
        conn.commit()
        conn.close()
        answer = {"success": True, "message_id": mid, "timestamp": now, "recipient": body["recipient"]}
        state["seen"][key] = answer
        self._json(200, answer)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
Path(sys.argv[2]).write_text(str(server.server_address[1]))
server.serve_forever()

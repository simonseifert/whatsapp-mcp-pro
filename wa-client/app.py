"""wa-client: self-hosted WhatsApp web client riding the bridge session.

Because it reuses the whatsapp-bridge device session, it costs ZERO extra
WhatsApp linked-device slots — open it in as many browsers as you like.
Reads history straight from the bridge's messages.db (read-only) and sends
through the bridge REST API.

Push: the bridge fires an HMAC-signed webhook at /api/webhook on new
messages; connected browsers hear about it over SSE (/api/events) and fetch
deltas immediately instead of polling.

Scheduled sends: /api/schedule queues messages in scheduled.db; a background
loop delivers them when due.

Configuration (env):
    WA_MCP_REPO      repo root (default: parent of this file's directory)
    WA_CLIENT_DATA   dir for scheduled.db + .webhook-secret (default: app dir)
    WA_WEB_HOST      bind address (default 127.0.0.1 — use a VPN/tailnet IP
                     to reach it from other devices; do NOT expose publicly,
                     the client itself has no login)
    WA_WEB_PORT      port (default 8084)
    WA_BRIDGE_URL    bridge REST base (default http://127.0.0.1:8080/api)
    WA_MCP_URL       optional MCP endpoint for semantic search via `recall`

Deps come from whatsapp-mcp-server's venv (starlette/uvicorn/httpx).
"""

import asyncio
import contextlib
import hashlib
import hmac
import os
import secrets as pysecrets
import sqlite3
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

APP_DIR = Path(__file__).resolve().parent
BASE = Path(os.environ.get("WA_MCP_REPO", APP_DIR.parent))
DB = BASE / "whatsapp-bridge" / "store" / "messages.db"
# 127.0.0.1 explicitly rather than localhost: the bridge binds IPv4 only and
# macOS resolves localhost to ::1 first.
BRIDGE = os.environ.get("WA_BRIDGE_URL", "http://127.0.0.1:8080/api")
STATIC = APP_DIR / "static"
DATA_DIR = Path(os.environ.get("WA_CLIENT_DATA", APP_DIR))
SCHED_DB = DATA_DIR / "scheduled.db"
UPLOADS = Path(os.environ.get("TMPDIR", "/tmp")) / "wa-client-uploads"
HOST = os.environ.get("WA_WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("WA_WEB_PORT", "8084"))
MCP_URL = os.environ.get("WA_MCP_URL", "")

MSG_COLS = (
    "m.rowid AS rowid, m.id, m.chat_jid, m.sender, m.sender_name, m.content, "
    "m.timestamp, m.is_from_me, m.media_type, m.filename"
)


def _env_value(key: str) -> str:
    for line in (BASE / ".env").read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def _api_key() -> str:
    return _env_value("API_KEY")


def _webhook_secret() -> str:
    f = DATA_DIR / ".webhook-secret"
    if f.exists():
        return f.read_text().strip()
    secret = pysecrets.token_hex(32)
    f.write_text(secret)
    f.chmod(0o600)
    return secret


HEADERS = {"X-API-Key": _api_key()}
WEBHOOK_SECRET = _webhook_secret()
client = httpx.AsyncClient(timeout=60)

# ---------- SSE push ----------

_subscribers: set[asyncio.Queue] = set()


def _notify(event: str = "update") -> None:
    for q in list(_subscribers):
        try:
            q.put_nowait(event)
        except Exception:
            pass


async def webhook(request):
    body = await request.body()
    sig = request.headers.get("X-Webhook-Signature", "")
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return JSONResponse({"success": False, "error": "bad signature"}, status_code=403)
    _notify()
    return JSONResponse({"success": True})


async def events(request):
    async def gen():
        q: asyncio.Queue = asyncio.Queue()
        _subscribers.add(q)
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------- read endpoints ----------


def q(sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


async def index(request):
    return FileResponse(STATIC / "index.html")


async def chats(request):
    rows = q(
        """
        SELECT c.jid, c.name, c.last_message_time,
               m.content AS last_content, m.media_type AS last_media_type,
               m.is_from_me AS last_from_me, m.sender_name AS last_sender_name
        FROM chats c
        LEFT JOIN messages m ON m.chat_jid = c.jid AND m.timestamp = (
            SELECT MAX(timestamp) FROM messages WHERE chat_jid = c.jid)
        WHERE c.last_message_time IS NOT NULL
          AND c.jid != 'status@broadcast'
        GROUP BY c.jid
        ORDER BY c.last_message_time DESC
        LIMIT 200
        """
    )
    top = q("SELECT COALESCE(MAX(rowid), 0) AS r FROM messages")
    return JSONResponse({"chats": rows, "max_rowid": top[0]["r"]})


async def messages(request):
    jid = request.query_params.get("chat_jid", "")
    before = request.query_params.get("before")
    limit = min(int(request.query_params.get("limit", "60")), 200)
    if before:
        rows = q(
            f"SELECT {MSG_COLS} FROM messages m WHERE m.chat_jid = ? AND m.timestamp < ? "
            "ORDER BY m.timestamp DESC LIMIT ?",
            (jid, before, limit),
        )
    else:
        rows = q(
            f"SELECT {MSG_COLS} FROM messages m WHERE m.chat_jid = ? "
            "ORDER BY m.timestamp DESC LIMIT ?",
            (jid, limit),
        )
    rows.reverse()
    return JSONResponse({"messages": rows})


async def updates(request):
    since = int(request.query_params.get("since", "0"))
    rows = q(
        f"SELECT {MSG_COLS} FROM messages m WHERE m.rowid > ? ORDER BY m.rowid ASC LIMIT 300",
        (since,),
    )
    max_rowid = rows[-1]["rowid"] if rows else since
    return JSONResponse({"messages": rows, "max_rowid": max_rowid})


# ---------- write endpoints (bridge proxied) ----------


async def send(request):
    body = await request.json()
    r = await client.post(
        f"{BRIDGE}/send",
        json={"recipient": body.get("recipient"), "message": body.get("message")},
        headers=HEADERS,
    )
    _notify()
    return JSONResponse(r.json(), status_code=r.status_code)


async def sendfile(request):
    form = await request.form()
    recipient = form.get("recipient")
    up = form.get("file")
    if not recipient or up is None:
        return JSONResponse({"success": False, "error": "recipient and file required"}, status_code=400)
    UPLOADS.mkdir(exist_ok=True)
    # Basename only: an uploaded filename must not become a path.
    safe_name = Path(up.filename or "upload.bin").name
    dest = UPLOADS / safe_name
    dest.write_bytes(await up.read())
    r = await client.post(
        f"{BRIDGE}/send",
        json={"recipient": recipient, "media_path": str(dest)},
        headers=HEADERS,
    )
    _notify()
    return JSONResponse(r.json(), status_code=r.status_code)


async def media(request):
    message_id = request.query_params.get("message_id", "")
    chat_jid = request.query_params.get("chat_jid", "")
    r = await client.post(
        f"{BRIDGE}/download",
        json={"message_id": message_id, "chat_jid": chat_jid},
        headers=HEADERS,
    )
    data = r.json()
    path = data.get("path") or data.get("file_path") or ""
    p = Path(path) if path else None
    # The post-June bridge returns paths relative to its own working dir.
    if p is not None and not p.is_absolute():
        p = BASE / "whatsapp-bridge" / p
    resolved = p.resolve() if p else None
    # Serve only files the bridge wrote inside its own tree.
    if not resolved or not resolved.is_file() or not str(resolved).startswith(str(BASE)):
        return JSONResponse({"success": False, "error": data.get("message", "download failed")}, status_code=404)
    return FileResponse(resolved, filename=resolved.name)


async def mark_read(request):
    body = await request.json()
    r = await client.post(f"{BRIDGE}/read", json=body, headers=HEADERS)
    return JSONResponse(r.json(), status_code=r.status_code)


# ---------- dispatch to a Claude session ----------
# The Claude sessions live on another machine that cannot be reached from here,
# so this only *records* the request. wa-dispatch already polls this host over
# SSH for new messages and picks these up on the same cycle, which avoids
# opening an inbound path back to the laptop.

DISPATCH_REQUESTS = os.path.expanduser("~/.local/share/wa-dispatch/requests.jsonl")


async def dispatch_request(request):
    import json as _json

    body = await request.json()
    chat_jid = (body or {}).get("chat_jid")
    if not chat_jid:
        return JSONResponse({"ok": False, "error": "chat_jid required"}, status_code=400)
    rec = {
        "chat_jid": chat_jid,
        "chat_name": (body or {}).get("chat_name") or "",
        "limit": int((body or {}).get("limit") or 25),
    }
    os.makedirs(os.path.dirname(DISPATCH_REQUESTS), exist_ok=True)
    with open(DISPATCH_REQUESTS, "a") as f:
        f.write(_json.dumps(rec) + "\n")
    return JSONResponse({"ok": True, "queued": rec})


# ---------- scheduled sends ----------


def sq(sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(SCHED_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient TEXT NOT NULL,
                recipient_name TEXT,
                message TEXT NOT NULL,
                send_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                sent_at TEXT
            )
            """
        )
        cur = conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
        return rows
    finally:
        conn.close()


async def schedule_create(request):
    body = await request.json()
    recipient, message, send_at = body.get("recipient"), body.get("message"), body.get("send_at")
    if not recipient or not message or not send_at:
        return JSONResponse({"success": False, "error": "recipient, message, send_at required"}, status_code=400)
    try:
        when = datetime.fromisoformat(send_at)
    except ValueError:
        return JSONResponse({"success": False, "error": "send_at must be ISO datetime"}, status_code=400)
    if when <= datetime.now():
        return JSONResponse({"success": False, "error": "send_at is in the past"}, status_code=400)
    sq(
        "INSERT INTO scheduled (recipient, recipient_name, message, send_at) VALUES (?, ?, ?, ?)",
        (recipient, body.get("recipient_name", ""), message, when.isoformat(sep=" ", timespec="seconds")),
    )
    return JSONResponse({"success": True})


async def schedule_list(request):
    rows = sq(
        "SELECT * FROM scheduled WHERE status IN ('pending', 'error') ORDER BY send_at ASC LIMIT 100"
    )
    return JSONResponse({"scheduled": rows})


async def schedule_delete(request):
    sid = request.query_params.get("id")
    if not sid:
        return JSONResponse({"success": False, "error": "id required"}, status_code=400)
    sq("DELETE FROM scheduled WHERE id = ? AND status IN ('pending', 'error')", (sid,))
    return JSONResponse({"success": True})


async def _scheduler_loop() -> None:
    while True:
        try:
            now = datetime.now().isoformat(sep=" ", timespec="seconds")
            due = sq(
                "SELECT * FROM scheduled WHERE status = 'pending' AND send_at <= ? LIMIT 10",
                (now,),
            )
            for job in due:
                try:
                    r = await client.post(
                        f"{BRIDGE}/send",
                        json={"recipient": job["recipient"], "message": job["message"]},
                        headers=HEADERS,
                    )
                    data = r.json()
                    if r.status_code == 200 and data.get("success"):
                        sq(
                            "UPDATE scheduled SET status='sent', sent_at=? WHERE id=?",
                            (now, job["id"]),
                        )
                    else:
                        sq(
                            "UPDATE scheduled SET status='error', error=? WHERE id=?",
                            (str(data.get("error") or data.get("message") or r.status_code), job["id"]),
                        )
                except Exception as exc:
                    sq("UPDATE scheduled SET status='error', error=? WHERE id=?", (str(exc), job["id"]))
                _notify("scheduled")
        except Exception:
            pass
        await asyncio.sleep(20)


# ---------- search ----------


async def search(request):
    query = request.query_params.get("q", "").strip()
    mode = request.query_params.get("mode", "keyword")
    if not query:
        return JSONResponse({"results": []})
    if mode == "semantic":
        if not MCP_URL:
            return JSONResponse({"results": [], "error": "semantic search not configured (set WA_MCP_URL)"})
        try:
            import json as _json

            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            mcp_token = _env_value("WA_MCP_READONLY_TOKEN")
            mcp_headers = {"Authorization": f"Bearer {mcp_token}"} if mcp_token else None
            async with streamablehttp_client(MCP_URL, headers=mcp_headers) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    res = await s.call_tool("recall", {"query": query, "limit": 20})
                    data = _json.loads(res.content[0].text)
            results = [
                {
                    "chat_jid": m.get("chat_jid"),
                    "chat_name": m.get("chat_name"),
                    "sender_name": m.get("sender"),
                    "content": m.get("content"),
                    "timestamp": m.get("timestamp"),
                    "similarity": m.get("similarity"),
                }
                for m in data.get("results", [])
            ]
            return JSONResponse({"results": results})
        except Exception as exc:
            return JSONResponse({"results": [], "error": f"semantic search unavailable: {exc}"})
    rows = q(
        """
        SELECT m.chat_jid, c.name AS chat_name, m.sender_name, m.content, m.timestamp
        FROM messages m LEFT JOIN chats c ON c.jid = m.chat_jid
        WHERE m.content LIKE ? ESCAPE '\\'
        ORDER BY m.timestamp DESC LIMIT 50
        """,
        ("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
    )
    return JSONResponse({"results": rows})


@contextlib.asynccontextmanager
async def _lifespan(app):
    task = asyncio.create_task(_scheduler_loop())
    yield
    task.cancel()


app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/chats", chats),
        Route("/api/messages", messages),
        Route("/api/updates", updates),
        Route("/api/events", events),
        Route("/api/webhook", webhook, methods=["POST"]),
        Route("/api/send", send, methods=["POST"]),
        Route("/api/sendfile", sendfile, methods=["POST"]),
        Route("/api/media", media),
        Route("/api/read", mark_read, methods=["POST"]),
        Route("/api/schedule", schedule_create, methods=["POST"]),
        Route("/api/schedule", schedule_list, methods=["GET"]),
        Route("/api/schedule", schedule_delete, methods=["DELETE"]),
        Route("/api/search", search),
        Route("/api/dispatch", dispatch_request, methods=["POST"]),
    ],
    # Read-only CORS so the dashboard widget (:8888) can list chats.
    # Everything is tailnet-bound anyway; writes stay same-origin only.
    middleware=[
        Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])
    ],
    lifespan=_lifespan,
)

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")

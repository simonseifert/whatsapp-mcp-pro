"""Routes for non-browser callers (Claude through the MCP server), behind a bearer token (`/api/agent/*`).

Nothing here sends a message. A pushed draft only appears on the chat's draft card; the user taps it, edits it and
sends it themselves. A test pins that this module never imports `sending`. The token is checked by `AuthMiddleware`
before any handler runs; handlers never see or echo it.
"""

import sqlite3
from dataclasses import asdict

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import appdb, store
from .ai import config as ai_config
from .ai import drafts
from .config import Settings

MAX_TEXT = 4096


def _error(status: int, message: str) -> Response:
    return JSONResponse({"error": message}, status_code=status)


async def _body(request: Request) -> dict | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def routes(conn: sqlite3.Connection, settings: Settings, worker: drafts.DraftWorker) -> list[Route]:
    def resolve(jid: str) -> tuple[str, str] | None:
        """(canonical jid, display name) or None. Blocking: reads the bridge store."""
        ro = store.open_ro(settings)
        try:
            chat = store.get_chat(ro, conn, jid)
        finally:
            ro.close()
        return (chat.jid, chat.name) if chat else None

    async def lookup(jid: str) -> tuple[str, str] | Response:
        try:
            found = await run_in_threadpool(resolve, jid)
        except sqlite3.Error:
            return _error(503, "Message store unavailable")
        return found if found else _error(404, "Unknown chat")

    async def push_draft(request: Request) -> Response:
        body = await _body(request)
        if body is None:
            return _error(422, "Expected a JSON object")
        chat, text, reply_to = body.get("chat"), body.get("text"), body.get("reply_to")
        if not isinstance(chat, str) or not chat:
            return _error(422, "'chat' (a JID) is required")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_TEXT:
            return _error(422, f"'text' must be 1 to {MAX_TEXT} characters")
        if reply_to is not None and not isinstance(reply_to, str):
            return _error(422, "'reply_to' must be a message id")
        found = await lookup(chat)
        if isinstance(found, Response):
            return found
        jid, name = found
        try:
            view = worker.push(jid, text.strip(), reply_to)
        except ValueError as exc:
            return _error(422, str(exc))
        except sqlite3.Error:
            return _error(503, "Message store unavailable")
        return JSONResponse({"draft": asdict(view), "chat_name": name}, status_code=201)

    async def chat_ai(request: Request) -> Response:
        enabled: bool | None = None
        if request.method == "PUT":
            body = await _body(request)
            if body is None or not isinstance(body.get("enabled"), bool):
                return _error(422, "'enabled' (true or false) is required")
            enabled = body["enabled"]
        found = await lookup(request.path_params["jid"])
        if isinstance(found, Response):
            return found
        with appdb.db_lock:
            current = (
                ai_config.save_chat_ai(conn, found[0], {"enabled": enabled})
                if enabled is not None
                else ai_config.chat_ai(conn, found[0])
            )
        return JSONResponse({"ai": asdict(current)})

    return [
        Route("/api/agent/drafts", push_draft, methods=["POST"]),
        Route("/api/agent/chats/{jid}/ai", chat_ai, methods=["GET", "PUT"]),
    ]

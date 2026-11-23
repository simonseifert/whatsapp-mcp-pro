"""Routes for non-browser callers (Claude through the MCP server), behind a bearer token (`/api/agent/*`).

Nothing here sends a message. A pushed draft only appears on the chat's draft card; the user taps it, edits it and
sends it themselves. A test pins that this module never imports `sending`. The token is checked by `AuthMiddleware`
before any handler runs; handlers never see or echo it.
"""

import json
import sqlite3
from dataclasses import asdict

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import appdb, store
from .ai import config as ai_config
from .ai import drafts
from .config import Settings

MAX_TEXT = 4096
MAX_BODY = 64 * 1024  # a draft is at most 4096 characters; nothing here needs more


def _error(status: int, message: str) -> Response:
    return JSONResponse({"error": message}, status_code=status)


class _TooLarge(Exception):
    pass


async def _body(request: Request) -> dict | None:
    """The JSON object body, None when it is not one. Raises _TooLarge before parsing anything over MAX_BODY."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY:
        raise _TooLarge
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY:
            raise _TooLarge
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


_TOO_LARGE = _error(413, "Body too large")


def routes(conn: sqlite3.Connection, settings: Settings, worker: drafts.DraftWorker) -> list[Route]:
    def lookup(jid: str) -> tuple[str, str] | Response:
        """(canonical jid, display name) or a 404/503. Runs on the event loop, like the other routes' store reads."""
        try:
            ro = store.open_ro(settings)
        except sqlite3.OperationalError:
            return _error(503, "Message store unavailable")
        try:
            chat = store.get_chat(ro, conn, jid)
        except sqlite3.Error:
            return _error(503, "Message store unavailable")
        finally:
            ro.close()
        return (chat.jid, chat.name) if chat else _error(404, "Unknown chat")

    async def push_draft(request: Request) -> Response:
        try:
            body = await _body(request)
        except _TooLarge:
            return _TOO_LARGE
        if body is None:
            return _error(422, "Expected a JSON object")
        chat, text, reply_to = body.get("chat"), body.get("text"), body.get("reply_to")
        if not isinstance(chat, str) or not chat:
            return _error(422, "'chat' (a JID) is required")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_TEXT:
            return _error(422, f"'text' must be 1 to {MAX_TEXT} characters")
        if reply_to is not None and not isinstance(reply_to, str):
            return _error(422, "'reply_to' must be a message id")
        found = lookup(chat)
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
            try:
                body = await _body(request)
            except _TooLarge:
                return _TOO_LARGE
            if body is None or not isinstance(body.get("enabled"), bool):
                return _error(422, "'enabled' (true or false) is required")
            enabled = body["enabled"]
        found = lookup(request.path_params["jid"])
        if isinstance(found, Response):
            return found
        try:
            with appdb.db_lock:
                current = (
                    ai_config.save_chat_ai(conn, found[0], {"enabled": enabled})
                    if enabled is not None
                    else ai_config.chat_ai(conn, found[0])
                )
        except ai_config.ConfigError as exc:
            return _error(404 if isinstance(exc, ai_config.NotFound) else 422, str(exc))
        return JSONResponse({"ai": asdict(current)})

    async def unknown_route(request: Request) -> Response:
        # Distinct from the middleware's "Not found" (feature off), so the MCP can tell a typo from a missing token.
        return _error(404, "Unknown agent route")

    return [
        Route("/api/agent/drafts", push_draft, methods=["POST"]),
        Route("/api/agent/chats/{jid}/ai", chat_ai, methods=["GET", "PUT"]),
        Route("/api/agent/{rest:path}", unknown_route),
    ]

"""JSON API routes."""

import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from . import auth, media, store, transcripts
from .config import Settings

MEDIA_RETRY_SECONDS = 6 * 3600


def _error(status: int, message: str) -> Response:
    return JSONResponse({"error": message}, status_code=status)


async def _passphrase(request: Request) -> str | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    value = body.get("passphrase") if isinstance(body, dict) else None
    return value if isinstance(value, str) else None


def routes(conn: sqlite3.Connection, settings: Settings) -> list[Route]:
    def start_session(request: Request) -> Response:
        token = auth.new_session(conn, request.headers.get("user-agent"))
        response = JSONResponse({"ok": True})
        response.set_cookie(
            auth.COOKIE,
            token,
            max_age=auth.SESSION_SECONDS,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="strict",
            path="/",
        )
        return response

    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def state(request: Request) -> Response:
        return JSONResponse(
            {
                "setup_needed": not auth.passphrase_is_set(conn),
                "logged_in": auth.session_valid(conn, request.cookies.get(auth.COOKIE)),
            }
        )

    async def setup(request: Request) -> Response:
        passphrase = await _passphrase(request)
        # No await below: the check and the insert run atomically on the event loop.
        if auth.passphrase_is_set(conn):
            return _error(409, "Already set up")
        if passphrase is None:
            return _error(400, "Passphrase required")
        try:
            inserted = auth.set_passphrase(conn, passphrase)
        except ValueError as exc:
            return _error(400, str(exc))
        return JSONResponse({"ok": True}) if inserted else _error(409, "Already set up")

    async def login(request: Request) -> Response:
        passphrase = await _passphrase(request)
        # No await below: limit check, verification and failure record are atomic on the event loop,
        # so parallel requests cannot all slip past the limit before any failure is recorded.
        ip = auth.client_ip(request)
        if auth.login_blocked(conn, ip):
            return _error(429, "Too many attempts, try again later")
        if passphrase is None or not auth.check_passphrase(conn, passphrase):
            auth.record_failure(conn, ip)
            return _error(401, "Wrong passphrase")
        return start_session(request)

    async def logout(request: Request) -> Response:
        auth.end_session(conn, request.cookies.get(auth.COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(auth.COOKIE, path="/", secure=settings.secure_cookies, httponly=True, samesite="strict")
        return response

    async def chats(request: Request) -> Response:
        chat_filter = request.query_params.get("filter", "all")
        if chat_filter not in store.FILTERS:
            return _error(400, "Unknown filter")
        archived = request.query_params.get("archived", "").lower() in ("1", "true")
        query = request.query_params.get("q")
        try:
            ro = store.open_ro(settings)
        except sqlite3.OperationalError:
            return _error(503, "Message store unavailable")
        try:
            listed, archived_count = store.list_chats_with_count(
                ro,
                conn,
                filter=chat_filter,  # type: ignore[arg-type]
                archived=archived,
                query=query,
            )
            return JSONResponse({"chats": [asdict(c) for c in listed], "archived_count": archived_count})
        except sqlite3.Error:
            return _error(503, "Message store unavailable")
        finally:
            ro.close()

    def with_store(work):  # noqa: ANN001, ANN202
        try:
            ro = store.open_ro(settings)
        except sqlite3.OperationalError:
            return _error(503, "Message store unavailable")
        try:
            return work(ro)
        except sqlite3.Error:
            return _error(503, "Message store unavailable")
        finally:
            ro.close()

    async def chat_detail(request: Request) -> Response:
        jid = request.path_params["jid"]

        def work(ro: sqlite3.Connection) -> Response:
            chat = store.get_chat(ro, conn, jid)
            if chat is None:
                return _error(404, "Chat not found")
            return JSONResponse({"chat": asdict(chat), "members_line": store.members_line(ro, chat.jid)})

        return with_store(work)

    async def chat_messages(request: Request) -> Response:
        jid = request.path_params["jid"]
        before = request.query_params.get("before")
        if before is not None and store.parse_ts(before) is None:
            return _error(400, "Bad 'before' timestamp")

        def work(ro: sqlite3.Connection) -> Response:
            messages, has_more = store.list_messages_page(ro, jid, before_ts=before)
            return JSONResponse({"messages": [asdict(m) for m in messages], "has_more": has_more})

        return with_store(work)

    def served(path: Path) -> Response:
        ctype = media.content_type(path)
        inline = media.is_inline(ctype)
        return FileResponse(
            path,
            media_type=ctype,
            filename=path.name,
            content_disposition_type="inline" if inline else "attachment",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "Cache-Control": "private, max-age=3600",
            },
        )

    async def media_file(request: Request) -> Response:
        chat, mid = request.path_params["chat"], request.path_params["id"]
        retry = request.query_params.get("retry") == "1"
        if retry:
            # One phone ping per message per 6 hours; recorded before asking so a failed ask counts too.
            now = int(time.time())
            row = conn.execute("SELECT at FROM media_retries WHERE message_id=?", (mid,)).fetchone()
            if row and now - row[0] < MEDIA_RETRY_SECONDS:
                return _error(429, "Already asked recently, try again later")
            conn.execute("INSERT OR REPLACE INTO media_retries (message_id, at) VALUES (?, ?)", (mid, now))
        path = await run_in_threadpool(media.file_for, settings, mid, chat, retry=retry)
        if path is None:
            return _error(404, "Media not available")
        return served(path)

    async def media_thumb(request: Request) -> Response:
        chat, mid = request.path_params["chat"], request.path_params["id"]
        path = await run_in_threadpool(media.file_for, settings, mid, chat)
        if path is None:
            return _error(404, "Media not available")
        thumb = await run_in_threadpool(media.thumbnail, settings, path)
        if thumb is None:
            return _error(404, "No preview")
        return served(thumb)

    async def avatar_file(request: Request) -> Response:
        path = await run_in_threadpool(media.avatar, settings, request.path_params["jid"])
        if path is None:
            return _error(404, "No picture")
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"},
        )

    async def transcript(request: Request) -> Response:
        chat, mid = request.path_params["chat"], request.path_params["id"]

        try:
            ro = store.open_ro(settings)
        except sqlite3.OperationalError:
            return _error(503, "Message store unavailable")
        try:
            media_type = store.message_media_type(ro, chat, mid)
        except sqlite3.Error:
            return _error(503, "Message store unavailable")
        finally:
            ro.close()
        if media_type not in ("audio", "ptt"):
            return _error(404, "Voice message not found")
        result = await run_in_threadpool(transcripts.get_or_make, conn, settings, mid, chat)
        if "error" in result:
            status = {
                transcripts.NO_BACKEND: 503,
                transcripts.NO_AUDIO: 404,
            }.get(result["error"], 502)
            return _error(status, result["error"])
        return JSONResponse(result)

    return [
        Route("/api/media/{chat}/{id}/thumb", media_thumb, methods=["GET"]),
        Route("/api/media/{chat}/{id}", media_file, methods=["GET"]),
        Route("/api/avatar/{jid}", avatar_file, methods=["GET"]),
        Route("/api/transcripts/{chat}/{id}", transcript, methods=["POST"]),
        Route("/api/health", health, methods=["GET"]),
        Route("/api/auth/state", state, methods=["GET"]),
        Route("/api/auth/setup", setup, methods=["POST"]),
        Route("/api/auth/login", login, methods=["POST"]),
        Route("/api/auth/logout", logout, methods=["POST"]),
        Route("/api/chats", chats, methods=["GET"]),
        Route("/api/chats/{jid}", chat_detail, methods=["GET"]),
        Route("/api/chats/{jid}/messages", chat_messages, methods=["GET"]),
    ]

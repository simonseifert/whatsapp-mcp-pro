"""JSON API routes."""

import sqlite3

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import auth
from .config import Settings


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
        if auth.passphrase_is_set(conn):
            return _error(409, "Already set up")
        passphrase = await _passphrase(request)
        if passphrase is None:
            return _error(400, "Passphrase required")
        try:
            auth.set_passphrase(conn, passphrase)
        except ValueError as exc:
            return _error(400, str(exc))
        return JSONResponse({"ok": True})

    async def login(request: Request) -> Response:
        ip = auth.client_ip(request)
        if auth.login_blocked(conn, ip):
            return _error(429, "Too many attempts, try again later")
        passphrase = await _passphrase(request)
        if passphrase is None or not auth.check_passphrase(conn, passphrase):
            auth.record_failure(conn, ip)
            return _error(401, "Wrong passphrase")
        return start_session(request)

    async def logout(request: Request) -> Response:
        auth.end_session(conn, request.cookies.get(auth.COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(auth.COOKIE, path="/", secure=settings.secure_cookies, httponly=True, samesite="strict")
        return response

    return [
        Route("/api/health", health, methods=["GET"]),
        Route("/api/auth/state", state, methods=["GET"]),
        Route("/api/auth/setup", setup, methods=["POST"]),
        Route("/api/auth/login", login, methods=["POST"]),
        Route("/api/auth/logout", logout, methods=["POST"]),
    ]

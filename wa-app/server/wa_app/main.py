"""App factory and the `wa-app` console entry point."""

import asyncio
import contextlib
import logging
import traceback
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from . import agent_api, api, appdb, auth, config, live
from .ai.drafts import DraftWorker
from .auth import AuthMiddleware
from .config import Settings

logger = logging.getLogger("wa_app")

DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


class SpaFiles(StaticFiles):
    """Static files with a fallback to index.html for client-side routes."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            return await super().get_response("index.html", scope)


def _log_worker_end(task: "asyncio.Task[None]") -> None:
    """The draft worker only ends when cancelled; anything else is a bug worth a line (type and traceback, no text:
    exception messages may quote chat content)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "draft worker stopped unexpectedly (%s)\n%s",
            type(exc).__name__,
            "\n".join(f"  {f.filename}:{f.lineno} in {f.name}" for f in traceback.extract_tb(exc.__traceback__)),
        )


def create_app(settings: Settings, dist: Path = DIST) -> Starlette:
    conn = appdb.connect(settings)

    async def api_not_found(request: Request) -> Response:
        return JSONResponse({"error": "Not found"}, status_code=404)

    hub = live.Hub()

    def _session_alive(hashed: str) -> bool:
        with appdb.db_lock:
            return auth.session_hash_valid(conn, hashed)

    poller = live.Poller(settings)
    worker = DraftWorker(settings, hub, appconn=conn)
    poller.on_events.append(worker.on_events)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):  # noqa: ANN202
        draft_task = asyncio.create_task(worker.run())
        draft_task.add_done_callback(_log_worker_end)
        try:
            async with live.running(poller, hub):
                yield
        finally:
            draft_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await draft_task
            worker.close()
        # Under uvicorn the real work is timeout_graceful_shutdown (it waits for streams before lifespan shutdown).
        hub.close()

    routes: list[Route | Mount] = [
        *live.routes(hub, _session_alive, auth.COOKIES),
        *api.routes(conn, settings, worker),
        *agent_api.routes(conn, settings, worker),
    ]
    routes.append(Route("/api/{rest:path}", api_not_found))
    if dist.is_dir():
        routes.append(Mount("/", SpaFiles(directory=dist, html=True)))
    else:

        async def no_build(request: Request) -> Response:
            return JSONResponse({"error": "Web build missing (run pnpm build in wa-app/web)"}, status_code=503)

        routes.append(Route("/{rest:path}", no_build))
    return Starlette(
        routes=routes,
        middleware=[Middleware(AuthMiddleware, conn=conn, agent_token=settings.agent_token)],
        lifespan=lifespan,
    )


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = config.load()
    logger.info(
        "wa-app on %s:%s, store %s, data %s", settings.host, settings.port, settings.store_path, settings.data_dir
    )
    uvicorn.run(
        create_app(settings), host=settings.host, port=settings.port, log_level="info", timeout_graceful_shutdown=3
    )

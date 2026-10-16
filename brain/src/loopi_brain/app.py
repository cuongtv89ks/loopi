"""FastAPI application factory."""
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from psycopg_pool import AsyncConnectionPool

from . import media
from .api.device_routes import router as device_router
from .config import Settings
from .db import make_pool
from .pipeline.jobs import open_jobs
from .web.day_routes import router as day_router
from .web.routes import router as web_router

log = logging.getLogger(__name__)


# The status page asks the model server for its models; it must answer fast or be reported as down.
STATUS_HTTP_TIMEOUT = httpx.Timeout(5.0)


def create_app(
    settings: Settings,
    pool: AsyncConnectionPool | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    owns_pool = pool is None
    active_pool = pool if pool is not None else make_pool(settings.database_url)
    owns_http = http_client is None
    http = http_client if http_client is not None else httpx.AsyncClient(timeout=STATUS_HTTP_TIMEOUT)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if owns_pool:
            await active_pool.open()
        try:
            async with open_jobs(active_pool):  # the API defers jobs and retries failed ones
                requeue = asyncio.create_task(media.requeue_stored(active_pool, settings))
                _app.state.requeue_task = requeue
                try:
                    yield
                finally:
                    requeue.cancel()
                    try:
                        with suppress(asyncio.CancelledError):
                            await requeue
                    except Exception:
                        log.exception("requeue task ended with an error")
        finally:
            if owns_http:
                await http.aclose()
            if owns_pool:
                await active_pool.close()

    app = FastAPI(title="Loopi Brain", lifespan=lifespan)
    app.state.settings = settings
    app.state.pool = active_pool
    app.state.http = http

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(device_router)
    app.include_router(web_router)
    app.include_router(day_router)
    settings.media_root.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=settings.media_root), name="media")
    app.mount(
        "/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static"
    )
    return app

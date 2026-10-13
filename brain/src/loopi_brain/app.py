"""FastAPI application factory."""
import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from . import media
from .api.device_routes import router as device_router
from .config import Settings
from .db import make_pool

log = logging.getLogger(__name__)


def create_app(settings: Settings, pool: AsyncConnectionPool | None = None) -> FastAPI:
    owns_pool = pool is None
    active_pool = pool if pool is not None else make_pool(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if owns_pool:
            await active_pool.open()
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
                if owns_pool:
                    await active_pool.close()

    app = FastAPI(title="Loopi Brain", lifespan=lifespan)
    app.state.settings = settings
    app.state.pool = active_pool
    app.include_router(device_router)
    return app

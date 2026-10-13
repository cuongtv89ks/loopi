"""FastAPI application factory."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from .api.device_routes import router as device_router
from .config import Settings
from .db import make_pool


def create_app(settings: Settings, pool: AsyncConnectionPool | None = None) -> FastAPI:
    owns_pool = pool is None
    active_pool = pool if pool is not None else make_pool(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if owns_pool:
            await active_pool.open()
        try:
            yield
        finally:
            if owns_pool:
                await active_pool.close()

    app = FastAPI(title="Loopi Brain", lifespan=lifespan)
    app.state.settings = settings
    app.state.pool = active_pool
    app.include_router(device_router)
    return app

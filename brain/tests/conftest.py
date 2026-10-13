import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

from loopi_brain.config import Settings
from loopi_brain.db import make_pool, migrate

PG_IMAGE = "pgvector/pgvector:pg17"
APP_TABLES = "device_command, item, pair_token, device"


@pytest.fixture(scope="session")
def database_url():
    with PostgresContainer(PG_IMAGE, driver=None) as pg:
        url = pg.get_connection_url()
        migrate(url)
        yield url


@pytest.fixture
async def pool(database_url):
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(f"TRUNCATE {APP_TABLES} CASCADE")
    p = make_pool(database_url)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def settings(database_url, tmp_path):
    return Settings(
        database_url=database_url,
        media_root=tmp_path / "media",
        pki_dir=tmp_path / "pki",
        min_free_bytes=0,
    )


@pytest.fixture
def pki_settings(settings):
    from loopi_brain import pki

    pki.init_pki(settings.pki_dir, ["brain.test"], ["127.0.0.1"])
    return settings

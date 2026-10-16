import httpx
import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

from loopi_brain.config import Settings
from loopi_brain.db import make_pool, migrate

PG_IMAGE = "pgvector/pgvector:pg17"
APP_TABLES = (
    "commitment, chapter, day_recap, setting, forget_log, day_job_error,"
    " moment, transcript_segment, keyframe, device_command, item, pair_token, device,"
    " procrastinate_events, procrastinate_periodic_defers, procrastinate_jobs, procrastinate_workers"
)


@pytest.fixture(autouse=True)
def _no_ambient_anthropic_key(monkeypatch):
    """Tests never read the developer's ANTHROPIC_API_KEY: a key in the environment would turn on the cloud provider."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


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


@pytest.fixture
async def jobs_open(pool):
    """The job app opened on the test pool (httpx's ASGITransport does not run the app lifespan)."""
    from loopi_brain.pipeline.jobs import open_jobs

    async with open_jobs(pool) as app:
        yield app


@pytest.fixture
async def client(pki_settings, pool, jobs_open):
    from loopi_brain.app import create_app

    app = create_app(pki_settings, pool=pool)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://brain.test"
    ) as c:
        yield c

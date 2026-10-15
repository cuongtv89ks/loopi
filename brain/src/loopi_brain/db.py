"""Database access: SQL migrations and the async connection pool."""
from importlib import resources

import psycopg
from pgvector.psycopg import register_vector_async
from procrastinate.schema import SchemaManager
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

MIGRATIONS_PACKAGE = "loopi_brain.migrations"
# Session advisory lock held for the whole migration run: api and worker start together.
MIGRATION_LOCK_ID = 7_400_550_001
# procrastinate is pinned to exactly this version in pyproject.toml. Its schema.sql is applied once, on a
# database that has none; upgrading procrastinate must apply its sql/migrations files for the versions in
# between (recorded under new names here) instead of bumping this constant.
PROCRASTINATE_SCHEMA = "procrastinate-3.10.0"


def migrate(database_url: str) -> list[str]:
    """Apply pending ``migrations/*.sql`` files in name order, then the job-queue schema.

    Returns the names applied. Concurrent callers are serialised by an advisory lock.
    """
    applied: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        # Session lock: released when this connection closes, whatever happens below.
        conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration ("
            " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {row[0] for row in conn.execute("SELECT name FROM schema_migration")}
        files = sorted(
            (f for f in resources.files(MIGRATIONS_PACKAGE).iterdir() if f.name.endswith(".sql")),
            key=lambda f: f.name,
        )
        steps = [(f.name, f.read_text()) for f in files]
        steps.append((PROCRASTINATE_SCHEMA, SchemaManager.get_schema()))
        for name, sql in steps:
            if name in done:
                continue
            with conn.transaction():
                conn.execute(sql)
                conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (name,))
            applied.append(name)
    return applied


def make_pool(database_url: str) -> AsyncConnectionPool:
    """Create a closed pool; callers open it (app lifespan, worker or test fixture).

    Every connection gets the pgvector adapters, so migrations must have run first.
    """
    return AsyncConnectionPool(
        database_url,
        open=False,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},
        configure=register_vector_async,
    )

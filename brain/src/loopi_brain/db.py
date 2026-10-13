"""Database access: SQL migrations and the async connection pool."""
from importlib import resources

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

MIGRATIONS_PACKAGE = "loopi_brain.migrations"


def migrate(database_url: str) -> list[str]:
    """Apply pending ``migrations/*.sql`` files in name order; return the names applied."""
    applied: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration ("
            " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {row[0] for row in conn.execute("SELECT name FROM schema_migration")}
        files = sorted(
            (f for f in resources.files(MIGRATIONS_PACKAGE).iterdir() if f.name.endswith(".sql")),
            key=lambda f: f.name,
        )
        for sql_file in files:
            if sql_file.name in done:
                continue
            with conn.transaction():
                conn.execute(sql_file.read_text())
                conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (sql_file.name,))
            applied.append(sql_file.name)
    return applied


def make_pool(database_url: str) -> AsyncConnectionPool:
    """Create a closed pool; callers open it (app lifespan or test fixture)."""
    return AsyncConnectionPool(
        database_url, open=False, min_size=1, max_size=10, kwargs={"row_factory": dict_row}
    )

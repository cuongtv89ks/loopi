import threading

import procrastinate
import psycopg
import pytest
from pgvector import Vector

from helpers import fresh_database, paired_device
from loopi_brain.db import PROCRASTINATE_SCHEMA, migrate

EXPECTED_TABLES = {
    "device", "pair_token", "item", "device_command", "schema_migration",
    "keyframe", "transcript_segment", "moment",
    "procrastinate_jobs", "procrastinate_events", "procrastinate_workers", "procrastinate_periodic_defers",
}


def test_migrate_is_idempotent_and_creates_schema(database_url):
    # The session fixture already migrated; a second run applies nothing.
    assert migrate(database_url) == []
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
        names = {r[0] for r in conn.execute("SELECT name FROM schema_migration")}
    assert EXPECTED_TABLES <= {r[0] for r in rows}
    assert {"001_init.sql", "002_pipeline.sql", PROCRASTINATE_SCHEMA} <= names


def test_recorded_procrastinate_schema_matches_installed_version():
    assert PROCRASTINATE_SCHEMA == f"procrastinate-{procrastinate.__version__}"


def test_item_end_before_start_is_rejected(database_url):
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "INSERT INTO device (id, name, cert_fingerprint) VALUES ('D-order', 'd', 'fp-order')"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """INSERT INTO item (id, device_id, kind, started_at, ended_at, sha256,
                                     size_bytes, raw_path, status)
                   VALUES ('I-order', 'D-order', 'video', '2026-10-14T10:01:00Z',
                           '2026-10-14T10:00:00Z', 'x', 1, 'raw/x', 'stored')"""
            )
        conn.rollback()


def test_p1_rows_become_pending_after_upgrade(database_url):
    from importlib import resources

    with fresh_database(database_url, "loopi_upgrade") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(resources.files("loopi_brain.migrations").joinpath("001_init.sql").read_text())
            conn.execute("CREATE TABLE schema_migration (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            conn.execute("INSERT INTO schema_migration (name) VALUES ('001_init.sql')")
            conn.execute("INSERT INTO device (id, name, cert_fingerprint) VALUES ('D1', 'd', 'fp')")
            conn.execute(
                "INSERT INTO item (id, device_id, kind, started_at, ended_at, sha256, size_bytes, raw_path, status)"
                " VALUES ('I1', 'D1', 'audio', now(), now(), 'x', 1, 'raw/x.opus', 'ready')"
            )
        assert migrate(url) == ["002_pipeline.sql", PROCRASTINATE_SCHEMA]
        with psycopg.connect(url) as conn:
            state = conn.execute("SELECT analysis_state, analysis_job_id FROM item WHERE id = 'I1'").fetchone()
        assert state == ("pending", None)


def test_concurrent_migrations_apply_each_step_once(database_url):
    results: list[list[str]] = []
    errors: list[BaseException] = []
    with fresh_database(database_url, "loopi_race") as url:

        def run():
            try:
                results.append(migrate(url))
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert errors == []
    assert sorted(results, key=len) == [[], ["001_init.sql", "002_pipeline.sql", PROCRASTINATE_SCHEMA]]


def test_cascading_foreign_keys_are_indexed(database_url):
    with psycopg.connect(database_url) as conn:
        indexed = {
            (r[0], r[1])
            for r in conn.execute(
                "SELECT t.relname, a.attname FROM pg_index i"
                " JOIN pg_class t ON t.oid = i.indrelid"
                " JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = i.indkey[0]"
            )
        }
    assert {("keyframe", "item_id"), ("transcript_segment", "item_id"), ("moment", "item_id"),
            ("moment", "keyframe_id"), ("moment", "transcript_segment_id")} <= indexed


async def test_pool_connections_read_and_write_vectors(pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    vec = Vector([1.0] + [0.0] * 1023)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, embedding, model_version)"
            " VALUES ('M1', %s, now(), now(), 'marker', 'x', %s, 'test')",
            (dev["device_id"], vec),
        )
        row = await (await conn.execute("SELECT embedding FROM moment WHERE id = 'M1'")).fetchone()
    assert row["embedding"].to_list()[:2] == [1.0, 0.0]

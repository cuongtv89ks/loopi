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
    "chapter", "day_recap", "commitment", "setting", "forget_log", "day_job_error",
    "eval_audio", "eval_caption", "eval_caption_score", "eval_question", "eval_answer", "eval_run",
    "eval_run_audio", "mcp_token", "vlog",
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
    applied = {"001_init.sql", "002_pipeline.sql", "003_understanding.sql", "004_retention_files.sql",
               "005_evaluation.sql", "006_mcp.sql", "007_vlog.sql", "008_ota.sql", "009_one_copy.sql"}
    assert applied | {PROCRASTINATE_SCHEMA} <= names


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
        assert migrate(url) == [
            "002_pipeline.sql", "003_understanding.sql", "004_retention_files.sql", "005_evaluation.sql",
            "006_mcp.sql", "007_vlog.sql", "008_ota.sql", "009_one_copy.sql", PROCRASTINATE_SCHEMA,
        ]
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
    assert sorted(results, key=len) == [
        [],
        ["001_init.sql", "002_pipeline.sql", "003_understanding.sql", "004_retention_files.sql", "005_evaluation.sql",
         "006_mcp.sql", "007_vlog.sql", "008_ota.sql", "009_one_copy.sql", PROCRASTINATE_SCHEMA],
    ]


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
            ("moment", "keyframe_id"), ("moment", "transcript_segment_id"),
            ("commitment", "source_moment_id"),
            ("eval_audio", "item_id"), ("eval_caption", "keyframe_id"), ("eval_caption_score", "sample_id"),
            ("eval_answer", "question_id"), ("eval_answer", "item_id"), ("eval_answer", "keyframe_id"),
            ("eval_run_audio", "run_id"), ("eval_run_audio", "sample_id")} <= indexed


def test_keyframe_has_a_plain_time_index_for_the_vlog_queries(database_url):
    # vlog/material.py reads keyframes by `ts` alone; (device_id, ts) cannot serve that on PG17 and the partial
    # (ts) WHERE caption IS NULL index does not cover captioned rows.
    with psycopg.connect(database_url) as conn:
        defs = [r[0] for r in conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'keyframe' AND indexname = 'keyframe_ts'")]
    assert len(defs) == 1
    assert defs[0].endswith("USING btree (ts)")  # no WHERE clause: it covers every keyframe


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


async def test_understanding_tables_keep_their_rules(pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, model_version)"
            " VALUES ('M1', %s, now(), now(), 'speech', 'mai đi chợ', 'test')",
            (dev["device_id"],),
        )
        await conn.execute(
            "INSERT INTO commitment (id, day, text, source_moment_id, model_version)"
            " VALUES ('C1', '2026-10-14', 'Đi chợ', 'M1', 'test')"
        )
        await conn.execute("DELETE FROM moment WHERE id = 'M1'")  # a reprocessed transcript
        row = await (await conn.execute("SELECT source_moment_id, status FROM commitment")).fetchone()
        assert row == {"source_moment_id": None, "status": "open"}  # the user's list keeps the item
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute("UPDATE commitment SET status = 'later'")
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(
                "INSERT INTO chapter (id, day, ts_start, ts_end, last_moment_at, moment_count, signature)"
                " VALUES ('CH1', '2026-10-14', now(), now() - interval '1 minute', now(), 1, 's')"
            )
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(
                "INSERT INTO forget_log (id, range_start, range_end, counts) VALUES ('F1', now(), now(), '{}')"
            )



async def test_evaluation_tables_keep_their_rules(pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO item (id, device_id, kind, started_at, ended_at, sha256, size_bytes, raw_path, status)"
            " VALUES ('I1', %s, 'audio', now(), now(), 'x', 1, 'raw/x.opus', 'ready')",
            (dev["device_id"],),
        )
        await conn.execute(
            "INSERT INTO eval_audio (id, item_id, path, lang, reference) VALUES ('A1', 'I1', 'audio/A1.opus', 'vi', 'ừ')"
        )
        row = await (await conn.execute("SELECT had_text, prefilled FROM eval_audio")).fetchone()
        assert row == {"had_text": False, "prefilled": True}
        await conn.execute("INSERT INTO eval_run (id, config) VALUES ('R1', '{}')")
        await conn.execute("INSERT INTO eval_run_audio (run_id, sample_id, segments) VALUES ('R1', 'A1', '[]')")
        bad = [
            (psycopg.errors.UniqueViolation,  # one golden clip per recording
             "INSERT INTO eval_audio (id, item_id, path, lang, reference) VALUES ('A2', 'I1', 'audio/A2.opus', 'vi', 'x')"),
            (psycopg.errors.CheckViolation, "UPDATE eval_audio SET lang = 'ja'"),  # vi, ko or en
            (psycopg.errors.CheckViolation, "UPDATE eval_audio SET reference = ' '"),  # speech needs its reference
            # "không có lời nói" has no language and no reference
            (psycopg.errors.CheckViolation, "UPDATE eval_audio SET no_speech = true"),
            (psycopg.errors.CheckViolation,
             "INSERT INTO eval_question (id, text, first_day, last_day) VALUES ('Q1', 'Hôm qua?', '2026-10-14',"
             " '2026-10-13')"),
            (psycopg.errors.CheckViolation, "INSERT INTO eval_question (id, text) VALUES ('Q2', '  ')"),
            (psycopg.errors.CheckViolation, "UPDATE eval_run SET status = 'later'"),
        ]
        for error, sql in bad:
            with pytest.raises(error):
                async with conn.transaction():
                    await conn.execute(sql)
        await conn.execute("UPDATE eval_audio SET no_speech = true, lang = NULL, reference = ''")
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM item WHERE id = 'I1'")  # forgotten: the golden clip and its results go too
        assert await (await conn.execute("SELECT id FROM eval_audio")).fetchall() == []
        assert await (await conn.execute("SELECT run_id FROM eval_run_audio")).fetchall() == []
        assert await (await conn.execute("SELECT id FROM eval_run")).fetchall() == [{"id": "R1"}]  # numbers stay

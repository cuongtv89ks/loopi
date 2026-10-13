import psycopg
import pytest

from loopi_brain.db import migrate

EXPECTED_TABLES = {"device", "pair_token", "item", "device_command", "schema_migration"}


def test_migrate_is_idempotent_and_creates_schema(database_url):
    # The session fixture already migrated; a second run applies nothing.
    assert migrate(database_url) == []
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
    assert EXPECTED_TABLES <= {r[0] for r in rows}


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

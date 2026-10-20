from importlib import resources

import psycopg
from psycopg.types.json import Jsonb

from helpers import fresh_database
from loopi_brain import store
from loopi_brain.db import migrate

BEFORE = [
    "001_init.sql", "002_pipeline.sql", "003_understanding.sql", "004_retention_files.sql", "005_evaluation.sql",
    "006_mcp.sql", "007_vlog.sql", "008_ota.sql", "009_one_copy.sql",
]


def _migrated_until_009(url: str) -> None:
    """A brain as it was before this change: every migration up to 009 applied."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("CREATE TABLE schema_migration (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        for name in BEFORE:
            conn.execute(resources.files("loopi_brain.migrations").joinpath(name).read_text())
            conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (name,))


def _retention(url: str) -> dict:
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT value FROM setting WHERE key = 'retention'").fetchone()[0]


def test_retention_is_on_by_default():
    assert store.Retention() == store.Retention(video_days=7, audio_days=7, enabled=True)


def test_the_migration_turns_retention_on_with_seven_days(database_url):
    with fresh_database(database_url, "loopi_retention_new") as url:
        _migrated_until_009(url)
        assert "010_retention_on.sql" in migrate(url)
        assert _retention(url) == {"video_days": 7, "audio_days": 7, "enabled": True}


def test_the_migration_turns_retention_on_and_keeps_the_owners_days(database_url):
    with fresh_database(database_url, "loopi_retention_kept") as url:
        _migrated_until_009(url)
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("INSERT INTO setting (key, value) VALUES ('retention', %s)",
                         (Jsonb({"video_days": 3, "audio_days": 30, "enabled": False}),))
        migrate(url)
        assert _retention(url) == {"video_days": 3, "audio_days": 30, "enabled": True}
        store.Retention.model_validate(_retention(url))  # still a valid Retention (extra="forbid")

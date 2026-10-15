# P2a — Perception Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the brain its perception pipeline: a Postgres-backed job worker that turns every uploaded segment into keyframes with English captions, Vietnamese/Korean/English transcripts and text-embedded moments, shows them on the timeline and on a new pipeline status page, and runs as two new compose services (`worker`, `models`).

**Architecture:** Procrastinate 3.10.0 runs jobs on the existing Postgres; its schema is applied by our own `db.migrate()`. One module-level job app (`pipeline/jobs.py`) is opened on the brain's psycopg pool by the API (to defer and retry jobs) and by the new `loopi-brain worker` process (to run them). Every stage is a plain async function that takes a `PipelineContext` (pool, settings, provider router, transcriber) and is wrapped by a thin Procrastinate task, so tests call stages directly. Rows store the id of their job (`item.analysis_job_id`, `keyframe.caption_job_id`), which makes dispatch idempotent: the API defers on upload (fast path) and a periodic dispatcher catches everything else. A provider router (spec §9.3) sends captions and embeddings to Ollama (MiniCPM-V 4.6, Qwen3-Embedding) and never sends images or audio to a cloud provider; ASR (Silero VAD + faster-whisper) runs in the worker.

**Tech Stack:** Python 3.13, uv, FastAPI, psycopg 3 + psycopg-pool, Procrastinate 3.10.0, pgvector 0.5 (Postgres 17 + pgvector + pg_trgm), NumPy, Pillow, faster-whisper 1.2.1 (CTranslate2, Silero VAD on onnxruntime, PyAV < 19), httpx, PyYAML, ffmpeg, Ollama 0.35.1; tests: pytest + pytest-asyncio + testcontainers, `httpx.MockTransport`.

**Spec:** `docs/superpowers/specs/2026-10-14-loopi-design.md` (§4, §9, §10.6, §12, §13, §14 P2). The device contract `docs/protocol/v1.openapi.yaml` is not touched.

**Phase 2 is split into three plans, executed in order** (the spec's P2 row is too big for one plan):
- **P2a — perception (this plan):** job worker (Procrastinate), provider router + Ollama, keyframe selection, captions (MiniCPM-V), ASR (Silero VAD + faster-whisper), moments + text embeddings, pipeline status page, timeline enrichment, compose `worker` + `models` services, real-data run and measurements.
- **P2b — understanding + UI:** hourly chapters, 23:30 recap + commitments, hybrid search + cited Q&A (SSE chat), Day page by chapters, Settings (providers view, retention, "Quên khoảng thời gian", failed jobs), 03:00 retention (video 7 days, audio after transcript), reprocess-by-day, optional Anthropic text provider.
- **P2c — evaluation + acceptance:** golden sets (ASR WER/CER vi/ko/en, caption scoring, recall@5), latency and CPU-per-day measurements, tuning, acceptance report.

**Ledger:** every ruling taken while executing goes to `docs/superpowers/ledgers/p2a-progress.md` (CLAUDE.md).

**Third-party APIs used here were checked against the installed packages** (procrastinate 3.10.0, faster-whisper 1.2.1, pgvector 0.5.1, numpy 2.5, pillow 12.3, psycopg 3.3) and a throw-away prototype of this whole plan, replayed task by task on a fresh base, passed the full suite after every task (finally 284 passed, 1 opt-in skip). Facts the code relies on:
- `App.open_async(pool=...)` is a no-op once the connector holds a pool, and `close_async()` keeps that pool when it came from outside. Re-opening on a new pool (every test, every lifespan) therefore needs a fresh `PsycopgConnector`: `open_jobs()` swaps one in with `App.replace_connector()`.
- `RetryStrategy(max_attempts=3)` compares with the attempts already made (0 on the first run): a job runs at most 4 times, then stays `failed` with `attempts = 4`. `retry_job_by_id_async(job_id, retry_at=...)` moves a `failed` job back to `todo`; it then runs once more.
- `configure_task(name, ..., connection=conn).defer_async(**kwargs)` inserts the job on our own connection, inside our transaction; a taken queueing lock raises `AlreadyEnqueued` (wrapped from a unique violation, so it needs a savepoint).
- `run_worker_async(wait=False, concurrency=1, install_signal_handlers=False, listen_notify=False)` processes jobs (including the ones deferred by the jobs it runs) and returns when none is due.
- `JobManager.get_stalled_jobs()` is async only; `retry_job(job)` puts a stalled job back to `todo`. Periodic tasks receive a `timestamp` keyword. `builtin_tasks.remove_old_jobs(context, *, max_hours, remove_failed=False)` only reads `context.app`.
- `SchemaManager.get_schema()` returns the full `schema.sql`; executed without parameters, psycopg does not interpret its `%` characters.
- faster-whisper 1.2.1 `decode_audio()` calls `av.open(..., metadata_errors=...)`, which PyAV 19 removed: PyAV is pinned `<19`. `vad.get_speech_timestamps(audio, VadOptions(...), 16000)` returns `[{"start": samples, "end": samples}]`. `WhisperModel.detect_language(audio)` returns `(lang, prob, [(lang, prob), ...])`; `Segment` has `start, end, text, avg_logprob, no_speech_prob, compression_ratio`.
- pgvector: `pgvector.psycopg.register_vector_async` works as the pool's `configure` callback (it commits its catalog query); values are written as `pgvector.Vector(list)` and read back as `Vector` (`.to_list()`).
- Ollama API: `POST /api/chat` with `stream: false`, `think: false`, `format: <JSON schema>`, base64 `images` on a message, `options.temperature`, `keep_alive`; `POST /api/embed` with `input: [...]` → `embeddings`; `GET /api/tags` → `models[].name`; `POST /api/pull` with `stream: false` → `{"status": "success"}`; errors are `{"error": "..."}` with HTTP ≥ 400. `minicpm-v4.6:1b` is in the official library.

## Global Constraints

- Spec `docs/superpowers/specs/2026-10-14-loopi-design.md` is binding (§4 principles, §9, §12, §13, §14 P2).
- Local-first: raw images and audio never leave the brain unless the matching privacy flag is `true`; cloud gets text only; default is local for every task; a backlog never escalates to cloud (§9.3, D3).
- Models (§9.3): caption `ollama` `minicpm-v4.6:1b`; asr `local` `faster-whisper/large-v3-turbo-int8` (CPU, int8); embed `ollama` `qwen3-embedding:0.6b` (1024 dims); chapter_summary `ollama` `qwen3:4b`; recap/chat `ollama` `qwen3:8b`.
- Every derived row stores the model version (§12 "Đổi model"): `model_version = "<provider>:<model>"` (stored as `keyframe.caption_model`, `moment.model_version` + `moment.embed_model`, `transcript_segment.model_version`; marker moments use `loopi:marker-v1`).
- A job is retried 3 times, then dead-lettered and shown in the UI with a reprocess button (§12) (except: a model-server outage ends a caption or embedding job without spending a retry, Tasks 5–6; the embedding run and the periodic tasks have no button because the dispatcher or the scheduler starts a new one, Tasks 3 and 9).
- Model server or cloud errors never affect ingest (§12): `PUT /v1/items` behaves exactly as in P1.
- Device contract unchanged: `docs/protocol/v1.openapi.yaml` is not modified by P2a.
- P2a deletes no recorded media (raw audio deletion is P2b retention); the only files it removes are its own JPEGs after a failed transaction (Task 4) and the smoke device's own rows and files (Task 10).
- Python ≥ 3.13, uv-locked; Postgres 17 + pgvector + pg_trgm (`pgvector/pgvector:pg17` in tests, `pgvector/pgvector:0.8.1-pg17` in compose); Procrastinate `==3.10.0`; Ollama image `ollama/ollama:0.35.1`.
- UI text Vietnamese; web POSTs need `HX-Request: true`.
- TDD, coverage ≥ 80 % for `loopi_brain`, conventional commits, never commit secrets, never prune/delete Docker resources you did not create (testcontainers' own containers are fine).
- The P1 suite (133 tests) stays green. Run everything from `brain/`: `cd brain && uv run pytest`.
- **Tasks 1–10 never run `docker compose …` or `deploy/smoke.sh`.** The worktree's `deploy/` and the live checkout's `deploy/` both resolve to compose project `deploy`, which is the owner's live stack: any compose command from here would rebuild, recreate or migrate it, or write test data into it. Verification is `bash -n deploy/smoke.sh` and `tests/test_deploy.py` (which parses the YAML with PyYAML only). Only Task 11 — the controller, after the owner's yes — touches compose.

## Review Focus

1. **A night:** hours of dark frames → zero keyframes and zero caption jobs, and every night segment still ends `done`. Tests: Task 4 (`test_night_hour_gives_no_keyframes`, `test_night_segment_gives_no_keyframes_and_no_caption_jobs`).
2. **Someone busy in front of the camera for an hour:** every frame is "new" (dHash differs) but nothing moves fast enough to be skipped → keyframes are bounded by the 30 s minimum gap (≤ 2 per minute), never one per frame. Test: Task 4 (`test_busy_hour_is_bounded_by_the_minimum_gap`).
3. **The model server is down, or the caption model is not pulled, across several dispatcher ticks:** the dispatcher defers no caption job while the server cannot serve it (no churn); a caption job already queued meets the outage, finishes without burning a retry and leaves the error on the keyframe; nothing dead-letters; an upload still gets 201; the caption arrives by itself on the first tick after the server is back. Tests: Task 3 (`test_outage_tick_defers_no_caption_job`, `test_tick_without_the_caption_model_defers_no_caption_job`, `test_healthy_tick_gives_uncaptioned_keyframes_one_caption_job`), Task 6 (`test_model_server_outage_dead_letters_nothing_and_captions_resume`, `test_missing_caption_model_waits_like_an_outage`).
4. **Whisper hallucinates on silence or TV noise** ("Hẹn gặp lại các bạn trong những video tiếp theo", "Thanks for watching", low-confidence non-speech) → no transcript row, no moment, the item is `done` and no embedding run is requested. Tests: Task 7 (`test_hallucinated_text_on_noise_leaves_no_transcript`, `test_known_hallucinations_are_dropped`).
5. **A backlog of hundreds of pending items while uploads keep arriving:** the dispatcher feeds the backlog oldest first and keeps at most `LOOPI_DISPATCH_MAX_QUEUED` (20) waiting jobs per task, so a fresh upload — deferred by the uncapped fast path — waits behind at most ~20 jobs of its kind, not the whole backlog; racing dispatcher ticks and the fast path still give each item exactly one job with its id recorded; re-running an analysis never creates a second set of keyframes. Tests: Task 3 (`test_backlog_is_fed_a_few_jobs_at_a_time_and_uploads_skip_the_line`, `test_backlog_and_fast_path_race_give_each_item_exactly_one_job`, `test_dispatcher_defers_oldest_first_so_device_order_follows_started_at`), Task 4 (`test_rerun_never_creates_a_second_set_of_keyframes`).

---

## File Structure

```
brain/
├── pyproject.toml                         # Task 1: procrastinate, pgvector, numpy, pillow, faster-whisper, av, httpx, pyyaml
├── uv.lock                                # Task 1
├── src/loopi_brain/
│   ├── config.py                          # Task 1: pipeline settings (LOOPI_*)
│   ├── db.py                              # Task 1: migrate() + advisory lock + procrastinate schema; pgvector pool
│   ├── migrations/002_pipeline.sql        # Task 1: item analysis columns, keyframe, transcript_segment, moment
│   ├── app.py                             # Task 3: lifespan opens the job app; Task 9: http client for the status page
│   ├── cli.py                             # Task 8: worker, worker-health, models pull (lazy imports)
│   ├── health.py                          # Task 8: worker_alive (psycopg only: the healthcheck runs every 30 s)
│   ├── api/device_routes.py               # Task 3: put_item fast path → dispatch.defer_item
│   ├── providers/
│   │   ├── __init__.py                    # Task 2
│   │   ├── base.py                        # Task 2: Provider protocol, errors, EMBED_DIM, CLOUD_PROVIDERS
│   │   ├── config.py                      # Task 2: providers.yaml models, defaults, loader
│   │   ├── ollama.py                      # Task 2: Ollama HTTP client (chat, embed, health, pull)
│   │   └── router.py                      # Task 2: Router (privacy, fallback, model_version), build_router
│   ├── pipeline/
│   │   ├── __init__.py                    # Task 3
│   │   ├── jobs.py                        # Task 3: job_app, task names, RETRY, open_jobs, enqueue
│   │   ├── context.py                     # Task 3: PipelineContext, set_context, get_context
│   │   ├── dispatch.py                    # Task 3: defer_item/keyframe/embed, health-gated dispatch_pending, maintenance
│   │   ├── state.py                       # Task 4: finish_item, error recording, row-exists checks
│   │   ├── frames.py                      # Task 4: dHash, keyframe selection, ffmpeg frame reading/extraction
│   │   ├── video.py                       # Task 4: analyze_video
│   │   ├── moments.py                     # Task 5: index_marker, embed_pending
│   │   ├── caption.py                     # Task 6: Caption, caption_keyframe
│   │   ├── asr.py                         # Task 7: VAD regions, filters, WhisperTranscriber, transcribe_audio
│   │   ├── worker.py                      # Task 8: run_worker, pull_models
│   │   └── status.py                      # Task 9: /pipeline numbers, model health, retry
│   └── web/
│       ├── routes.py                      # Task 9: enriched timeline, /pipeline, retry
│       └── templates/{base,timeline,pipeline,_retried}.html   # Task 9
└── tests/
    ├── conftest.py                        # Task 1: truncation; Task 3: jobs_open fixture
    ├── helpers.py                         # every task appends its shared helpers
    └── test_{config,db,providers,dispatch,frames,video,moments,caption,asr,worker,web_pipeline,deploy}.py
deploy/
├── providers.yaml                         # Task 2 (committed, no secrets)
├── docker-compose.yml                     # Task 10: worker + models services
├── smoke.sh                               # Task 10: test video must reach analysis_state='done'; removes its data
└── README.md                              # Task 10
docs/measurements/2026-10-p2a-pipeline.md  # Task 11 (controller)
```

---

### Task 1: Dependencies, pipeline settings, schema and migrations

**Files:**
- Modify: `brain/pyproject.toml`, `brain/uv.lock`
- Modify: `brain/src/loopi_brain/config.py`, `brain/src/loopi_brain/db.py`
- Create: `brain/src/loopi_brain/migrations/002_pipeline.sql`
- Modify: `brain/tests/conftest.py` (truncation list), `brain/tests/helpers.py` (`fresh_database`)
- Test: `brain/tests/test_db.py` (rewritten, keeps the P1 tests), `brain/tests/test_config.py` (new)

**Interfaces:**
- Consumes: P1 `migrate()`, `make_pool()`, `Settings`, `001_init.sql`.
- Produces:
  - `Settings` gains `ollama_url: str = "http://models:11434"`, `ollama_keep_alive: str = "30m"`, `providers_file: Path | None = None`, `model_dir: Path = Path("/srv/loopi/models")`, `worker_concurrency: int = 2`, `dispatch_max_queued: int = 20`, `keyframe_dark_luma: float = 24.0`, `keyframe_motion_max: float = 8.0`, `keyframe_hash_threshold: int = 12`, `keyframe_min_gap_s: float = 30.0`, `asr_languages: str = "vi,ko,en"`, `whisper_cpu_threads: int = 4`, property `asr_language_list -> list[str]`.
  - `db.migrate(database_url) -> list[str]` applies `*.sql` then the Procrastinate schema under `pg_advisory_lock(MIGRATION_LOCK_ID)`; returns names applied (e.g. `["002_pipeline.sql", "procrastinate-3.10.0"]`). Constants `MIGRATION_LOCK_ID`, `PROCRASTINATE_SCHEMA = "procrastinate-3.10.0"`.
  - `db.make_pool(url)` registers pgvector on every connection.
  - Tables: `item.analysis_state/analysis_job_id/analysis_error/analyzed_at`, `keyframe`, `transcript_segment`, `moment` (columns as in `002_pipeline.sql` below), with indexes on every cascading foreign key.
  - `helpers.fresh_database(database_url, name)` — context manager yielding a conninfo for an empty database.

- [ ] **Step 1: Add the dependencies and lock them**

Replace `brain/pyproject.toml` with:

```toml
[project]
name = "loopi-brain"
version = "0.1.0"
description = "Loopi brain: ingest, storage and web UI for the Loopi lifelog device"
requires-python = ">=3.13"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "psycopg[binary]>=3.2",
    "psycopg-pool>=3.2",
    "cryptography>=43",
    "pydantic-settings>=2.6",
    "jinja2>=3.1",
    "python-multipart>=0.0.17",
    "python-ulid>=3.0",
    "anyio>=4.6",
    # Exact pin: db.migrate() applies this version's schema.sql and records it as
    # "procrastinate-3.10.0"; a version bump must also apply its sql/migrations files.
    "procrastinate==3.10.0",
    "pgvector>=0.4",
    "numpy>=2.1",
    "pillow>=11.0",
    "faster-whisper==1.2.1",
    # faster-whisper 1.2.1 decode_audio() passes av.open(metadata_errors=...), which PyAV 19 removed.
    "av>=15,<19",
    "httpx>=0.28",
    "pyyaml>=6.0",
]

[project.scripts]
loopi-brain = "loopi_brain.cli:main"

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "pytest-cov>=6.0",
    "testcontainers[postgres]>=4.8",
    "jsonschema>=4.23",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/loopi_brain"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
```

Run: `cd brain && uv lock && uv sync && uv run python -c "import procrastinate, av, faster_whisper; print(procrastinate.__version__, av.__version__)"`
Expected: `3.10.0 18.x.y` (PyAV 18, never 19).

Run: `cd brain && uv run pytest -q`
Expected: `133 passed` — nothing uses the new packages yet.

- [ ] **Step 2: Write the failing tests**

Create `brain/tests/test_config.py`:

```python
from pathlib import Path

from loopi_brain.config import Settings


def test_pipeline_defaults():
    s = Settings(database_url="postgresql://x")
    assert s.ollama_url == "http://models:11434"
    assert s.ollama_keep_alive == "30m"
    assert s.providers_file is None
    assert s.model_dir == Path("/srv/loopi/models")
    assert s.worker_concurrency == 2 and s.dispatch_max_queued == 20
    assert (s.keyframe_dark_luma, s.keyframe_motion_max, s.keyframe_hash_threshold, s.keyframe_min_gap_s) == (
        24.0, 8.0, 12, 30.0,
    )
    assert s.asr_language_list == ["vi", "ko", "en"]
    assert s.whisper_cpu_threads == 4


def test_pipeline_settings_from_env(monkeypatch):
    monkeypatch.setenv("LOOPI_DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("LOOPI_ASR_LANGUAGES", " ko , en ,")
    monkeypatch.setenv("LOOPI_KEYFRAME_MIN_GAP_S", "45")
    monkeypatch.setenv("LOOPI_PROVIDERS_FILE", "/etc/loopi/providers.yaml")
    s = Settings()
    assert s.asr_language_list == ["ko", "en"]
    assert s.keyframe_min_gap_s == 45.0
    assert s.providers_file == Path("/etc/loopi/providers.yaml")
```

Replace `brain/tests/test_db.py` (the two P1 tests are kept):

```python
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
```

In `brain/tests/helpers.py` add `import contextlib` above `import datetime as dt`, and append:

```python


@contextlib.contextmanager
def fresh_database(database_url: str, name: str):
    """Create an empty database next to the test database; yield its URL; drop it afterwards."""
    import psycopg
    from psycopg.conninfo import make_conninfo

    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield make_conninfo(database_url, dbname=name)
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_config.py tests/test_db.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'ollama_url'` and a collection error `ImportError: cannot import name 'PROCRASTINATE_SCHEMA' from 'loopi_brain.db'`.

- [ ] **Step 4: Add the settings**

Replace `brain/src/loopi_brain/config.py` with:

```python
"""Runtime settings, read from LOOPI_* environment variables."""
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOOPI_")

    database_url: str
    media_root: Path = Path("/srv/loopi/media")
    pki_dir: Path = Path("/srv/loopi/pki")
    tz: str = "Asia/Seoul"
    min_free_bytes: int = 2 * 1024**3
    max_item_bytes: int = 64 * 1024**2
    pair_token_ttl_minutes: int = 30
    supported_agent_major: int = 0
    default_video_fps: int = 2
    # Perception pipeline (P2a)
    ollama_url: str = "http://models:11434"
    ollama_keep_alive: str = "30m"
    providers_file: Path | None = None
    model_dir: Path = Path("/srv/loopi/models")
    worker_concurrency: int = 2
    dispatch_max_queued: int = 20  # backlog jobs per task kept waiting at once (fresh uploads are not capped)
    keyframe_dark_luma: float = 24.0
    keyframe_motion_max: float = 8.0
    keyframe_hash_threshold: int = 12
    keyframe_min_gap_s: float = 30.0
    asr_languages: str = "vi,ko,en"
    whisper_cpu_threads: int = 4

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def asr_language_list(self) -> list[str]:
        return [code.strip() for code in self.asr_languages.split(",") if code.strip()]
```

- [ ] **Step 5: Write the migration**

Create `brain/src/loopi_brain/migrations/002_pipeline.sql`:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Existing P1 rows become 'pending', so recordings made before the upgrade are analysed too.
ALTER TABLE item
    ADD COLUMN analysis_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (analysis_state IN ('pending', 'done', 'skipped')),
    ADD COLUMN analysis_job_id BIGINT,
    ADD COLUMN analysis_error TEXT,
    ADD COLUMN analyzed_at TIMESTAMPTZ;
CREATE INDEX item_analysis_pending ON item (started_at) WHERE analysis_state = 'pending';

CREATE TABLE keyframe (
    id              TEXT PRIMARY KEY,
    item_id         TEXT NOT NULL REFERENCES item(id) ON DELETE CASCADE,
    device_id       TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    frame_index     INT NOT NULL,
    path            TEXT NOT NULL,
    dhash           BIGINT NOT NULL,
    luma            REAL NOT NULL,
    caption         JSONB,
    caption_model   TEXT,
    captioned_at    TIMESTAMPTZ,
    caption_job_id  BIGINT,
    caption_error   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (item_id, frame_index)
);
CREATE INDEX keyframe_device_ts ON keyframe (device_id, ts);
CREATE INDEX keyframe_uncaptioned ON keyframe (ts) WHERE caption IS NULL;

CREATE TABLE transcript_segment (
    id              TEXT PRIMARY KEY,
    item_id         TEXT NOT NULL REFERENCES item(id) ON DELETE CASCADE,
    device_id       TEXT NOT NULL,
    start_ts        TIMESTAMPTZ NOT NULL,
    end_ts          TIMESTAMPTZ NOT NULL,
    lang            TEXT NOT NULL,
    text            TEXT NOT NULL,
    avg_logprob     REAL,
    no_speech_prob  REAL,
    model_version   TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX transcript_segment_start ON transcript_segment (start_ts);
CREATE INDEX transcript_segment_item ON transcript_segment (item_id);

CREATE TABLE moment (
    id                     TEXT PRIMARY KEY,
    device_id              TEXT NOT NULL,
    ts_start               TIMESTAMPTZ NOT NULL,
    ts_end                 TIMESTAMPTZ NOT NULL,
    kind                   TEXT NOT NULL CHECK (kind IN ('visual', 'speech', 'marker')),
    text                   TEXT NOT NULL,
    lang                   TEXT,
    item_id                TEXT REFERENCES item(id) ON DELETE CASCADE,
    keyframe_id            TEXT REFERENCES keyframe(id) ON DELETE CASCADE,
    transcript_segment_id  TEXT REFERENCES transcript_segment(id) ON DELETE CASCADE,
    embedding              vector(1024),
    embed_model            TEXT,
    model_version          TEXT NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX moment_ts_start ON moment (ts_start);
CREATE INDEX moment_embedding ON moment USING hnsw (embedding vector_cosine_ops);
CREATE INDEX moment_text_trgm ON moment USING gin (text gin_trgm_ops);
CREATE INDEX moment_unembedded ON moment (created_at) WHERE embedding IS NULL;
-- Foreign keys that cascade: without these, every deleted item, keyframe or segment scans moment.
CREATE INDEX moment_item ON moment (item_id);
CREATE INDEX moment_keyframe ON moment (keyframe_id);
CREATE INDEX moment_transcript_segment ON moment (transcript_segment_id);
```

- [ ] **Step 6: Lock the migration run, apply the Procrastinate schema, register pgvector**

Replace `brain/src/loopi_brain/db.py` with:

```python
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
```

- [ ] **Step 7: Truncate the new tables between tests**

In `brain/tests/conftest.py` replace the `APP_TABLES` line with:

```python
APP_TABLES = (
    "moment, transcript_segment, keyframe, device_command, item, pair_token, device,"
    " procrastinate_events, procrastinate_periodic_defers, procrastinate_jobs, procrastinate_workers"
)
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_config.py tests/test_db.py -v`
Expected: PASS (9 tests).

Run: `cd brain && uv run pytest -q`
Expected: all pass (the P1 suite included).

- [ ] **Step 9: Commit**

```bash
git add brain/pyproject.toml brain/uv.lock brain/src/loopi_brain/config.py brain/src/loopi_brain/db.py \
  brain/src/loopi_brain/migrations/002_pipeline.sql brain/tests/conftest.py brain/tests/helpers.py \
  brain/tests/test_db.py brain/tests/test_config.py
git commit -F - <<'EOF'
feat(brain): pipeline schema, settings and locked migrations with the Procrastinate schema

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 2: Provider router, Ollama client and `providers.yaml`

**Files:**
- Create: `brain/src/loopi_brain/providers/__init__.py`, `base.py`, `config.py`, `ollama.py`, `router.py`
- Create: `deploy/providers.yaml`
- Modify: `brain/tests/helpers.py` (append `FakeOllama`)
- Test: `brain/tests/test_providers.py`

**Interfaces:**
- Consumes: `Settings.ollama_url`, `Settings.ollama_keep_alive` (Task 1).
- Produces:
  - `providers.base`: `EMBED_DIM = 1024`, `CLOUD_PROVIDERS = frozenset({"anthropic"})`, `ProviderError`, `ProviderUnavailable(ProviderError)`, `PrivacyBlocked`, `Provider` protocol (`async chat(model, messages, *, images, json_schema, temperature, max_tokens) -> str`, `async embed(model, texts) -> list[list[float]]`).
  - `providers.config`: `Privacy`, `TaskRoute(provider, model, fallback)` with `chain() -> list[TaskRoute]`, `ProvidersConfig(privacy, tasks)` with `ollama_models() -> list[str]`, `TASKS`, `DEFAULT_CONFIG`, `load_providers(path: Path | None) -> ProvidersConfig`, `ProvidersConfigError`.
  - `providers.ollama`: `OllamaProvider(base_url, *, keep_alive="30m", client=None)` with `chat`, `embed`, `health(models) -> OllamaHealth(reachable, present, missing)` (one `GET /api/tags`, `HEALTH_TIMEOUT` = 5 s), `pull(model)`, `aclose()`; `DEFAULT_TIMEOUT` (connect 5 s, read 300 s).
  - `providers.router`: `ChatResult(text, model_version)`, `EmbedResult(vectors, model_version)`, `Router(config, providers)` with `route(task)`, `model_version(task)`, `async chat(task, messages, images=None, json_schema=None, temperature=0.0, max_tokens=None)` (`max_tokens` → Ollama `options.num_predict`), `async embed(task, texts)`, `async health()`, `task_ready(task, health) -> bool`; `build_router(config, settings, client=None) -> Router`.
  - `helpers.FakeOllama(caption=None, models=None, dims=1024)` — `httpx.MockTransport` handler with `.down` (connection refused), `.error` (HTTP status for chat/embed), `.models`, `.requests`, `.timeouts`, `.client()`, `.paths()`; `helpers.DEFAULT_CAPTION`, `helpers.DEFAULT_OLLAMA_MODELS`.

Rulings in this task: unknown provider names, `local` for any task but `asr`, a non-`local` `asr`, an unsupported ASR model, or a missing/unknown task name are config errors at load time (P2a knows only `ollama` and `local`; the Anthropic provider lands in P2b). No file configured means the built-in defaults; a configured file that cannot be read is an error. Images never go to a cloud route unless `allow_cloud_images`; a local fallback still serves them. Fallback is tried on `ProviderError`/`ProviderUnavailable`; when every route fails, the last error is raised. Error mapping: connection refused, DNS failure, connect timeout **and HTTP 404** (model not pulled — a configuration problem the status page shows, not an item problem) are `ProviderUnavailable`; read/write timeouts, dropped connections, other HTTP errors and unusable bodies are `ProviderError` (normal retries, then the dead-letter list). `task_ready(task, health)` is true when one of the task's routes has its model on the reachable Ollama server (routes to other providers count as ready).

- [ ] **Step 1: Write the failing tests**

Append to `brain/tests/helpers.py`:

```python


DEFAULT_CAPTION = {
    "scene": "A small home office with a desk by a window",
    "activity": "A person is typing on a laptop",
    "people_count": 1,
    "objects": ["laptop", "desk lamp", "mug"],
    "text_seen": "",
    "notable": False,
}
DEFAULT_OLLAMA_MODELS = ["minicpm-v4.6:1b", "qwen3-embedding:0.6b", "qwen3:4b", "qwen3:8b"]


class FakeOllama:
    """In-process stand-in for the Ollama HTTP API, used as an ``httpx.MockTransport`` handler.

    Set ``down = True`` to simulate a stopped model server (every request raises ConnectError), or
    ``error = 500`` to make chat and embed requests fail with that HTTP status.
    """

    def __init__(self, caption: dict | str | None = None, models: list[str] | None = None, dims: int = 1024):
        self.down = False
        self.error: int | None = None
        self.caption = DEFAULT_CAPTION if caption is None else caption
        self.models = list(DEFAULT_OLLAMA_MODELS if models is None else models)
        self.dims = dims
        self.requests: list[tuple[str, dict]] = []
        self.timeouts: list[tuple[str, dict | None]] = []

    def __call__(self, request):
        import json

        import httpx

        if self.down:
            raise httpx.ConnectError("[Errno 111] Connection refused", request=request)
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        self.requests.append((path, body))
        self.timeouts.append((path, request.extensions.get("timeout")))
        if path in ("/api/chat", "/api/embed"):
            if self.error is not None:
                return httpx.Response(self.error, json={"error": "model runner has unexpectedly stopped"})
            if body["model"] not in self.models:
                return httpx.Response(404, json={"error": f"model '{body['model']}' not found"})
        if path == "/api/chat":
            content = self.caption if isinstance(self.caption, str) else json.dumps(self.caption)
            return httpx.Response(
                200, json={"model": body["model"], "message": {"role": "assistant", "content": content}, "done": True}
            )
        if path == "/api/embed":
            vectors = []
            for i, _text in enumerate(body["input"]):
                v = [0.0] * self.dims
                v[i % self.dims] = 1.0
                vectors.append(v)
            return httpx.Response(200, json={"model": body["model"], "embeddings": vectors})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m, "model": m} for m in self.models]})
        if path == "/api/pull":
            self.models.append(body["model"])
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(404, json={"error": "not found"})

    def client(self):
        import httpx

        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def paths(self) -> list[str]:
        return [p for p, _ in self.requests]
```

Create `brain/tests/test_providers.py`:

```python
import base64
import json

import httpx
import pytest

from helpers import DEFAULT_CAPTION, FakeOllama
from loopi_brain.config import Settings
from loopi_brain.providers.base import (
    EMBED_DIM,
    PrivacyBlocked,
    ProviderError,
    ProviderUnavailable,
)
from loopi_brain.providers.config import (
    DEFAULT_CONFIG,
    ProvidersConfig,
    ProvidersConfigError,
    TaskRoute,
    load_providers,
)
from loopi_brain.providers.ollama import OllamaHealth, OllamaProvider
from loopi_brain.providers.router import Router, build_router

DEPLOY_PROVIDERS = __import__("pathlib").Path(__file__).resolve().parents[2] / "deploy" / "providers.yaml"
SETTINGS = Settings(database_url="postgresql://unused", ollama_url="http://models.test:11434")


def _config(**overrides) -> ProvidersConfig:
    tasks = {name: route.model_dump() for name, route in DEFAULT_CONFIG.tasks.items()}
    tasks.update(overrides.pop("tasks", {}))
    return ProvidersConfig.model_validate({"tasks": tasks, **overrides})


def test_committed_providers_yaml_equals_built_in_defaults():
    assert load_providers(DEPLOY_PROVIDERS) == DEFAULT_CONFIG
    assert load_providers(None) == DEFAULT_CONFIG
    assert DEFAULT_CONFIG.privacy.allow_cloud_images is False
    assert DEFAULT_CONFIG.privacy.allow_cloud_audio is False
    assert DEFAULT_CONFIG.ollama_models() == ["minicpm-v4.6:1b", "qwen3-embedding:0.6b", "qwen3:4b", "qwen3:8b"]


@pytest.mark.parametrize(
    "tasks",
    [
        {"caption": {"provider": "openai", "model": "gpt"}},
        {"asr": {"provider": "ollama", "model": "whisper"}},
        {"asr": {"provider": "local", "model": "faster-whisper/tiny"}},
        {"embed": {"provider": "local", "model": "x"}},
        {"recap": {"provider": "ollama", "model": "qwen3:8b", "fallback": {"provider": "anthropic", "model": "c"}}},
        {"vlog": {"provider": "ollama", "model": "qwen3:8b"}},
    ],
)
def test_invalid_routes_are_rejected_at_load_time(tmp_path, tasks):
    with pytest.raises(ValueError):
        _config(tasks=tasks)
    data = {"tasks": {**{n: r.model_dump() for n, r in DEFAULT_CONFIG.tasks.items()}, **tasks}}
    path = tmp_path / "providers.yaml"
    path.write_text(json.dumps(data))  # JSON is valid YAML
    with pytest.raises(ProvidersConfigError):
        load_providers(path)


def test_missing_task_typo_or_unreadable_file_is_a_config_error(tmp_path):
    path = tmp_path / "providers.yaml"
    path.write_text("tasks:\n  caption: {provider: ollama, model: m}\n")
    with pytest.raises(ProvidersConfigError, match="missing"):
        load_providers(path)
    path.write_text("privacy: {allow_cloud_image: true}\ntasks: {}\n")
    with pytest.raises(ProvidersConfigError):
        load_providers(path)
    path.write_text("tasks: [unclosed\n")
    with pytest.raises(ProvidersConfigError):
        load_providers(path)
    with pytest.raises(ProvidersConfigError, match="cannot read"):
        load_providers(tmp_path / "absent.yaml")


async def test_ollama_chat_sends_schema_images_and_no_thinking():
    fake = FakeOllama()
    provider = OllamaProvider("http://models.test:11434/", keep_alive="30m", client=fake.client())
    text = await provider.chat(
        "minicpm-v4.6:1b",
        [{"role": "user", "content": "describe"}],
        images=[b"\xff\xd8jpeg"],
        json_schema={"type": "object"},
        temperature=0.0,
        max_tokens=400,
    )
    assert json.loads(text) == DEFAULT_CAPTION
    path, body = fake.requests[0]
    assert path == "/api/chat"
    assert body["stream"] is False and body["think"] is False and body["keep_alive"] == "30m"
    assert body["format"] == {"type": "object"} and body["options"] == {"temperature": 0.0, "num_predict": 400}
    assert body["messages"] == [
        {"role": "user", "content": "describe", "images": [base64.b64encode(b"\xff\xd8jpeg").decode()]}
    ]


async def test_ollama_chat_without_schema_or_images_sends_neither():
    fake = FakeOllama(caption="plain text")
    provider = OllamaProvider("http://models.test:11434", client=fake.client())
    assert await provider.chat("qwen3:8b", [{"role": "user", "content": "hi"}]) == "plain text"
    body = fake.requests[0][1]
    assert "format" not in body and "images" not in body["messages"][0]
    assert body["options"] == {"temperature": 0.0}  # no token limit unless asked


async def test_ollama_embed_returns_vectors():
    fake = FakeOllama()
    provider = OllamaProvider("http://models.test:11434", client=fake.client())
    vectors = await provider.embed("qwen3-embedding:0.6b", ["a", "b"])
    assert len(vectors) == 2 and len(vectors[0]) == EMBED_DIM
    assert fake.requests[0] == ("/api/embed", {"model": "qwen3-embedding:0.6b", "input": ["a", "b"], "keep_alive": "30m"})


def _transport_raising(error_type):
    def handler(request):
        raise error_type("simulated", request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_connection_failures_and_missing_models_are_unavailable():
    fake = FakeOllama()
    provider = OllamaProvider("http://models.test:11434", client=fake.client())
    with pytest.raises(ProviderUnavailable, match="HTTP 404.*not found"):  # model not pulled: configuration
        await provider.chat("missing:1b", [{"role": "user", "content": "x"}])
    with pytest.raises(ProviderUnavailable, match="HTTP 404"):
        await provider.embed("missing-embedding", ["a"])
    fake.down = True  # connection refused
    with pytest.raises(ProviderUnavailable):
        await provider.embed("qwen3-embedding:0.6b", ["a"])
    slow_connect = OllamaProvider("http://models.test:11434", client=_transport_raising(httpx.ConnectTimeout))
    with pytest.raises(ProviderUnavailable):
        await slow_connect.chat("qwen3:8b", [{"role": "user", "content": "x"}])


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.RemoteProtocolError])
async def test_failures_after_connecting_are_provider_errors(error_type):
    provider = OllamaProvider("http://models.test:11434", client=_transport_raising(error_type))
    with pytest.raises(ProviderError) as info:
        await provider.chat("qwen3:8b", [{"role": "user", "content": "x"}])
    assert not isinstance(info.value, ProviderUnavailable)  # normal retries, then the dead-letter list


async def test_server_errors_are_provider_errors():
    fake = FakeOllama()
    fake.error = 500
    provider = OllamaProvider("http://models.test:11434", client=fake.client())
    with pytest.raises(ProviderError, match="HTTP 500") as info:
        await provider.chat("minicpm-v4.6:1b", [{"role": "user", "content": "x"}])
    assert not isinstance(info.value, ProviderUnavailable)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="<html>oops</html>"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["a list"]),
        httpx.Response(200, json={"message": {}}),
    ],
)
async def test_unusable_chat_responses_are_provider_errors(response):
    provider = OllamaProvider(
        "http://models.test:11434", client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response))
    )
    with pytest.raises(ProviderError):
        await provider.chat("qwen3:8b", [{"role": "user", "content": "x"}])


async def test_embed_without_embeddings_is_provider_error():
    provider = OllamaProvider(
        "http://models.test:11434",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
    )
    with pytest.raises(ProviderError):
        await provider.embed("qwen3-embedding:0.6b", ["a"])


async def test_health_reports_missing_models_and_unreachable_server():
    fake = FakeOllama(models=["qwen3:8b", "minicpm-v4.6:1b"])
    router = build_router(DEFAULT_CONFIG, SETTINGS, fake.client())
    health = await router.health()
    assert health.reachable is True
    assert health.present == ["minicpm-v4.6:1b", "qwen3:8b"]
    assert health.missing == ["qwen3-embedding:0.6b", "qwen3:4b"]
    assert fake.timeouts == [("/api/tags", {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0})]
    assert router.task_ready("caption", health) and not router.task_ready("embed", health)
    fake.down = True
    health = await router.health()
    assert health.reachable is False and health.missing == DEFAULT_CONFIG.ollama_models()
    assert not router.task_ready("caption", health) and not router.task_ready("embed", health)


def test_routes_the_health_check_cannot_see_count_as_ready():
    config = _config()
    config.tasks["recap"] = TaskRoute.model_construct(provider="anthropic", model="claude", fallback=None)
    router = Router(config, {})
    nothing = OllamaHealth(reachable=False, present=[], missing=config.ollama_models())
    assert router.task_ready("recap", nothing) and not router.task_ready("caption", nothing)


async def test_pull_downloads_a_model():
    fake = FakeOllama(models=[])
    provider = OllamaProvider("http://models.test:11434", client=fake.client())
    await provider.pull("qwen3:4b")
    assert fake.requests == [("/api/pull", {"model": "qwen3:4b", "stream": False})]
    bad = OllamaProvider(
        "http://models.test:11434",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "x"}))),
    )
    with pytest.raises(ProviderError):
        await bad.pull("qwen3:4b")


class StubProvider:
    def __init__(self, error: Exception | None = None, dims: int = EMBED_DIM):
        self.error, self.dims, self.calls = error, dims, []

    async def chat(self, model, messages, *, images, json_schema, temperature, max_tokens=None):
        self.calls.append(("chat", model, images))
        if self.error:
            raise self.error
        return f"answer from {model}"

    async def embed(self, model, texts):
        self.calls.append(("embed", model, texts))
        if self.error:
            raise self.error
        return [[0.0] * self.dims for _ in texts]


def _router(config, **providers) -> Router:
    return Router(config, providers)


async def test_router_reports_model_version_of_the_route_that_answered():
    stub = StubProvider()
    router = _router(DEFAULT_CONFIG, ollama=stub)
    result = await router.chat("caption", [{"role": "user", "content": "x"}], images=[b"img"], json_schema={})
    assert result.text == "answer from minicpm-v4.6:1b"
    assert result.model_version == "ollama:minicpm-v4.6:1b"
    assert router.model_version("asr") == "local:faster-whisper/large-v3-turbo-int8"
    assert router.route("embed") == TaskRoute(provider="ollama", model="qwen3-embedding:0.6b")


async def test_router_falls_back_on_error_and_raises_the_last_error():
    config = _config(tasks={"recap": {"provider": "ollama", "model": "qwen3:8b",
                                      "fallback": {"provider": "ollama", "model": "qwen3:4b"}}})

    class FirstFails(StubProvider):
        async def chat(self, model, messages, *, images, json_schema, temperature, max_tokens=None):
            if model == "qwen3:8b":
                raise ProviderUnavailable("down")
            return await super().chat(model, messages, images=images, json_schema=json_schema,
                                      temperature=temperature, max_tokens=max_tokens)

    result = await _router(config, ollama=FirstFails()).chat("recap", [{"role": "user", "content": "x"}])
    assert result.model_version == "ollama:qwen3:4b"
    with pytest.raises(ProviderError, match="boom"):
        await _router(config, ollama=StubProvider(ProviderError("boom"))).chat(
            "recap", [{"role": "user", "content": "x"}]
        )


async def test_router_never_sends_images_to_a_cloud_provider():
    # P2a's config loader rejects cloud providers, so build the cloud route without validation.
    config = _config()
    config.tasks["caption"] = TaskRoute.model_construct(provider="anthropic", model="claude", fallback=None)
    stub = StubProvider()
    router = _router(config, ollama=stub, anthropic=stub)
    with pytest.raises(PrivacyBlocked):
        await router.chat("caption", [{"role": "user", "content": "x"}], images=[b"img"])
    assert stub.calls == []
    # Text without images may go to the cloud route; a local fallback still serves images.
    assert (await router.chat("caption", [{"role": "user", "content": "x"}])).model_version == "anthropic:claude"
    config.tasks["caption"] = TaskRoute.model_construct(
        provider="anthropic", model="claude", fallback=TaskRoute(provider="ollama", model="minicpm-v4.6:1b")
    )
    result = await router.chat("caption", [{"role": "user", "content": "x"}], images=[b"img"])
    assert result.model_version == "ollama:minicpm-v4.6:1b"


async def test_router_rejects_wrong_embedding_dimensions():
    router = _router(DEFAULT_CONFIG, ollama=StubProvider(dims=768))
    with pytest.raises(ProviderError, match="1024"):
        await router.embed("embed", ["a"])
    result = await _router(DEFAULT_CONFIG, ollama=StubProvider()).embed("embed", ["a", "b"])
    assert len(result.vectors) == 2 and result.model_version == "ollama:qwen3-embedding:0.6b"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_providers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'loopi_brain.providers'`.

- [ ] **Step 3: Write the provider interface**

Create `brain/src/loopi_brain/providers/__init__.py`:

```python
"""Model providers and the task router (spec §9.3)."""
```

Create `brain/src/loopi_brain/providers/base.py`:

```python
"""Provider interface and the errors every provider raises."""
from __future__ import annotations

from typing import Protocol

EMBED_DIM = 1024
CLOUD_PROVIDERS = frozenset({"anthropic"})


class ProviderError(Exception):
    """The provider failed a request it should serve: HTTP errors other than 404, read/write timeouts,
    dropped connections, unusable bodies, wrong dimensions. Retried normally, then dead-lettered."""


class ProviderUnavailable(ProviderError):
    """The provider cannot serve the task right now: connection refused, DNS failure, connect timeout,
    or HTTP 404 (model not pulled). A configuration/availability problem, not an item problem."""


class PrivacyBlocked(Exception):
    """The request carries images or audio and every route for the task is a cloud provider."""


class Provider(Protocol):
    async def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        images: list[bytes] | None,
        json_schema: dict | None,
        temperature: float,
        max_tokens: int | None,
    ) -> str: ...

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]: ...
```

- [ ] **Step 4: Write the configuration and the committed `providers.yaml`**

Create `brain/src/loopi_brain/providers/config.py`:

```python
"""`providers.yaml`: which provider and model serve each pipeline task (spec §9.3)."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

# P2a ships the Ollama provider and the in-process ASR ("local"); cloud providers arrive in P2b.
KNOWN_PROVIDERS = frozenset({"ollama", "local"})
TASKS = ("caption", "asr", "embed", "chapter_summary", "recap", "chat")
LOCAL_ASR_MODELS = frozenset({"faster-whisper/large-v3-turbo-int8"})


class ProvidersConfigError(Exception):
    """providers.yaml is missing, unreadable or invalid."""


class Privacy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_cloud_images: bool = False
    allow_cloud_audio: bool = False


class TaskRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    fallback: TaskRoute | None = None

    def chain(self) -> list[TaskRoute]:
        """This route followed by its fallbacks, in order."""
        routes: list[TaskRoute] = []
        route: TaskRoute | None = self
        while route is not None:
            routes.append(route)
            route = route.fallback
        return routes


class ProvidersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    privacy: Privacy = Privacy()
    tasks: dict[str, TaskRoute]

    @model_validator(mode="after")
    def _check(self) -> ProvidersConfig:
        missing = [t for t in TASKS if t not in self.tasks]
        unknown = sorted(set(self.tasks) - set(TASKS))
        if missing or unknown:
            raise ValueError(f"tasks must be exactly {', '.join(TASKS)} (missing {missing}, unknown {unknown})")
        for task, route in self.tasks.items():
            for step in route.chain():
                if step.provider not in KNOWN_PROVIDERS:
                    raise ValueError(f"task {task}: unknown provider {step.provider!r}")
                if (step.provider == "local") != (task == "asr"):
                    raise ValueError(f"task {task}: provider 'local' serves asr only, and asr must be local")
                if task == "asr" and step.model not in LOCAL_ASR_MODELS:
                    raise ValueError(f"task asr: unsupported model {step.model!r}")
        return self

    def ollama_models(self) -> list[str]:
        """Every Ollama model named by any route or fallback, without duplicates, in task order."""
        models: list[str] = []
        for task in TASKS:
            for step in self.tasks[task].chain():
                if step.provider == "ollama" and step.model not in models:
                    models.append(step.model)
        return models


DEFAULT_CONFIG = ProvidersConfig(
    tasks={
        "caption": TaskRoute(provider="ollama", model="minicpm-v4.6:1b"),
        "asr": TaskRoute(provider="local", model="faster-whisper/large-v3-turbo-int8"),
        "embed": TaskRoute(provider="ollama", model="qwen3-embedding:0.6b"),
        "chapter_summary": TaskRoute(provider="ollama", model="qwen3:4b"),
        "recap": TaskRoute(provider="ollama", model="qwen3:8b"),
        "chat": TaskRoute(provider="ollama", model="qwen3:8b"),
    }
)


def load_providers(path: Path | None) -> ProvidersConfig:
    """Read and validate ``path``; no path means the built-in defaults (all local)."""
    if path is None:
        return DEFAULT_CONFIG
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProvidersConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return ProvidersConfig.model_validate(data)
    except ValidationError as exc:
        raise ProvidersConfigError(f"invalid {path}: {exc}") from exc
```

Create `deploy/providers.yaml`:

```yaml
# Which provider and model serve each pipeline task (spec §9.3). Mounted read-only into api and worker
# at /etc/loopi/providers.yaml (LOOPI_PROVIDERS_FILE). No secrets here: API keys come only from the env.
# Every task runs locally by default; raw images and audio never leave the brain unless the matching
# privacy flag is true, and a processing backlog never escalates to the cloud.
privacy:
  allow_cloud_images: false
  allow_cloud_audio: false
tasks:
  caption:         { provider: ollama, model: "minicpm-v4.6:1b" }
  asr:             { provider: local,  model: faster-whisper/large-v3-turbo-int8 }
  embed:           { provider: ollama, model: "qwen3-embedding:0.6b" }
  chapter_summary: { provider: ollama, model: "qwen3:4b" }
  recap:           { provider: ollama, model: "qwen3:8b" }
  chat:            { provider: ollama, model: "qwen3:8b" }
  # Cloud fallbacks need the Anthropic provider (P2b) and ANTHROPIC_API_KEY, e.g.:
  # recap: { provider: ollama, model: "qwen3:8b", fallback: { provider: anthropic, model: claude-haiku-5-5 } }
```

- [ ] **Step 5: Write the Ollama client**

Create `brain/src/loopi_brain/providers/ollama.py`:

```python
"""Ollama HTTP API client (https://github.com/ollama/ollama/blob/main/docs/api.md)."""
from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx

from .base import ProviderError, ProviderUnavailable

# CPU inference of one image or one long prompt can take minutes.
DEFAULT_TIMEOUT = httpx.Timeout(300.0, connect=5.0)
# The dispatcher asks every minute whether the server can take work; it must answer fast.
HEALTH_TIMEOUT = httpx.Timeout(5.0)
ERROR_TAIL = 500


@dataclass(frozen=True)
class OllamaHealth:
    reachable: bool
    present: list[str]
    missing: list[str]


def _tagged(model: str) -> str:
    return model if ":" in model else f"{model}:latest"


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str, *, keep_alive: str = "30m", client: httpx.AsyncClient | None = None):
        self._base = base_url.rstrip("/")
        self._keep_alive = keep_alive
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, payload: dict | None = None, timeout=None) -> dict:
        kwargs: dict = {"json": payload} if payload is not None else {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            response = await self._client.request(method, f"{self._base}{path}", **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:  # refused, DNS failure, connect timeout
            raise ProviderUnavailable(f"ollama {path}: {type(exc).__name__}: {exc}") from exc
        except httpx.TransportError as exc:  # read/write timeout, dropped connection: this request failed
            raise ProviderError(f"ollama {path}: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("error") or response.text
            except ValueError:
                detail = response.text
            message = f"ollama {path} HTTP {response.status_code}: {str(detail)[-ERROR_TAIL:]}"
            if response.status_code == 404:  # model not pulled: configuration, shown on the status page
                raise ProviderUnavailable(message)
            raise ProviderError(message)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(f"ollama {path}: response is not JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError(f"ollama {path}: unexpected response {str(data)[:ERROR_TAIL]}")
        return data

    async def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        images: list[bytes] | None = None,
        json_schema: dict | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        msgs = [dict(m) for m in messages]
        if images:
            msgs[-1]["images"] = [base64.b64encode(img).decode("ascii") for img in images]
        options: dict = {"temperature": temperature}
        if max_tokens is not None:
            options["num_predict"] = max_tokens  # a looping model stops instead of filling the context
        payload: dict = {
            "model": model,
            "messages": msgs,
            "stream": False,
            "think": False,
            "keep_alive": self._keep_alive,
            "options": options,
        }
        if json_schema is not None:
            payload["format"] = json_schema
        data = await self._request("POST", "/api/chat", payload)
        content = (data.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise ProviderError("ollama /api/chat: response has no message content")
        return content

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        data = await self._request(
            "POST", "/api/embed", {"model": model, "input": texts, "keep_alive": self._keep_alive}
        )
        vectors = data.get("embeddings")
        if not isinstance(vectors, list):
            raise ProviderError("ollama /api/embed: response has no embeddings")
        return vectors

    async def health(self, models: list[str]) -> OllamaHealth:
        try:
            data = await self._request("GET", "/api/tags", timeout=HEALTH_TIMEOUT)
        except ProviderError:
            return OllamaHealth(reachable=False, present=[], missing=list(models))
        installed = {_tagged(m.get("name", "")) for m in data.get("models", []) if isinstance(m, dict)}
        present = [m for m in models if _tagged(m) in installed]
        return OllamaHealth(True, present, [m for m in models if _tagged(m) not in installed])

    async def pull(self, model: str) -> None:
        """Download ``model`` into the Ollama server (blocks until done; GBs on first run)."""
        data = await self._request(
            "POST", "/api/pull", {"model": model, "stream": False}, timeout=httpx.Timeout(None, connect=5.0)
        )
        if data.get("status") != "success":
            raise ProviderError(f"ollama pull {model}: {data}")
```

- [ ] **Step 6: Write the router**

Create `brain/src/loopi_brain/providers/router.py`:

```python
"""Route each pipeline task to its provider, with privacy rules and fallback (spec §9.3)."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..config import Settings
from .base import CLOUD_PROVIDERS, EMBED_DIM, PrivacyBlocked, Provider, ProviderError
from .config import ProvidersConfig, TaskRoute
from .ollama import OllamaHealth, OllamaProvider

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatResult:
    text: str
    model_version: str


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]]
    model_version: str


def model_version(route: TaskRoute) -> str:
    return f"{route.provider}:{route.model}"


class Router:
    def __init__(self, config: ProvidersConfig, providers: dict[str, Provider]):
        self.config = config
        self.providers = providers

    def route(self, task: str) -> TaskRoute:
        return self.config.tasks[task]

    def model_version(self, task: str) -> str:
        return model_version(self.route(task))

    def _usable(self, task: str, *, images: bool) -> list[TaskRoute]:
        steps = self.route(task).chain()
        if images and not self.config.privacy.allow_cloud_images:
            allowed = [s for s in steps if s.provider not in CLOUD_PROVIDERS]
            if not allowed:
                raise PrivacyBlocked(f"task {task}: images may not be sent to a cloud provider")
            return allowed
        return steps

    async def chat(
        self,
        task: str,
        messages: list[dict],
        images: list[bytes] | None = None,
        json_schema: dict | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> ChatResult:
        error: ProviderError | None = None
        for step in self._usable(task, images=bool(images)):
            try:
                text = await self.providers[step.provider].chat(
                    step.model, messages, images=images, json_schema=json_schema, temperature=temperature,
                    max_tokens=max_tokens,
                )
            except ProviderError as exc:
                log.warning("task %s: %s failed: %s", task, model_version(step), exc)
                error = exc
                continue
            return ChatResult(text, model_version(step))
        assert error is not None
        raise error

    async def embed(self, task: str, texts: list[str]) -> EmbedResult:
        error: ProviderError | None = None
        for step in self._usable(task, images=False):
            try:
                vectors = await self.providers[step.provider].embed(step.model, texts)
                if len(vectors) != len(texts) or any(len(v) != EMBED_DIM for v in vectors):
                    raise ProviderError(
                        f"{model_version(step)} returned {len(vectors)} vectors of "
                        f"{sorted({len(v) for v in vectors})} dims; expected {len(texts)} of {EMBED_DIM}"
                    )
            except ProviderError as exc:
                log.warning("task %s: %s failed: %s", task, model_version(step), exc)
                error = exc
                continue
            return EmbedResult(vectors, model_version(step))
        assert error is not None
        raise error

    async def health(self) -> OllamaHealth:
        """Is the Ollama server reachable, and which configured models does it have?"""
        return await self.providers["ollama"].health(self.config.ollama_models())

    def task_ready(self, task: str, health: OllamaHealth) -> bool:
        """Can ``task`` run now: does one of its routes have its model on the reachable Ollama server?

        Routes to other providers cannot be checked here and count as ready.
        """
        return any(s.provider != "ollama" or s.model in health.present for s in self.route(task).chain())


def build_router(config: ProvidersConfig, settings: Settings, client: httpx.AsyncClient | None = None) -> Router:
    ollama = OllamaProvider(settings.ollama_url, keep_alive=settings.ollama_keep_alive, client=client)
    return Router(config, {"ollama": ollama})
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_providers.py -v`
Expected: PASS (27 tests).

Run: `cd brain && uv run pytest -q`
Expected: the whole suite passes.

- [ ] **Step 8: Commit**

```bash
git add brain/src/loopi_brain/providers deploy/providers.yaml brain/tests/helpers.py brain/tests/test_providers.py
git commit -F - <<'EOF'
feat(brain): provider router with local-first privacy, fallback and an Ollama client

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 3: Job app, idempotent dispatch and the upload fast path

**Files:**
- Create: `brain/src/loopi_brain/pipeline/__init__.py`, `jobs.py`, `context.py`, `dispatch.py`
- Modify: `brain/src/loopi_brain/api/device_routes.py` (`put_item`), `brain/src/loopi_brain/app.py` (lifespan)
- Modify: `brain/tests/conftest.py` (`jobs_open`, `client`), `brain/tests/helpers.py` (append)
- Test: `brain/tests/test_dispatch.py`

**Interfaces:**
- Consumes: Task 1 schema (`item.analysis_*`, `keyframe.caption_job_id`, Procrastinate tables) and `Settings.dispatch_max_queued`, Task 2 `Router.health()`, `Router.task_ready()`, `build_router`, `DEFAULT_CONFIG`, helpers `FakeOllama`.
- Produces:
  - `pipeline.jobs`: `job_app` (`procrastinate.App`), `TASK_MODULES` (list later tasks extend), task names `ANALYZE_VIDEO = "analyze_video"`, `CAPTION_KEYFRAME = "caption_keyframe"`, `TRANSCRIBE_AUDIO = "transcribe_audio"`, `INDEX_MARKER = "index_marker"`, `EMBED_MOMENTS = "embed_moments"`, `DISPATCH_PENDING`, `RETRY_STALLED`, `REMOVE_OLD_JOBS`, `PIPELINE_TASKS`; `RETRY` (`RetryStrategy(max_attempts=3, exponential_wait=5)`); `open_jobs(pool)` async context manager; `async enqueue(conn, task, *, queue, queueing_lock, lock=None, **task_kwargs) -> int`.
  - `pipeline.context`: `PipelineContext(pool, settings, router, transcriber)`, `set_context(ctx | None)`, `get_context() -> PipelineContext` (raises `RuntimeError` when unset).
  - `pipeline.dispatch`: `async defer_item(pool, item_id) -> int | None`, `async defer_keyframe(pool, keyframe_id) -> int | None`, `async defer_keyframes(pool, ids)`, `async defer_embed(pool)`, `async dispatch_pending(pool, router, max_queued=20) -> tuple[int, int]` (items deferred, keyframes deferred), `async recover_stalled(pool) -> tuple[int, int]` (retried, failed), `STALLED_ERROR`, `DISPATCH_MAX_QUEUED = 20`, `DISPATCH_BATCH = 200`; periodic tasks `dispatch_pending_job` (every minute, uses the context's pool, router and `settings.dispatch_max_queued`), `retry_stalled_jobs` (every 10 min, calls `recover_stalled`), `remove_old_jobs` (04:15 daily, succeeded jobs older than 7 days).
  - Jobs: `analyze_video` queue `video` lock `video:<device_id>`, `transcribe_audio` queue `audio` lock `asr`, `index_marker` queue `light`; queueing lock `item:<id>`; `caption_keyframe` queue `caption` queueing lock `kf:<id>`; `embed_moments` queue `light` queueing lock `embed`.
  - Fixture `jobs_open` (job app opened on the test pool); `client` now depends on it.
  - Helpers: `T0`, `new_ulid()`, `async insert_item(pool, device_id, *, kind="video", started_at=T0, seconds=60, item_id=None, raw_path=None, status="ready", meta=None) -> str`, `fetch_all`, `fetch_one`, `jobs_by_lock(pool) -> dict[str, list[dict]]`.

Rulings in this task: the job is deferred **inside the transaction that locks the row** (`SELECT … FOR UPDATE`, then `enqueue` in a savepoint, then `UPDATE … SET analysis_job_id`), so a crash can never leave a job without its id on the row, and the fast path and any number of dispatcher ticks serialise on the row lock. A `todo` job already holding the queueing lock is adopted. A row whose job failed keeps the job id, so it is never re-deferred (the retry button re-runs that job). The dispatcher feeds the backlog a little at a time: for each task (`analyze_video`, `transcribe_audio`, `index_marker`, `caption_keyframe`) it defers rows, oldest first, only while fewer than `LOOPI_DISPATCH_MAX_QUEUED` (default 20) jobs of that task wait (`todo`, which includes jobs waiting for a retry), and never more than 200 per task per tick. The upload fast path is never capped. **Each tick checks the model server once** (`Router.health()`: one `GET /api/tags`, 5 s timeout): it defers caption jobs only if the server is reachable and has the caption model, and one `embed_moments` run only if moments wait for an embedding and the embed model is there; otherwise it skips that work for this tick and logs one line. Keyframes then keep `caption_job_id` NULL and whatever error a job recorded — no job is created just to meet a stopped server. Item jobs (video, audio, markers) need no model server and are never held back. Retry waits are 5 s, 25 s, 125 s: Procrastinate runs jobs that share a lock strictly in order, so a job waiting for its retry holds back every later job of the same device's videos (or all ASR); long waits would stall the pipeline behind one bad file. No job priorities: because the backlog keeps at most ~20 waiting jobs per task, a fresh upload waits behind at most that many jobs of its kind (for ASR, ≈ 20 × the time of one transcription), never behind the whole backlog. Stalled jobs (their worker died: native crash, out of memory, forced stop) go back to `todo` until their attempts are used up; one that has already run 4 times ends `failed`, on the dead-letter list, with "WorkerLost: …" stored on its row — a file that kills the worker can no longer loop forever.

- [ ] **Step 1: Write the failing tests**

Append to `brain/tests/helpers.py`:

```python


T0 = dt.datetime(2026, 10, 14, 1, 0, tzinfo=dt.UTC)


def new_ulid() -> str:
    from ulid import ULID

    return str(ULID())


async def insert_item(
    pool,
    device_id: str,
    *,
    kind: str = "video",
    started_at: dt.datetime = T0,
    seconds: float = 60,
    item_id: str | None = None,
    raw_path: str | None = None,
    status: str = "ready",
    meta: dict | None = None,
) -> str:
    """Insert an item row directly (no file is written unless the test does it); return its id."""
    from psycopg.types.json import Jsonb

    item_id = item_id or new_ulid()
    ext = {"video": "h264", "audio": "opus", "marker": "json"}[kind]
    raw_path = raw_path or f"raw/{device_id}/{started_at:%Y/%m/%d}/{item_id}.{ext}"
    if meta is None:
        meta = {"video_fps": 2} if kind == "video" else {}
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO item (id, device_id, kind, started_at, ended_at, sha256, size_bytes, raw_path, meta, status)"
            " VALUES (%s, %s, %s, %s, %s, 'x', 1, %s, %s, %s)",
            (item_id, device_id, kind, started_at, started_at + dt.timedelta(seconds=seconds), raw_path,
             Jsonb(meta), status),
        )
    return item_id


async def fetch_all(pool, sql: str, params: tuple = ()) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def fetch_one(pool, sql: str, params: tuple = ()) -> dict | None:
    rows = await fetch_all(pool, sql, params)
    return rows[0] if rows else None


async def jobs_by_lock(pool) -> dict[str, list[dict]]:
    """Procrastinate jobs grouped by queueing lock."""
    grouped: dict[str, list[dict]] = {}
    for row in await fetch_all(pool, "SELECT * FROM procrastinate_jobs ORDER BY id"):
        grouped.setdefault(row["queueing_lock"], []).append(row)
    return grouped
```

Replace `brain/tests/conftest.py` with:

```python
import httpx
import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

from loopi_brain.config import Settings
from loopi_brain.db import make_pool, migrate

PG_IMAGE = "pgvector/pgvector:pg17"
APP_TABLES = (
    "moment, transcript_segment, keyframe, device_command, item, pair_token, device,"
    " procrastinate_events, procrastinate_periodic_defers, procrastinate_jobs, procrastinate_workers"
)


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
```

Create `brain/tests/test_dispatch.py`:

```python
import asyncio
import datetime as dt
import json
import types

import pytest

from helpers import (
    T0,
    FakeOllama,
    fetch_all,
    fetch_one,
    insert_item,
    item_headers,
    jobs_by_lock,
    new_ulid,
    paired_device,
)
from loopi_brain.pipeline import dispatch, jobs
from loopi_brain.providers.config import DEFAULT_CONFIG
from loopi_brain.providers.router import build_router


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def ollama():
    return FakeOllama()


@pytest.fixture
def router(pki_settings, ollama):
    return build_router(DEFAULT_CONFIG, pki_settings, ollama.client())


async def _insert_keyframe(pool, item_id, device_id, ts, caption=None) -> str:
    kf_id = new_ulid()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO keyframe (id, item_id, device_id, ts, frame_index, path, dhash, luma, caption)"
            " VALUES (%s, %s, %s, %s, %s, %s, 0, 100, %s)",
            (kf_id, item_id, device_id, ts, int(ts.timestamp()) % 1000, f"keyframes/{kf_id}.jpg",
             json.dumps(caption) if caption else None),
        )
    return kf_id


async def test_defer_item_creates_one_job_per_kind_and_records_it(pool, dev, jobs_open):
    device_id = dev["device_id"]
    video = await insert_item(pool, device_id, kind="video")
    audio = await insert_item(pool, device_id, kind="audio")
    marker = await insert_item(pool, device_id, kind="marker")
    for item_id in (video, audio, marker):
        assert await dispatch.defer_item(pool, item_id) is not None
        assert await dispatch.defer_item(pool, item_id) is None  # already has a job
    by_lock = await jobs_by_lock(pool)
    expected = {
        video: (jobs.ANALYZE_VIDEO, "video", f"video:{device_id}"),
        audio: (jobs.TRANSCRIBE_AUDIO, "audio", "asr"),
        marker: (jobs.INDEX_MARKER, "light", None),
    }
    for item_id, (task, queue, lock) in expected.items():
        [job] = by_lock[f"item:{item_id}"]
        assert (job["task_name"], job["queue_name"], job["lock"], job["args"]) == (task, queue, lock, {"item_id": item_id})
        row = await fetch_one(pool, "SELECT analysis_job_id FROM item WHERE id = %s", (item_id,))
        assert row["analysis_job_id"] == job["id"]


async def test_todo_job_holding_the_queueing_lock_is_adopted(pool, dev, jobs_open):
    item_id = await insert_item(pool, dev["device_id"], kind="marker")
    async with pool.connection() as conn, conn.transaction():  # e.g. deferred, then the row update was lost
        orphan = await jobs.enqueue(conn, jobs.INDEX_MARKER, queue="light", queueing_lock=f"item:{item_id}", item_id=item_id)
    assert await dispatch.defer_item(pool, item_id) == orphan
    assert len((await jobs_by_lock(pool))[f"item:{item_id}"]) == 1


async def test_backlog_and_fast_path_race_give_each_item_exactly_one_job(pool, pki_settings, router, jobs_open):
    devices = [(await paired_device(pool, pki_settings))["device_id"] for _ in range(3)]
    ids = [
        await insert_item(pool, devices[n % 3], kind=("video", "audio", "marker")[n % 3],
                          started_at=T0 + dt.timedelta(minutes=n))
        for n in range(300)
    ]
    fast_path = [dispatch.defer_item(pool, i) for i in reversed(ids[::7])]
    tick = dispatch.dispatch_pending(pool, router, max_queued=300)  # no backlog cap: this tests exactly-once
    await asyncio.gather(tick, dispatch.dispatch_pending(pool, router, max_queued=300), *fast_path)
    by_lock = await jobs_by_lock(pool)
    assert sorted(by_lock) == sorted(f"item:{i}" for i in ids)
    assert all(len(v) == 1 for v in by_lock.values())
    rows = await fetch_all(pool, "SELECT id, analysis_job_id FROM item")
    assert {r["id"]: r["analysis_job_id"] for r in rows} == {i: by_lock[f"item:{i}"][0]["id"] for i in ids}


async def _run_all_waiting(pool):
    """Pretend the worker ran every waiting job."""
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'succeeded' WHERE status = 'todo'")


async def test_dispatcher_defers_oldest_first_so_device_order_follows_started_at(pool, dev, router, jobs_open):
    started = [T0 + dt.timedelta(minutes=m) for m in (5, 1, 3, 2, 4)]
    ids = {await insert_item(pool, dev["device_id"], started_at=s): s for s in started}
    for expected in (2, 2, 1):
        assert await dispatch.dispatch_pending(pool, router, max_queued=2) == (expected, 0)
        await _run_all_waiting(pool)
    rows = await fetch_all(
        pool, "SELECT j.id, j.args->>'item_id' AS item_id FROM procrastinate_jobs j ORDER BY j.id"
    )
    assert [ids[r["item_id"]] for r in rows] == sorted(started)


async def test_backlog_is_fed_a_few_jobs_at_a_time_and_uploads_skip_the_line(pool, dev, router, jobs_open):
    for n in range(100):
        await insert_item(pool, dev["device_id"], kind="audio", started_at=T0 + dt.timedelta(minutes=n))
    for n in range(3):
        await insert_item(pool, dev["device_id"], kind="marker", started_at=T0 + dt.timedelta(minutes=n), seconds=0)
    assert await dispatch.dispatch_pending(pool, router) == (23, 0)  # 20 of the audio backlog, every marker
    assert await dispatch.dispatch_pending(pool, router) == (0, 0)  # 20 transcriptions already wait
    fresh = await insert_item(pool, dev["device_id"], kind="audio", started_at=T0 + dt.timedelta(days=1))
    assert await dispatch.defer_item(pool, fresh) is not None  # the upload fast path is never capped
    async with pool.connection() as conn:  # the worker finishes 15 of the 21 waiting transcriptions
        await conn.execute(
            "UPDATE procrastinate_jobs SET status = 'succeeded' WHERE id IN (SELECT id FROM procrastinate_jobs"
            " WHERE status = 'todo' AND task_name = 'transcribe_audio' ORDER BY id LIMIT 15)"
        )
    assert await dispatch.dispatch_pending(pool, router) == (14, 0)  # topped up to 20 waiting again
    waiting = await fetch_one(
        pool, "SELECT count(*) AS n FROM procrastinate_jobs WHERE status = 'todo' AND task_name = 'transcribe_audio'"
    )
    assert waiting["n"] == 20


async def test_done_skipped_and_dead_lettered_items_are_not_redeferred(pool, dev, router, jobs_open):
    done = await insert_item(pool, dev["device_id"])
    skipped = await insert_item(pool, dev["device_id"], kind="audio")
    failed = await insert_item(pool, dev["device_id"], kind="marker")
    job_id = await dispatch.defer_item(pool, failed)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (done,))
        await conn.execute("UPDATE item SET analysis_state = 'skipped' WHERE id = %s", (skipped,))
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed' WHERE id = %s", (job_id,))
    assert await dispatch.dispatch_pending(pool, router) == (0, 0)
    assert [r["id"] for r in await fetch_all(pool, "SELECT id FROM procrastinate_jobs")] == [job_id]


async def _done_video_with_keyframes(pool, device_id) -> tuple[str, str]:
    """A done video item with one uncaptioned and one captioned keyframe; returns (item, waiting keyframe)."""
    item_id = await insert_item(pool, device_id)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (item_id,))
    waiting = await _insert_keyframe(pool, item_id, device_id, T0)
    await _insert_keyframe(pool, item_id, device_id, T0 + dt.timedelta(minutes=1), caption={"scene": "x"})
    return item_id, waiting


async def test_healthy_tick_gives_uncaptioned_keyframes_one_caption_job(pool, dev, router, jobs_open):
    _item, waiting = await _done_video_with_keyframes(pool, dev["device_id"])
    assert await dispatch.dispatch_pending(pool, router) == (0, 1)
    assert await dispatch.dispatch_pending(pool, router) == (0, 0)
    [job] = (await jobs_by_lock(pool))[f"kf:{waiting}"]
    assert (job["task_name"], job["queue_name"], job["args"]) == (jobs.CAPTION_KEYFRAME, "caption", {"keyframe_id": waiting})
    row = await fetch_one(pool, "SELECT caption_job_id FROM keyframe WHERE id = %s", (waiting,))
    assert row["caption_job_id"] == job["id"]


async def test_outage_tick_defers_no_caption_job(pool, dev, router, ollama, jobs_open, caplog):
    _item, waiting = await _done_video_with_keyframes(pool, dev["device_id"])
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption_error = 'ProviderUnavailable: down' WHERE id = %s", (waiting,))
    ollama.down = True
    assert await dispatch.dispatch_pending(pool, router) == (0, 0)
    assert await jobs_by_lock(pool) == {}
    row = await fetch_one(pool, "SELECT caption_job_id, caption_error FROM keyframe WHERE id = %s", (waiting,))
    assert row == {"caption_job_id": None, "caption_error": "ProviderUnavailable: down"}
    assert "model server is unreachable: captions and embeddings wait for the next tick" in caplog.text
    ollama.down = False
    assert await dispatch.dispatch_pending(pool, router) == (0, 1)


async def test_tick_without_the_caption_model_defers_no_caption_job(pool, dev, ollama, router, jobs_open, caplog):
    await _done_video_with_keyframes(pool, dev["device_id"])
    ollama.models.remove("minicpm-v4.6:1b")
    assert await dispatch.dispatch_pending(pool, router) == (0, 0)
    assert await jobs_by_lock(pool) == {}
    assert "model server lacks minicpm-v4.6:1b: captions wait for the next tick" in caplog.text
    assert [p for p in ollama.paths() if p != "/api/tags"] == []  # one health check per tick, nothing else


async def _moment_waiting(pool, device_id):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, model_version)"
            " VALUES (%s, %s, now(), now(), 'marker', 'x', 'test')",
            (new_ulid(), device_id),
        )


async def test_tick_asks_for_embeddings_only_when_moments_wait_and_the_model_is_there(
    pool, dev, ollama, router, jobs_open
):
    assert await dispatch.dispatch_pending(pool, router) == (0, 0)
    assert "embed" not in await jobs_by_lock(pool)  # nothing waits
    await _moment_waiting(pool, dev["device_id"])
    ollama.models.remove("qwen3-embedding:0.6b")
    await dispatch.dispatch_pending(pool, router)
    assert "embed" not in await jobs_by_lock(pool)  # model missing
    ollama.models.append("qwen3-embedding:0.6b")
    await dispatch.dispatch_pending(pool, router)
    await dispatch.dispatch_pending(pool, router)
    [job] = (await jobs_by_lock(pool))["embed"]
    assert job["task_name"] == jobs.EMBED_MOMENTS


async def test_embedding_runs_are_coalesced(pool, jobs_open):
    await dispatch.defer_embed(pool)
    await dispatch.defer_embed(pool)
    [job] = (await jobs_by_lock(pool))["embed"]
    assert (job["task_name"], job["queue_name"], job["status"]) == (jobs.EMBED_MOMENTS, "light", "todo")


async def test_upload_defers_analysis_on_the_fast_path(client, pool, dev):
    body = json.dumps({"note": "chìa khoá"}).encode()
    item_id = new_ulid()
    r = await client.put(f"/v1/items/{item_id}", content=body,
                         headers=item_headers(dev["fingerprint"], body, kind="marker", end="2026-10-14T01:00:00Z"))
    assert r.status_code == 201
    row = await fetch_one(pool, "SELECT analysis_state, analysis_job_id FROM item WHERE id = %s", (item_id,))
    [job] = (await jobs_by_lock(pool))[f"item:{item_id}"]
    assert row == {"analysis_state": "pending", "analysis_job_id": job["id"]}


async def test_fast_path_failure_never_affects_the_upload(client, pool, dev, router, monkeypatch, caplog):
    async def broken(*_a, **_k):
        raise RuntimeError("queue down")

    monkeypatch.setattr(jobs, "enqueue", broken)
    body = b'{"note": "x"}'
    item_id = new_ulid()
    r = await client.put(f"/v1/items/{item_id}", content=body,
                         headers=item_headers(dev["fingerprint"], body, kind="marker", end="2026-10-14T01:00:00Z"))
    assert r.status_code == 201
    assert f"could not defer analysis of item {item_id}" in caplog.text
    monkeypatch.undo()
    assert await dispatch.dispatch_pending(pool, router) == (1, 0)


async def test_stalled_jobs_are_retried_until_their_attempts_are_used_up(pool, dev, jobs_open):
    first = await insert_item(pool, dev["device_id"], kind="marker", seconds=0)
    killer = await insert_item(pool, dev["device_id"], kind="audio")
    running = await insert_item(pool, dev["device_id"], kind="marker", seconds=0, started_at=T0 + dt.timedelta(hours=1))
    jobs_by_item = {i: await dispatch.defer_item(pool, i) for i in (first, killer, running)}
    async with pool.connection() as conn:
        dead = (await (await conn.execute(
            "INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now() - interval '5 minutes') RETURNING id"
        )).fetchone())["id"]
        alive = (await (await conn.execute(
            "INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now()) RETURNING id"
        )).fetchone())["id"]
        for item_id, worker_id, attempts in ((first, dead, 1), (killer, dead, 3), (running, alive, 0)):
            await conn.execute(
                "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s, attempts = %s WHERE id = %s",
                (worker_id, attempts, jobs_by_item[item_id]),
            )
    assert await dispatch.recover_stalled(pool) == (1, 1)
    states = {
        r["id"]: (r["status"], r["attempts"])
        for r in await fetch_all(pool, "SELECT id, status, attempts FROM procrastinate_jobs")
    }
    assert states[jobs_by_item[first]] == ("todo", 2)
    assert states[jobs_by_item[killer]] == ("failed", 4)  # on the dead-letter list
    assert states[jobs_by_item[running]] == ("doing", 0)  # its worker is alive
    row = await fetch_one(pool, "SELECT analysis_error FROM item WHERE id = %s", (killer,))
    assert row["analysis_error"] == dispatch.STALLED_ERROR


async def test_enqueue_reraises_when_the_lock_holder_left_the_queue(pool, monkeypatch, jobs_open):
    from procrastinate.exceptions import AlreadyEnqueued

    class Taken:
        async def defer_async(self, **_kwargs):
            raise AlreadyEnqueued("taken")

    monkeypatch.setattr(jobs.job_app, "configure_task", lambda *_a, **_k: Taken())
    async with pool.connection() as conn, conn.transaction():
        with pytest.raises(AlreadyEnqueued):  # no 'todo' holder to adopt: the dispatcher retries next tick
            await jobs.enqueue(conn, jobs.INDEX_MARKER, queue="light", queueing_lock="item:gone", item_id="gone")


async def test_remove_old_jobs_keeps_failed_jobs(pool, dev, jobs_open):
    ok_item = await insert_item(pool, dev["device_id"], kind="marker")
    bad_item = await insert_item(pool, dev["device_id"], kind="marker")
    ok_job = await dispatch.defer_item(pool, ok_item)
    bad_job = await dispatch.defer_item(pool, bad_item)
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'succeeded' WHERE id = %s", (ok_job,))
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed' WHERE id = %s", (bad_job,))
        await conn.execute("UPDATE procrastinate_events SET at = now() - interval '8 days'")
    await dispatch.remove_old_jobs(types.SimpleNamespace(app=jobs.job_app))
    assert [r["id"] for r in await fetch_all(pool, "SELECT id FROM procrastinate_jobs")] == [bad_job]


async def test_periodic_tasks_are_registered():
    periodic = {(p.task.name, p.cron) for p in jobs.job_app.periodic_registry.periodic_tasks.values()}
    assert {
        (jobs.DISPATCH_PENDING, "* * * * *"),
        (jobs.RETRY_STALLED, "*/10 * * * *"),
        (jobs.REMOVE_OLD_JOBS, "15 4 * * *"),
    } <= periodic


async def test_api_lifespan_opens_the_job_app_on_its_pool(pool, pki_settings):
    from procrastinate.exceptions import AppNotOpen

    from loopi_brain.app import create_app

    app = create_app(pki_settings, pool=pool)
    async with app.router.lifespan_context(app):
        assert jobs.job_app.connector.pool is pool  # the retry button needs it
    with pytest.raises(AppNotOpen):
        _ = jobs.job_app.connector.pool
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_dispatch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'loopi_brain.pipeline'` (every P1 test that uses `client` fails the same way until Step 3 creates `pipeline/jobs.py`, because `client` now needs `jobs_open`).

- [ ] **Step 3: Write the job app**

Create `brain/src/loopi_brain/pipeline/__init__.py`:

```python
"""Perception pipeline: jobs, keyframes, captions, ASR, moments (spec §9.2)."""
```

Create `brain/src/loopi_brain/pipeline/jobs.py`:

```python
"""The Procrastinate job app: task names, queues, retry policy, and how jobs are deferred."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import procrastinate
from procrastinate.exceptions import AlreadyEnqueued
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

# Modules that register tasks on job_app; the worker imports them all before fetching jobs.
TASK_MODULES = [
    "loopi_brain.pipeline.dispatch",
]

ANALYZE_VIDEO = "analyze_video"
CAPTION_KEYFRAME = "caption_keyframe"
TRANSCRIBE_AUDIO = "transcribe_audio"
INDEX_MARKER = "index_marker"
EMBED_MOMENTS = "embed_moments"
DISPATCH_PENDING = "dispatch_pending"
RETRY_STALLED = "retry_stalled_jobs"
REMOVE_OLD_JOBS = "remove_old_jobs"
PIPELINE_TASKS = (ANALYZE_VIDEO, CAPTION_KEYFRAME, TRANSCRIBE_AUDIO, INDEX_MARKER, EMBED_MOMENTS)

# Spec §12: a failed job is retried 3 times, then stays 'failed' (the dead-letter list).
# RetryStrategy compares max_attempts with the attempts already made (0 on the first run),
# so max_attempts=3 means at most 4 runs. Waits are short (5 s, 25 s, 125 s) because a job
# waiting for its retry holds back every later job with the same lock (a device's videos, all ASR).
RETRY = procrastinate.RetryStrategy(max_attempts=3, exponential_wait=5)

job_app = procrastinate.App(connector=procrastinate.PsycopgConnector(), import_paths=TASK_MODULES)


@asynccontextmanager
async def open_jobs(pool: AsyncConnectionPool) -> AsyncIterator[procrastinate.App]:
    """Open ``job_app`` on ``pool`` for the duration of the block.

    A fresh connector per block: an opened PsycopgConnector keeps its first pool forever
    (open_async is a no-op once a pool is set), so reusing it across pools would break tests
    and restarts.
    """
    with job_app.replace_connector(procrastinate.PsycopgConnector()):
        async with job_app.open_async(pool=pool):
            yield job_app


async def enqueue(
    conn: AsyncConnection,
    task: str,
    *,
    queue: str,
    queueing_lock: str,
    lock: str | None = None,
    **task_kwargs,
) -> int:
    """Defer ``task`` inside the caller's transaction and return the job id.

    If a 'todo' job already holds ``queueing_lock``, return that job's id instead.
    """
    try:
        async with conn.transaction():  # savepoint: a unique violation must not abort the caller
            deferrer = job_app.configure_task(
                task, queue=queue, lock=lock, queueing_lock=queueing_lock, connection=conn
            )
            return await deferrer.defer_async(**task_kwargs)
    except AlreadyEnqueued:
        cur = await conn.execute(
            "SELECT id FROM procrastinate_jobs WHERE queueing_lock = %s AND status = 'todo'",
            (queueing_lock,),
        )
        row = await cur.fetchone()
        if row is None:  # the holder started running in between; the dispatcher retries next tick
            raise
        return row["id"]
```

Create `brain/src/loopi_brain/pipeline/context.py`:

```python
"""Per-process pipeline context: what every job needs, set once by the worker."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from psycopg_pool import AsyncConnectionPool

from ..config import Settings
from ..providers.router import Router

if TYPE_CHECKING:
    from .asr import Transcriber


@dataclass
class PipelineContext:
    pool: AsyncConnectionPool
    settings: Settings
    router: Router
    transcriber: Transcriber


_context: PipelineContext | None = None


def set_context(ctx: PipelineContext | None) -> None:
    global _context
    _context = ctx


def get_context() -> PipelineContext:
    if _context is None:
        raise RuntimeError("pipeline context not set: jobs run only inside `loopi-brain worker`")
    return _context
```

- [ ] **Step 4: Write the dispatcher and the periodic maintenance tasks**

Create `brain/src/loopi_brain/pipeline/dispatch.py`:

```python
"""Turn rows that still need work into jobs, exactly once (API fast path + periodic dispatcher)."""
from __future__ import annotations

import logging

from procrastinate import builtin_tasks
from procrastinate.exceptions import AlreadyEnqueued
from procrastinate.jobs import Status
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from ..providers.router import Router
from . import jobs
from .context import get_context
from .jobs import job_app

log = logging.getLogger(__name__)
DISPATCH_BATCH = 200  # hard cap per task per tick
DISPATCH_MAX_QUEUED = 20  # default of Settings.dispatch_max_queued
OLD_JOBS_HOURS = 7 * 24
ITEM_KINDS = ("video", "audio", "marker")
STALLED_ERROR = "WorkerLost: the worker stopped while running this job (crash, out of memory or forced stop)"


def _item_job(kind: str, device_id: str) -> tuple[str, str, str | None]:
    """(task, queue, lock) for an item of ``kind``."""
    if kind == "video":
        return jobs.ANALYZE_VIDEO, "video", f"video:{device_id}"  # keyframe order per device
    if kind == "audio":
        return jobs.TRANSCRIBE_AUDIO, "audio", "asr"  # one Whisper run at a time
    return jobs.INDEX_MARKER, "light", None


async def _defer_item(conn: AsyncConnection, item_id: str) -> int | None:
    cur = await conn.execute(
        "SELECT kind, device_id FROM item"
        " WHERE id = %s AND analysis_state = 'pending' AND analysis_job_id IS NULL FOR UPDATE",
        (item_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    task, queue, lock = _item_job(row["kind"], row["device_id"])
    job_id = await jobs.enqueue(conn, task, queue=queue, lock=lock, queueing_lock=f"item:{item_id}", item_id=item_id)
    await conn.execute("UPDATE item SET analysis_job_id = %s WHERE id = %s", (job_id, item_id))
    return job_id


async def defer_item(pool: AsyncConnectionPool, item_id: str) -> int | None:
    """Defer the analysis job of a pending item that has none yet; return the job id (None if nothing to do).

    Errors are logged, never raised: this runs after the upload was acknowledged, and the
    dispatcher picks up anything missed here.
    """
    try:
        async with pool.connection() as conn, conn.transaction():
            return await _defer_item(conn, item_id)
    except Exception:
        log.exception("could not defer analysis of item %s", item_id)
        return None


async def defer_keyframe(pool: AsyncConnectionPool, keyframe_id: str) -> int | None:
    """Defer the caption job of an uncaptioned keyframe that has none yet (errors are logged)."""
    try:
        async with pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT id FROM keyframe WHERE id = %s AND caption IS NULL AND caption_job_id IS NULL FOR UPDATE",
                (keyframe_id,),
            )
            if await cur.fetchone() is None:
                return None
            job_id = await jobs.enqueue(
                conn, jobs.CAPTION_KEYFRAME, queue="caption", queueing_lock=f"kf:{keyframe_id}", keyframe_id=keyframe_id
            )
            await conn.execute("UPDATE keyframe SET caption_job_id = %s WHERE id = %s", (job_id, keyframe_id))
            return job_id
    except Exception:
        log.exception("could not defer caption of keyframe %s", keyframe_id)
        return None


async def defer_keyframes(pool: AsyncConnectionPool, keyframe_ids: list[str]) -> None:
    for keyframe_id in keyframe_ids:
        await defer_keyframe(pool, keyframe_id)


async def defer_embed(pool: AsyncConnectionPool) -> None:
    """Ask for an embedding run; one waiting run is enough (errors are logged)."""
    try:
        async with pool.connection() as conn, conn.transaction():
            await jobs.enqueue(conn, jobs.EMBED_MOMENTS, queue="light", queueing_lock="embed")
    except AlreadyEnqueued:
        pass
    except Exception:
        log.exception("could not defer the embedding job")


async def _queued(pool: AsyncConnectionPool) -> dict[str, int]:
    """Jobs waiting ('todo', including those waiting for a retry) per task name."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT task_name, count(*) AS n FROM procrastinate_jobs WHERE status = 'todo' GROUP BY task_name"
        )
        return {r["task_name"]: r["n"] for r in await cur.fetchall()}


def _room(queued: dict[str, int], task: str, max_queued: int) -> int:
    return max(0, min(DISPATCH_BATCH, max_queued - queued.get(task, 0)))


async def dispatch_pending(
    pool: AsyncConnectionPool, router: Router, max_queued: int = DISPATCH_MAX_QUEUED
) -> tuple[int, int]:
    """One dispatcher tick. Returns (items deferred, keyframes deferred).

    Backlog work is fed a little at a time: for each task, rows are deferred (oldest first) only
    while fewer than ``max_queued`` jobs of that task wait, so a fresh upload — deferred by the
    uncapped fast path — never queues behind a whole backlog. Caption jobs and the embedding run
    are deferred only if the model server can serve them right now (one health check per tick);
    otherwise that work is skipped with one log line, and no job is created just to fail.
    """
    queued = await _queued(pool)
    items = keyframes = 0
    for kind in ITEM_KINDS:
        room = _room(queued, _item_job(kind, "")[0], max_queued)
        if not room:
            continue
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM item WHERE analysis_state = 'pending' AND analysis_job_id IS NULL AND kind = %s"
                " ORDER BY started_at, id LIMIT %s",
                (kind, room),
            )
            item_ids = [r["id"] for r in await cur.fetchall()]
        for item_id in item_ids:
            items += await defer_item(pool, item_id) is not None

    health = await router.health()  # one GET /api/tags with a short timeout
    caption_ready = router.task_ready("caption", health)
    embed_ready = router.task_ready("embed", health)
    room = _room(queued, jobs.CAPTION_KEYFRAME, max_queued)
    if caption_ready and room:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM keyframe WHERE caption IS NULL AND caption_job_id IS NULL ORDER BY ts, id LIMIT %s",
                (room,),
            )
            keyframe_ids = [r["id"] for r in await cur.fetchall()]
        for keyframe_id in keyframe_ids:
            keyframes += await defer_keyframe(pool, keyframe_id) is not None
    if embed_ready:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT EXISTS (SELECT 1 FROM moment WHERE embedding IS NULL) AS waiting")
            waiting = (await cur.fetchone())["waiting"]
        if waiting:
            await defer_embed(pool)
    if not (caption_ready and embed_ready):
        state = f"lacks {', '.join(health.missing)}" if health.reachable else "is unreachable"
        paused = " and ".join(name for name, ready in (("captions", caption_ready), ("embeddings", embed_ready))
                              if not ready)
        log.warning("model server %s: %s wait for the next tick", state, paused)
    if items or keyframes:
        log.info("dispatched %d item jobs and %d caption jobs", items, keyframes)
    return items, keyframes


async def recover_stalled(pool: AsyncConnectionPool) -> tuple[int, int]:
    """Jobs left 'doing' by a dead worker go back to 'todo' (Procrastinate's recipe) — unless they
    already used their attempts: a job that keeps killing the worker (native crash, out of memory)
    ends 'failed', on the dead-letter list, with the reason stored on its row.

    Returns (retried, failed).
    """
    retried = failed = 0
    for job in await job_app.job_manager.get_stalled_jobs():
        if job.attempts < jobs.RETRY.max_attempts:
            log.warning("retrying stalled job %s (%s)", job.id, job.task_name)
            await job_app.job_manager.retry_job(job)
            retried += 1
            continue
        log.error("job %s (%s) stopped the worker %d times; dead-lettered", job.id, job.task_name, job.attempts + 1)
        await job_app.job_manager.finish_job_by_id_async(job.id, status=Status.FAILED, delete_job=False)
        target = job.task_kwargs
        async with pool.connection() as conn:
            if "item_id" in target:
                await conn.execute("UPDATE item SET analysis_error = %s WHERE id = %s", (STALLED_ERROR, target["item_id"]))
            elif "keyframe_id" in target:
                await conn.execute(
                    "UPDATE keyframe SET caption_error = %s WHERE id = %s", (STALLED_ERROR, target["keyframe_id"])
                )
        failed += 1
    return retried, failed


@job_app.periodic(cron="* * * * *")
@job_app.task(name=jobs.DISPATCH_PENDING, queue="light", queueing_lock="dispatch")
async def dispatch_pending_job(timestamp: int | None = None) -> None:
    ctx = get_context()
    await dispatch_pending(ctx.pool, ctx.router, ctx.settings.dispatch_max_queued)


@job_app.periodic(cron="*/10 * * * *")
@job_app.task(name=jobs.RETRY_STALLED, queue="light", queueing_lock="retry_stalled")
async def retry_stalled_jobs(timestamp: int | None = None) -> None:
    await recover_stalled(get_context().pool)


@job_app.periodic(cron="15 4 * * *")
@job_app.task(name=jobs.REMOVE_OLD_JOBS, queue="light", queueing_lock="remove_old_jobs", pass_context=True)
async def remove_old_jobs(context, timestamp: int | None = None) -> None:
    """Delete succeeded jobs older than 7 days; failed jobs stay (they are the dead-letter list)."""
    await builtin_tasks.remove_old_jobs(context, max_hours=OLD_JOBS_HOURS)
```

- [ ] **Step 5: Defer on upload, open the job app in the API lifespan**

In `brain/src/loopi_brain/api/device_routes.py` add the import below `from .. import devices, items, media, pki`:

```python
from ..pipeline import dispatch
```

and replace the end of `put_item` (from `if status == 201 and header.kind == "video":` to the `return`) with:

```python
    if status == 201:
        # Fast path into the pipeline; the periodic dispatcher catches anything this misses.
        background.add_task(dispatch.defer_item, request.app.state.pool, header.id)
        if header.kind == "video":
            background.add_task(media.remux_item, request.app.state.pool, settings, header.id)
    return JSONResponse(ack, status_code=status)
```

Replace `brain/src/loopi_brain/app.py` with:

```python
"""FastAPI application factory."""
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from psycopg_pool import AsyncConnectionPool

from . import media
from .api.device_routes import router as device_router
from .config import Settings
from .db import make_pool
from .pipeline.jobs import open_jobs
from .web.routes import router as web_router

log = logging.getLogger(__name__)


def create_app(settings: Settings, pool: AsyncConnectionPool | None = None) -> FastAPI:
    owns_pool = pool is None
    active_pool = pool if pool is not None else make_pool(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if owns_pool:
            await active_pool.open()
        try:
            async with open_jobs(active_pool):  # the API defers jobs and retries failed ones
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

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(device_router)
    app.include_router(web_router)
    settings.media_root.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=settings.media_root), name="media")
    app.mount(
        "/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static"
    )
    return app
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_dispatch.py -v`
Expected: PASS (18 tests).

Run: `cd brain && uv run pytest -q`
Expected: all pass — P1 uploads now also create `index_marker`/`analyze_video`/`transcribe_audio` jobs, which no P1 test inspects.

- [ ] **Step 7: Commit**

```bash
git add brain/src/loopi_brain/pipeline brain/src/loopi_brain/api/device_routes.py brain/src/loopi_brain/app.py \
  brain/tests/conftest.py brain/tests/helpers.py brain/tests/test_dispatch.py
git commit -F - <<'EOF'
feat(brain): Procrastinate job app with idempotent dispatch and an upload fast path

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Pre-flight rulings for this task (controller, 2026-10-15; they amend the steps above):**
- **R-F2 (dead-letter pile-up):** the queueing lock `embed` covers only `todo` jobs, so a persistent embedding error would add a new failed `embed_moments` row every few minutes and push real failures off the status page. In `dispatch.py` add `async def prune_failed_embed_runs(pool) -> int` that deletes every failed `embed_moments` job except the newest one (`DELETE FROM procrastinate_jobs WHERE task_name = %s AND status = 'failed' AND id < (SELECT max(id) FROM procrastinate_jobs WHERE task_name = %s AND status = 'failed')`, task name from `jobs.EMBED_MOMENTS`; events cascade) and returns the number deleted; `dispatch_pending` calls it right before `defer_embed`. Add a test to `tests/test_dispatch.py`: two failed embed runs + one healthy tick with a moment waiting → exactly one failed `embed_moments` row is left (the newest) and one new `todo` run exists. Update the test count you expect accordingly.
- **R-F4 (shared moment helper):** add `async def insert_moment(pool, device_id: str, *, kind: str = "marker", ts: dt.datetime | None = None, text: str = "x", lang: str | None = None) -> str` to `tests/helpers.py` (inserts `id, device_id, ts_start, ts_end, kind, text, lang, model_version='test'`; `ts` defaults to now; both timestamps = `ts`; returns the id) and use it in `test_dispatch.py` instead of `_moment_waiting`. Tasks 5 and 9 import it instead of defining their own. (`_insert_keyframe` stays test-local: it writes no JPEG and takes a caption, unlike Task 6's `helpers.insert_keyframe`.)

---

### Task 4: Keyframe selection and `analyze_video`

**Files:**
- Create: `brain/src/loopi_brain/pipeline/frames.py`, `state.py`, `video.py`
- Modify: `brain/src/loopi_brain/pipeline/jobs.py` (`TASK_MODULES`)
- Modify: `brain/tests/helpers.py` (append)
- Test: `brain/tests/test_frames.py`, `brain/tests/test_video.py`

**Interfaces:**
- Consumes: Task 3 `dispatch.defer_keyframes`, `jobs.job_app`, `jobs.RETRY`, `jobs.ANALYZE_VIDEO`, `context.get_context`, `PipelineContext`; Task 1 `keyframe` table and `Settings.keyframe_*`; P1 `devices.new_id()`.
- Produces:
  - `frames`: `SMALL_W, SMALL_H = 64, 36`, `DHASH_DEADBAND = 2`, `FrameError`, `KeyframeParams(dark_luma=24.0, motion_max=8.0, hash_threshold=12, min_gap_s=30.0)` + `KeyframeParams.from_settings(settings)`, `Selected(index, ts, dhash, luma)`, `dhash(frame) -> int` (unsigned 64-bit), `hamming(a, b) -> int`, `to_signed64`, `to_unsigned64`, `motion_scores(frames) -> np.ndarray`, `select_keyframes(frames, fps, start, last_hash, last_ts, params) -> list[Selected]`, `keyframe_rel_path(device_id, ts, keyframe_id) -> PurePosixPath`, `async read_small_frames(src, fps) -> np.ndarray (N, 36, 64) uint8`, `async extract_jpeg(src, fps, index, dst)`.
  - `state`: `error_text(exc) -> str` (≤ 500 chars, `Type: …message tail`), `async record_item_error(pool, item_id, exc)`, `async record_keyframe_error(pool, keyframe_id, exc)`, `async finish_item(pool, item_id, state)`, `async item_exists(pool, item_id) -> bool`, `async keyframe_exists(pool, keyframe_id) -> bool`.
  - `video`: `async analyze_video(ctx, item_id) -> list[str]` (new keyframe ids), task `analyze_video_job(item_id)`.
  - Helpers: `make_h264(path, source, seconds, fps=2)`, `TWO_SCENES`, `NIGHT`, `make_ctx(pool, settings, *, ollama=None, transcriber=None) -> PipelineContext`.

Rulings in this task: a dHash bit is 1 only where the right neighbour is brighter by **more than 2 grey levels** (`DHASH_DEADBAND`), so sensor noise on a still, dim or flat scene cannot flip bits and spend a keyframe every 30 s (tested: an hour of a still noisy scene gives one keyframe; the busy-hour bound is unchanged). `skipped` = nothing to analyse — the raw file is gone or the item's remux ended in `status = 'error'`. Frames are read with `-fps_mode passthrough` so ffmpeg never duplicates or drops frames (keyframe time = `started_at + index / fps`). The previous keyframe of the device is the latest one with `ts < started_at` of this item. JPEGs are written first, rows and `analysis_state = 'done'` then commit in one transaction that first locks the item row; if that transaction fails the JPEGs are deleted. **A row deleted before or during its job is a no-op** (the job succeeds and writes nothing): the transaction finds the item gone and removes the JPEGs, and an error raised after the item disappeared (e.g. its raw file was deleted first) is not recorded or raised. The smoke test's cleanup (Task 10) relies on this.

- [ ] **Step 1: Write the failing tests**

Append to `brain/tests/helpers.py`:

```python


def make_h264(path, source: str, seconds: float, fps: int = 2):
    """Encode an ffmpeg lavfi ``source`` (e.g. ``testsrc2=size=640x360``) as raw Annex-B H.264."""
    import shutil
    import subprocess

    import pytest

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required for video tests")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source, "-t", str(seconds),
         "-r", str(fps), "-c:v", "libx264", "-g", str(fps * 2), "-pix_fmt", "yuv420p",
         "-bsf:v", "h264_mp4toannexb", "-f", "h264", str(path)],
        check=True,
    )
    return path


# 40 s of testsrc2 then 40 s of SMPTE bars at 640x360: two scenes, one hard cut at frame 80.
TWO_SCENES = (
    "testsrc2=size=640x360:rate=2:duration=40[a];"
    "smptebars=size=640x360:rate=2:duration=40[b];[a][b]concat=n=2:v=1:a=0"
)
NIGHT = "color=c=0x101010:size=640x360:rate=2"


def make_ctx(pool, settings, *, ollama=None, transcriber=None):
    """A PipelineContext whose Ollama is ``ollama`` (a FakeOllama; a fresh one if None)."""
    from loopi_brain.pipeline.context import PipelineContext
    from loopi_brain.providers.config import DEFAULT_CONFIG
    from loopi_brain.providers.router import build_router

    ollama = ollama or FakeOllama()
    return PipelineContext(pool, settings, build_router(DEFAULT_CONFIG, settings, ollama.client()), transcriber)
```

Create `brain/tests/test_frames.py`:

```python
import datetime as dt

import numpy as np
import pytest
from PIL import Image

from helpers import NIGHT, T0, TWO_SCENES, make_h264
from loopi_brain.config import Settings
from loopi_brain.pipeline import frames
from loopi_brain.pipeline.frames import (
    SMALL_H,
    SMALL_W,
    FrameError,
    KeyframeParams,
    dhash,
    hamming,
    select_keyframes,
    to_signed64,
    to_unsigned64,
)

PARAMS = KeyframeParams()
HOUR_AT_2FPS = 2 * 3600


def _frame(value: int) -> np.ndarray:
    return np.full((SMALL_H, SMALL_W), value, dtype=np.uint8)


def _textures(n: int, seed: int = 7, lo: int = 124, hi: int = 133) -> np.ndarray:
    """n frames, each a different random 6x8 block pattern of grey levels lo..hi-1. With the default
    low contrast every frame looks new (dHash differs) but nothing moves fast (mean absolute
    difference about 2.7 grey levels)."""
    coarse = np.random.default_rng(seed).integers(lo, hi, size=(n, 6, 8)).astype(np.uint8)
    return coarse.repeat(6, axis=1).repeat(8, axis=2)


def test_dhash_is_64_bits_and_round_trips_through_bigint():
    h = dhash(_textures(1)[0])
    assert 0 <= h < 1 << 64
    assert dhash(_frame(90)) == 0  # a flat frame has no gradients
    for value in (0, 1, (1 << 63) - 1, 1 << 63, (1 << 64) - 1):
        signed = to_signed64(value)
        assert -(1 << 63) <= signed < 1 << 63
        assert to_unsigned64(signed) == value
    assert hamming(0, (1 << 64) - 1) == 64 and hamming(h, h) == 0


def test_keyframe_params_come_from_settings():
    s = Settings(database_url="postgresql://x", keyframe_dark_luma=10, keyframe_motion_max=3,
                 keyframe_hash_threshold=20, keyframe_min_gap_s=60)
    assert KeyframeParams.from_settings(s) == KeyframeParams(10, 3, 20, 60)


def test_night_hour_gives_no_keyframes():
    night = np.random.default_rng(1).integers(0, 30, size=(HOUR_AT_2FPS, SMALL_H, SMALL_W), dtype=np.uint8)
    assert select_keyframes(night, 2, T0, None, None, PARAMS) == []


def test_busy_hour_is_bounded_by_the_minimum_gap():
    busy = _textures(HOUR_AT_2FPS)
    picks = select_keyframes(busy, 2, T0, None, None, PARAMS)
    gaps = [(b.ts - a.ts).total_seconds() for a, b in zip(picks, picks[1:], strict=False)]
    assert 100 <= len(picks) <= 121  # at most 2 per minute, never one per frame
    assert min(gaps) >= PARAMS.min_gap_s


def test_still_noisy_scene_keeps_one_keyframe_per_hour():
    rng = np.random.default_rng(3)
    dim_flat = np.clip(40 + rng.normal(0, 3, size=(HOUR_AT_2FPS, SMALL_H, SMALL_W)), 0, 255).astype(np.uint8)
    room = _textures(1, seed=5, lo=60, hi=200)[0].astype(float)  # a fixed, contrasted scene
    still_room = np.clip(room + rng.normal(0, 1.5, size=(HOUR_AT_2FPS, SMALL_H, SMALL_W)), 0, 255).astype(np.uint8)
    for clip in (dim_flat, still_room):  # sensor noise only: nothing new happens all hour
        assert len(select_keyframes(clip, 2, T0, None, None, PARAMS)) == 1


def test_fast_motion_frames_are_skipped():
    still = _frame(120)
    moving = [_frame(200 if i % 2 else 120) for i in range(10)]  # flickers by 80 grey levels; frame 9 is 200
    clip = np.stack([*moving, still, still])
    picks = select_keyframes(clip, 2, T0, None, None, PARAMS)
    assert [p.index for p in picks] == [11]  # frame 10 still differs from 9 by 80; frame 11 is still


def test_same_scene_as_previous_keyframe_is_not_new_and_new_scene_is():
    scene = _textures(1, seed=1, lo=60, hi=200)[0]
    other = _textures(1, seed=2, lo=60, hi=200)[0]
    clip = np.stack([scene] * 100 + [other] * 100)
    last_ts = T0 - dt.timedelta(minutes=5)
    picks = select_keyframes(clip, 2, T0, dhash(scene), last_ts, PARAMS)
    assert [p.index for p in picks] == [101]  # 100 is the cut (motion); 101 is the first still frame
    assert picks[0].ts == T0 + dt.timedelta(seconds=50.5)
    assert picks[0].dhash == dhash(other) and picks[0].luma == pytest.approx(other.mean())


def test_first_keyframe_waits_for_the_gap_after_the_previous_one():
    clip = _textures(120)
    picks = select_keyframes(clip, 2, T0, 0, T0 - dt.timedelta(seconds=10), PARAMS)
    assert picks[0].ts - T0 >= dt.timedelta(seconds=20)


def test_empty_input_selects_nothing():
    assert select_keyframes(np.zeros((0, SMALL_H, SMALL_W), np.uint8), 2, T0, None, None, PARAMS) == []
    one = select_keyframes(_textures(1), 2, T0, None, None, PARAMS)
    assert [p.index for p in one] == [0]


def test_keyframe_path_uses_the_utc_date():
    seoul = dt.timezone(dt.timedelta(hours=9))
    ts = dt.datetime(2026, 10, 15, 8, 30, tzinfo=seoul)  # 2026-10-14 23:30 UTC
    assert str(frames.keyframe_rel_path("DEV", ts, "KF")) == "keyframes/DEV/2026/10/14/KF.jpg"


async def test_read_small_frames_and_extract_full_resolution_jpeg(tmp_path):
    src = make_h264(tmp_path / "scenes.h264", TWO_SCENES, 80)
    small = await frames.read_small_frames(src, 2)
    assert small.shape == (160, SMALL_H, SMALL_W) and small.dtype == np.uint8
    picks = select_keyframes(small, 2, T0, None, None, PARAMS)
    assert [p.index for p in picks] == [0, 81]
    dst = tmp_path / "out" / "kf.jpg"
    await frames.extract_jpeg(src, 2, 81, dst)
    with Image.open(dst) as img:
        assert (img.format, img.size) == ("JPEG", (640, 360))
        seen = np.asarray(img.convert("L").resize((SMALL_W, SMALL_H), Image.Resampling.BOX), dtype=np.int16)
    assert [p.name for p in dst.parent.iterdir()] == ["kf.jpg"]
    # The JPEG is the selected frame itself: close to small frame 81, far from frame 79 (the other scene).
    assert np.abs(seen - small[81]).mean() < 6 < 20 < np.abs(seen - small[79]).mean()


async def test_night_video_decodes_to_dark_frames(tmp_path):
    src = make_h264(tmp_path / "night.h264", NIGHT, 30)
    small = await frames.read_small_frames(src, 2)
    assert len(small) == 60 and small.mean() < PARAMS.dark_luma
    assert select_keyframes(small, 2, T0, None, None, PARAMS) == []


async def test_ffmpeg_errors_raise_frame_error_and_leave_no_temp_files(tmp_path):
    garbage = tmp_path / "bad.h264"
    garbage.write_bytes(b"definitely not h264")
    with pytest.raises(FrameError):
        await frames.read_small_frames(garbage, 2)
    src = make_h264(tmp_path / "short.h264", TWO_SCENES, 2)
    with pytest.raises(FrameError):
        await frames.extract_jpeg(src, 2, 500, tmp_path / "kf" / "x.jpg")
    assert list((tmp_path / "kf").iterdir()) == []


async def test_ffmpeg_timeout_kills_the_process(tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ffmpeg").write_text("#!/bin/sh\nexec sleep 30\n")
    (fake / "ffmpeg").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")
    monkeypatch.setattr(frames, "FFMPEG_TIMEOUT_S", 0.3)
    with pytest.raises(FrameError, match="timed out"):
        await frames.read_small_frames(tmp_path / "x.h264", 2)
```

Create `brain/tests/test_video.py`:

```python
import datetime as dt

import pytest

from helpers import NIGHT, T0, TWO_SCENES, fetch_all, fetch_one, insert_item, jobs_by_lock, make_ctx, make_h264, paired_device
from loopi_brain.pipeline import context, video
from loopi_brain.pipeline.frames import FrameError


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def ctx(pool, pki_settings):
    return make_ctx(pool, pki_settings)


async def _video_item(pool, settings, device_id, source, seconds, started_at=T0) -> str:
    item_id = await insert_item(pool, device_id, kind="video", started_at=started_at, seconds=seconds)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    make_h264(settings.media_root / row["raw_path"], source, seconds)
    return item_id


def _jpegs(settings) -> list:
    root = settings.media_root / "keyframes"
    return sorted(root.rglob("*.jpg")) if root.exists() else []


async def test_new_scenes_become_keyframes_with_caption_jobs(pool, pki_settings, dev, ctx, jobs_open):
    item_id = await _video_item(pool, pki_settings, dev["device_id"], TWO_SCENES, 80)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_error = 'old failure' WHERE id = %s", (item_id,))
    ids = await video.analyze_video(ctx, item_id)
    rows = await fetch_all(pool, "SELECT * FROM keyframe WHERE item_id = %s ORDER BY ts", (item_id,))
    assert [r["id"] for r in rows] == ids and len(ids) == 2
    assert [r["ts"] for r in rows] == [T0, T0 + dt.timedelta(seconds=40.5)]
    assert [r["frame_index"] for r in rows] == [0, 81]
    for row in rows:
        assert row["path"] == f"keyframes/{dev['device_id']}/2026/10/14/{row['id']}.jpg"
        assert (pki_settings.media_root / row["path"]).is_file()
        assert row["device_id"] == dev["device_id"] and row["luma"] > 24 and row["caption"] is None
    item = await fetch_one(pool, "SELECT analysis_state, analyzed_at, analysis_error FROM item WHERE id = %s", (item_id,))
    assert item["analysis_state"] == "done" and item["analyzed_at"] is not None and item["analysis_error"] is None
    by_lock = await jobs_by_lock(pool)
    for row in await fetch_all(pool, "SELECT id, caption_job_id FROM keyframe"):
        assert by_lock[f"kf:{row['id']}"][0]["id"] == row["caption_job_id"]


async def test_night_segment_gives_no_keyframes_and_no_caption_jobs(pool, pki_settings, dev, ctx, jobs_open):
    item_id = await _video_item(pool, pki_settings, dev["device_id"], NIGHT, 60)
    assert await video.analyze_video(ctx, item_id) == []
    assert await fetch_all(pool, "SELECT id FROM keyframe") == []
    assert await jobs_by_lock(pool) == {}
    assert _jpegs(pki_settings) == []
    item = await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,))
    assert item["analysis_state"] == "done"


async def test_next_segment_compares_with_the_previous_keyframe_of_the_device(pool, pki_settings, dev, ctx, jobs_open):
    first = await _video_item(pool, pki_settings, dev["device_id"], TWO_SCENES, 80)
    await video.analyze_video(ctx, first)
    same_scene = await _video_item(pool, pki_settings, dev["device_id"], "smptebars=size=640x360:rate=2", 60,
                                   started_at=T0 + dt.timedelta(seconds=80))
    assert await video.analyze_video(ctx, same_scene) == []  # still the bars of the last keyframe
    other = await paired_device(pool, pki_settings)
    elsewhere = await _video_item(pool, pki_settings, other["device_id"], "smptebars=size=640x360:rate=2", 60,
                                  started_at=T0 + dt.timedelta(seconds=80))
    assert len(await video.analyze_video(ctx, elsewhere)) == 1  # another device has its own history


async def test_rerun_never_creates_a_second_set_of_keyframes(pool, pki_settings, dev, ctx, jobs_open):
    item_id = await _video_item(pool, pki_settings, dev["device_id"], TWO_SCENES, 80)
    first = await video.analyze_video(ctx, item_id)
    assert await video.analyze_video(ctx, item_id) == []  # already done
    async with pool.connection() as conn:  # crash after the commit, before the job was acknowledged
        await conn.execute("UPDATE item SET analysis_state = 'pending' WHERE id = %s", (item_id,))
    assert await video.analyze_video(ctx, item_id) == []
    assert [r["id"] for r in await fetch_all(pool, "SELECT id FROM keyframe ORDER BY ts")] == first
    assert len(_jpegs(pki_settings)) == 2
    item = await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,))
    assert item["analysis_state"] == "done"


async def test_missing_raw_file_or_failed_remux_is_skipped(pool, pki_settings, dev, ctx, jobs_open):
    gone = await insert_item(pool, dev["device_id"], kind="video")
    broken = await _video_item(pool, pki_settings, dev["device_id"], TWO_SCENES, 4, started_at=T0 + dt.timedelta(hours=1))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET status = 'error' WHERE id = %s", (broken,))
    for item_id in (gone, broken):
        assert await video.analyze_video(ctx, item_id) == []
        row = await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,))
        assert row["analysis_state"] == "skipped"


async def test_other_kinds_and_unknown_ids_are_ignored(pool, pki_settings, dev, ctx):
    audio = await insert_item(pool, dev["device_id"], kind="audio")
    assert await video.analyze_video(ctx, audio) == []
    assert await video.analyze_video(ctx, "01NOSUCHITEM0000000000000") == []
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (audio,)))["analysis_state"] == "pending"


async def test_undecodable_video_records_the_error_and_raises(pool, pki_settings, dev, ctx):
    item_id = await insert_item(pool, dev["device_id"], kind="video")
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    raw = pki_settings.media_root / row["raw_path"]
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"definitely not h264")
    with pytest.raises(FrameError):
        await video.analyze_video(ctx, item_id)
    item = await fetch_one(pool, "SELECT analysis_state, analysis_error FROM item WHERE id = %s", (item_id,))
    assert item["analysis_state"] == "pending" and item["analysis_error"].startswith("FrameError: ")
    assert len(item["analysis_error"]) <= 500


async def test_failed_transaction_removes_the_written_jpegs(pool, pki_settings, dev, ctx, monkeypatch):
    item_id = await _video_item(pool, pki_settings, dev["device_id"], TWO_SCENES, 80)
    monkeypatch.setattr(video.devices, "new_id", lambda: "01SAMEIDFORBOTHKEYFRAMES00")
    with pytest.raises(Exception, match="duplicate key"):
        await video.analyze_video(ctx, item_id)
    assert await fetch_all(pool, "SELECT id FROM keyframe") == []
    assert _jpegs(pki_settings) == []
    item = await fetch_one(pool, "SELECT analysis_state, analysis_error FROM item WHERE id = %s", (item_id,))
    assert item["analysis_state"] == "pending" and "duplicate key" in item["analysis_error"]


async def test_item_deleted_during_analysis_stores_nothing(pool, pki_settings, dev, ctx, jobs_open, monkeypatch):
    item_id = await _video_item(pool, pki_settings, dev["device_id"], TWO_SCENES, 80)
    real_extract = video.extract_jpeg

    async def extract_then_delete(src, fps, index, dst):
        await real_extract(src, fps, index, dst)
        async with pool.connection() as conn:  # e.g. the smoke test's cleanup
            await conn.execute("DELETE FROM item WHERE id = %s", (item_id,))

    monkeypatch.setattr(video, "extract_jpeg", extract_then_delete)
    assert await video.analyze_video(ctx, item_id) == []
    assert await fetch_all(pool, "SELECT id FROM keyframe") == []
    assert _jpegs(pki_settings) == [] and await jobs_by_lock(pool) == {}


async def test_item_deleted_with_its_raw_file_is_a_no_op(pool, pki_settings, dev, ctx, monkeypatch):
    item_id = await _video_item(pool, pki_settings, dev["device_id"], TWO_SCENES, 4)
    real_read = video.read_small_frames

    async def delete_then_read(src, fps):
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM item WHERE id = %s", (item_id,))
        src.unlink()
        return await real_read(src, fps)  # ffmpeg fails: the file is gone

    monkeypatch.setattr(video, "read_small_frames", delete_then_read)
    assert await video.analyze_video(ctx, item_id) == []  # no exception: the job succeeds


async def test_job_wrapper_uses_the_process_context(pool, pki_settings, dev, ctx, jobs_open):
    item_id = await _video_item(pool, pki_settings, dev["device_id"], NIGHT, 10)
    context.set_context(ctx)
    try:
        await video.analyze_video_job(item_id=item_id)
    finally:
        context.set_context(None)
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "done"
    with pytest.raises(RuntimeError, match="pipeline context not set"):
        context.get_context()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_frames.py tests/test_video.py -v`
Expected: FAIL — `ImportError: cannot import name 'frames' from 'loopi_brain.pipeline'`.

- [ ] **Step 3: Write the pure selection and the ffmpeg helpers**

Create `brain/src/loopi_brain/pipeline/frames.py`:

```python
"""Keyframe selection (spec §9.2 steps 3-4): small grey frames, dHash, darkness and motion filters."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import numpy as np
from PIL import Image

from ..config import Settings

SMALL_W, SMALL_H = 64, 36
FFMPEG_TIMEOUT_S = 300.0
STDERR_TAIL = 500
_U64 = 1 << 64
# Neighbouring cells must differ by more than this many grey levels to give a 1 bit, so sensor noise
# on flat or dim areas cannot flip bits (a still, noisy scene keeps one hash).
DHASH_DEADBAND = 2


class FrameError(Exception):
    """ffmpeg could not decode or extract frames."""


@dataclass(frozen=True)
class KeyframeParams:
    dark_luma: float = 24.0  # mean grey level below which a frame is "night"
    motion_max: float = 8.0  # mean absolute difference to the neighbouring frame above which it is blurred motion
    hash_threshold: int = 12  # dHash bits that must differ from the previous keyframe
    min_gap_s: float = 30.0  # minimum time between two keyframes of a device

    @classmethod
    def from_settings(cls, settings: Settings) -> KeyframeParams:
        return cls(
            dark_luma=settings.keyframe_dark_luma,
            motion_max=settings.keyframe_motion_max,
            hash_threshold=settings.keyframe_hash_threshold,
            min_gap_s=settings.keyframe_min_gap_s,
        )


@dataclass(frozen=True)
class Selected:
    index: int
    ts: dt.datetime
    dhash: int  # unsigned 64-bit
    luma: float


def dhash(frame: np.ndarray) -> int:
    """64-bit difference hash: BOX-resize to 9x8; a bit is 1 where the right neighbour is brighter
    by more than DHASH_DEADBAND grey levels."""
    small = np.asarray(Image.fromarray(frame).resize((9, 8), Image.Resampling.BOX), dtype=np.int16)
    value = 0
    for bit in ((small[:, 1:] - small[:, :-1]) > DHASH_DEADBAND).flatten():
        value = (value << 1) | int(bit)
    return value


def hamming(a: int, b: int) -> int:
    return ((a ^ b) % _U64).bit_count()


def to_signed64(value: int) -> int:
    """Unsigned 64-bit hash -> Postgres BIGINT."""
    return value - _U64 if value >= 1 << 63 else value


def to_unsigned64(value: int) -> int:
    """Postgres BIGINT -> unsigned 64-bit hash."""
    return value % _U64


def motion_scores(frames: np.ndarray) -> np.ndarray:
    """Mean absolute difference of each frame to the previous one (frame 0: to the next one)."""
    n = len(frames)
    if n < 2:
        return np.zeros(n)
    diffs = np.abs(np.diff(frames.astype(np.int16), axis=0)).reshape(n - 1, -1).mean(axis=1)
    return np.concatenate(([diffs[0]], diffs))


def select_keyframes(
    frames: np.ndarray,
    fps: int,
    start: dt.datetime,
    last_hash: int | None,
    last_ts: dt.datetime | None,
    params: KeyframeParams,
) -> list[Selected]:
    """Pick the frames that are really new: lit, still, far (in time and dHash) from the previous keyframe."""
    if len(frames) == 0:
        return []
    luma = frames.reshape(len(frames), -1).mean(axis=1)
    motion = motion_scores(frames)
    selected: list[Selected] = []
    for i in range(len(frames)):
        if luma[i] < params.dark_luma or motion[i] > params.motion_max:
            continue
        ts = start + dt.timedelta(seconds=i / fps)
        if last_ts is not None and (ts - last_ts).total_seconds() < params.min_gap_s:
            continue
        h = dhash(frames[i])
        if last_hash is not None and hamming(h, last_hash) < params.hash_threshold:
            continue
        selected.append(Selected(i, ts, h, float(luma[i])))
        last_hash, last_ts = h, ts
    return selected


def keyframe_rel_path(device_id: str, ts: dt.datetime, keyframe_id: str) -> PurePosixPath:
    """keyframes/<device>/YYYY/MM/DD/<id>.jpg, UTC date of the keyframe (same convention as raw/)."""
    return PurePosixPath("keyframes", device_id, ts.astimezone(dt.UTC).strftime("%Y/%m/%d"), f"{keyframe_id}.jpg")


async def _ffmpeg(args: list[str]) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        try:
            out, err = await asyncio.wait_for(proc.communicate(), FFMPEG_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise FrameError(f"ffmpeg timed out after {FFMPEG_TIMEOUT_S}s") from exc
        if proc.returncode != 0:
            raise FrameError(err.decode(errors="replace")[-STDERR_TAIL:] or f"ffmpeg exit {proc.returncode}")
        return out
    except BaseException:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
            await asyncio.shield(proc.wait())
        raise


async def read_small_frames(src: Path, fps: int) -> np.ndarray:
    """Decode every frame of a raw H.264 file as 64x36 grey -> array of shape (N, 36, 64), uint8."""
    out = await _ffmpeg([
        "-framerate", str(fps), "-f", "h264", "-i", str(src),
        "-vf", f"scale={SMALL_W}:{SMALL_H}:flags=area,format=gray", "-fps_mode", "passthrough",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ])
    size = SMALL_W * SMALL_H
    count = len(out) // size
    return np.frombuffer(out[: count * size], dtype=np.uint8).reshape(count, SMALL_H, SMALL_W)


async def extract_jpeg(src: Path, fps: int, index: int, dst: Path) -> None:
    """Write frame ``index`` of ``src`` at full resolution to ``dst`` (temp file, then rename)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.stem}.{uuid.uuid4().hex}.part.jpg")
    try:
        await _ffmpeg([
            "-y", "-framerate", str(fps), "-f", "h264", "-i", str(src),
            "-vf", f"select=eq(n\\,{index})", "-fps_mode", "passthrough", "-frames:v", "1", "-q:v", "3", str(tmp),
        ])
        if not tmp.is_file():
            raise FrameError(f"ffmpeg wrote no frame {index}")
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)
```

- [ ] **Step 4: Write the shared row-state helpers**

Create `brain/src/loopi_brain/pipeline/state.py`:

```python
"""Row state shared by every stage: finish an item, store a job's error on its row, check a row still exists.

Procrastinate keeps no error text, so each task writes the last 500 characters of its
error onto the row it works on before re-raising, and clears it on success. A row deleted
before or during its job (e.g. by the smoke test's cleanup) makes the job a no-op instead.
"""
from __future__ import annotations

import logging

from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)
ERROR_TAIL = 500


def error_text(exc: BaseException) -> str:
    """``Type: message``, at most 500 characters; long messages keep their end (ffmpeg puts the cause last)."""
    name = type(exc).__name__
    return f"{name}: {str(exc)[-(ERROR_TAIL - len(name) - 2):]}"


async def _store(pool: AsyncConnectionPool, sql: str, row_id: str, message: str) -> None:
    try:
        async with pool.connection() as conn:
            await conn.execute(sql, (message, row_id))
    except Exception:
        log.exception("could not record pipeline error for %s", row_id)


async def record_item_error(pool: AsyncConnectionPool, item_id: str, exc: BaseException) -> None:
    await _store(pool, "UPDATE item SET analysis_error = %s WHERE id = %s", item_id, error_text(exc))


async def record_keyframe_error(pool: AsyncConnectionPool, keyframe_id: str, exc: BaseException) -> None:
    await _store(pool, "UPDATE keyframe SET caption_error = %s WHERE id = %s", keyframe_id, error_text(exc))


async def finish_item(pool: AsyncConnectionPool, item_id: str, state: str) -> None:
    """Mark a pending item 'done' or 'skipped' and clear any error left by earlier attempts."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE item SET analysis_state = %s, analyzed_at = now(), analysis_error = NULL"
            " WHERE id = %s AND analysis_state = 'pending'",
            (state, item_id),
        )


async def item_exists(pool: AsyncConnectionPool, item_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM item WHERE id = %s", (item_id,))
        return await cur.fetchone() is not None


async def keyframe_exists(pool: AsyncConnectionPool, keyframe_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM keyframe WHERE id = %s", (keyframe_id,))
        return await cur.fetchone() is not None
```

- [ ] **Step 5: Write `analyze_video` and register its module**

Create `brain/src/loopi_brain/pipeline/video.py`:

```python
"""analyze_video: pick the new keyframes of one video segment and store them (spec §9.2 steps 3-4)."""
from __future__ import annotations

import logging
from pathlib import Path

from .. import devices
from . import dispatch, jobs
from .context import PipelineContext, get_context
from .frames import (
    KeyframeParams,
    extract_jpeg,
    keyframe_rel_path,
    read_small_frames,
    select_keyframes,
    to_signed64,
    to_unsigned64,
)
from .jobs import job_app
from .state import error_text, finish_item, item_exists, record_item_error

log = logging.getLogger(__name__)


async def _analyze(ctx: PipelineContext, item_id: str) -> list[str]:
    pool, settings = ctx.pool, ctx.settings
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT device_id, kind, started_at, raw_path, status, meta, analysis_state FROM item WHERE id = %s",
            (item_id,),
        )
        item = await cur.fetchone()
        if item is None or item["kind"] != "video" or item["analysis_state"] != "pending":
            return []
        cur = await conn.execute("SELECT count(*) AS n FROM keyframe WHERE item_id = %s", (item_id,))
        already = (await cur.fetchone())["n"]
        cur = await conn.execute(
            "SELECT ts, dhash FROM keyframe WHERE device_id = %s AND ts < %s ORDER BY ts DESC LIMIT 1",
            (item["device_id"], item["started_at"]),
        )
        previous = await cur.fetchone()
    if already:  # a previous run committed its keyframes; only the state is missing
        await finish_item(pool, item_id, "done")
        return []
    raw = settings.media_root / item["raw_path"]
    if item["status"] == "error" or not raw.is_file():
        await finish_item(pool, item_id, "skipped")
        return []

    fps = int(item["meta"].get("video_fps", settings.default_video_fps))
    frames = await read_small_frames(raw, fps)
    picks = select_keyframes(
        frames, fps, item["started_at"],
        to_unsigned64(previous["dhash"]) if previous else None,
        previous["ts"] if previous else None,
        KeyframeParams.from_settings(settings),
    )
    written: list[Path] = []
    rows = []
    stored = False
    try:
        for pick in picks:
            keyframe_id = devices.new_id()
            rel = keyframe_rel_path(item["device_id"], pick.ts, keyframe_id)
            await extract_jpeg(raw, fps, pick.index, settings.media_root / rel)
            written.append(settings.media_root / rel)
            rows.append((keyframe_id, item_id, item["device_id"], pick.ts, pick.index, str(rel),
                         to_signed64(pick.dhash), pick.luma))
        async with pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT 1 FROM item WHERE id = %s AND analysis_state = 'pending' FOR UPDATE", (item_id,)
            )
            if await cur.fetchone() is not None:  # still there and still pending
                for row in rows:
                    await conn.execute(
                        "INSERT INTO keyframe (id, item_id, device_id, ts, frame_index, path, dhash, luma)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        row,
                    )
                await conn.execute(
                    "UPDATE item SET analysis_state = 'done', analyzed_at = now(), analysis_error = NULL"
                    " WHERE id = %s",
                    (item_id,),
                )
                stored = True
    finally:
        if not stored:  # failed, or the item was deleted meanwhile: leave no JPEG behind
            for path in written:
                path.unlink(missing_ok=True)
    if not stored:
        log.info("item %s was deleted during its analysis; nothing stored", item_id)
        return []
    keyframe_ids = [row[0] for row in rows]
    log.info("item %s: %d of %d frames are keyframes", item_id, len(keyframe_ids), len(frames))
    await dispatch.defer_keyframes(pool, keyframe_ids)
    return keyframe_ids


async def analyze_video(ctx: PipelineContext, item_id: str) -> list[str]:
    """Select and store the keyframes of a pending video item; return the new keyframe ids."""
    try:
        return await _analyze(ctx, item_id)
    except Exception as exc:
        if not await item_exists(ctx.pool, item_id):  # deleted while we worked (e.g. its raw file went first)
            log.info("item %s was deleted during its analysis: %s", item_id, error_text(exc))
            return []
        await record_item_error(ctx.pool, item_id, exc)
        raise


@job_app.task(name=jobs.ANALYZE_VIDEO, queue="video", retry=jobs.RETRY)
async def analyze_video_job(item_id: str) -> None:
    await analyze_video(get_context(), item_id)
```

In `brain/src/loopi_brain/pipeline/jobs.py` set:

```python
TASK_MODULES = [
    "loopi_brain.pipeline.dispatch",
    "loopi_brain.pipeline.video",
]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_frames.py tests/test_video.py -v`
Expected: PASS (25 tests; needs `ffmpeg` with libx264 on PATH).

Run: `cd brain && uv run pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add brain/src/loopi_brain/pipeline/frames.py brain/src/loopi_brain/pipeline/state.py \
  brain/src/loopi_brain/pipeline/video.py brain/src/loopi_brain/pipeline/jobs.py \
  brain/tests/helpers.py brain/tests/test_frames.py brain/tests/test_video.py
git commit -F - <<'EOF'
feat(brain): select keyframes by darkness, motion, dHash and minimum gap

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Pre-flight rulings for this task (controller, 2026-10-15; they amend the steps above):**
- **R-F6 (one copy of the item-state SQL):** add to `pipeline/state.py` `async def lock_pending_item(conn, item_id: str) -> bool` (`SELECT 1 FROM item WHERE id = %s AND analysis_state = 'pending' FOR UPDATE`; true when a row came back) and `async def mark_item_done(conn, item_id: str) -> None` (`UPDATE item SET analysis_state = 'done', analyzed_at = now(), analysis_error = NULL WHERE id = %s`), and use them in `video.py` instead of the inline statements, inside the transaction that already holds the row. `finish_item` stays as it is (it opens its own connection, sets 'done' or 'skipped' and guards on `analysis_state = 'pending'`). Tasks 5 and 7 use the two new helpers too. Behavior and test counts do not change.

---

### Task 5: Marker moments and text embeddings

**Files:**
- Create: `brain/src/loopi_brain/pipeline/moments.py`
- Modify: `brain/src/loopi_brain/pipeline/jobs.py` (`TASK_MODULES`)
- Test: `brain/tests/test_moments.py`

**Interfaces:**
- Consumes: Task 2 `Router.embed`, `ProviderUnavailable`; Task 3 `dispatch.defer_embed`, `jobs.*`, `get_context`; Task 4 `state.record_item_error`, helpers `make_ctx`.
- Produces: `moments.MARKER_TEXT = "Marked moment"`, `MARKER_VERSION = "loopi:marker-v1"`, `EMBED_BATCH = 32`, `marker_text(meta) -> str`, `async index_marker(ctx, item_id) -> bool`, `async embed_pending(ctx, batch=32) -> int`; tasks `index_marker_job(item_id)` and `embed_moments_job()` (queueing lock `embed`; deferred when moments are written and by the dispatcher's health-gated tick — not a periodic task of its own).

This task comes before captions because a caption defers `embed_moments`, and Task 6 runs a real worker that must find that task.

Rulings in this task: a marker's moment text is its `note` (trimmed) or "Marked moment"; re-indexing replaces the marker's moment. Embedding documents carry no instruction prefix (P2b adds one to queries). A model server outage ends an embedding run quietly (logged); the dispatcher asks for a new run on the first tick the embed model is reachable. Vectors of the wrong size are a `ProviderError` (the job fails and retries).

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_moments.py`:

```python
import datetime as dt

import pytest

from helpers import T0, FakeOllama, fetch_all, fetch_one, insert_item, jobs_by_lock, make_ctx, new_ulid, paired_device
from loopi_brain.pipeline import moments
from loopi_brain.providers.base import ProviderError


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


async def _moment(pool, device_id, text, minute=0) -> str:
    moment_id = new_ulid()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, model_version)"
            " VALUES (%s, %s, %s, %s, 'marker', %s, 'test')",
            (moment_id, device_id, T0 + dt.timedelta(minutes=minute), T0 + dt.timedelta(minutes=minute), text),
        )
    return moment_id


@pytest.mark.parametrize(
    ("meta", "text"),
    [({"note": " chìa khoá "}, "chìa khoá"), ({}, "Marked moment"), ({"note": "  "}, "Marked moment"),
     ({"note": 42}, "Marked moment")],
)
async def test_marker_becomes_one_moment(pool, pki_settings, dev, jobs_open, meta, text):
    item_id = await insert_item(pool, dev["device_id"], kind="marker", seconds=0, meta=meta)
    ctx = make_ctx(pool, pki_settings)
    assert await moments.index_marker(ctx, item_id) is True
    assert await moments.index_marker(ctx, item_id) is False  # already done
    [moment] = await fetch_all(pool, "SELECT * FROM moment")
    assert (moment["kind"], moment["text"], moment["item_id"], moment["ts_start"]) == ("marker", text, item_id, T0)
    assert moment["model_version"] == "loopi:marker-v1" and moment["lang"] is None
    item = await fetch_one(pool, "SELECT analysis_state, analysis_error FROM item WHERE id = %s", (item_id,))
    assert item == {"analysis_state": "done", "analysis_error": None}
    assert "embed" in await jobs_by_lock(pool)


async def test_reindexing_a_marker_replaces_its_moment(pool, pki_settings, dev, jobs_open):
    item_id = await insert_item(pool, dev["device_id"], kind="marker", seconds=0)
    ctx = make_ctx(pool, pki_settings)
    await moments.index_marker(ctx, item_id)
    async with pool.connection() as conn:  # e.g. a crash before the job was acknowledged
        await conn.execute("UPDATE item SET analysis_state = 'pending' WHERE id = %s", (item_id,))
    assert await moments.index_marker(ctx, item_id) is True
    assert len(await fetch_all(pool, "SELECT id FROM moment")) == 1


async def test_other_items_are_not_markers(pool, pki_settings, dev):
    video = await insert_item(pool, dev["device_id"], kind="video")
    assert await moments.index_marker(make_ctx(pool, pki_settings), video) is False
    assert await moments.index_marker(make_ctx(pool, pki_settings), "01NOSUCHITEM0000000000000") is False


async def test_marker_failure_is_recorded_on_the_item(pool, pki_settings, dev, monkeypatch):
    item_id = await insert_item(pool, dev["device_id"], kind="marker", seconds=0)

    def boom(_meta):
        raise RuntimeError("bad marker")

    monkeypatch.setattr(moments, "marker_text", boom)
    with pytest.raises(RuntimeError):
        await moments.index_marker(make_ctx(pool, pki_settings), item_id)
    item = await fetch_one(pool, "SELECT analysis_state, analysis_error FROM item WHERE id = %s", (item_id,))
    assert item == {"analysis_state": "pending", "analysis_error": "RuntimeError: bad marker"}


async def test_embed_pending_embeds_everything_in_batches(pool, pki_settings, dev):
    for n in range(70):
        await _moment(pool, dev["device_id"], f"moment {n}", minute=n)
    fake = FakeOllama()
    assert await moments.embed_pending(make_ctx(pool, pki_settings, ollama=fake)) == 70
    assert [len(body["input"]) for _, body in fake.requests] == [32, 32, 6]
    assert all(body["model"] == "qwen3-embedding:0.6b" for _, body in fake.requests)
    rows = await fetch_all(pool, "SELECT embedding, embed_model FROM moment")
    assert all(r["embed_model"] == "ollama:qwen3-embedding:0.6b" and len(r["embedding"].to_list()) == 1024 for r in rows)
    assert await moments.embed_pending(make_ctx(pool, pki_settings, ollama=fake)) == 0


async def test_embedding_pauses_while_the_model_server_is_down(pool, pki_settings, dev, caplog):
    await _moment(pool, dev["device_id"], "hello")
    fake = FakeOllama()
    fake.down = True
    assert await moments.embed_pending(make_ctx(pool, pki_settings, ollama=fake)) == 0
    assert "embedding paused" in caplog.text
    assert (await fetch_one(pool, "SELECT embedding FROM moment"))["embedding"] is None


async def test_wrong_dimensions_fail_the_embedding_job(pool, pki_settings, dev):
    await _moment(pool, dev["device_id"], "hello")
    with pytest.raises(ProviderError, match="1024"):
        await moments.embed_pending(make_ctx(pool, pki_settings, ollama=FakeOllama(dims=768)))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_moments.py -v`
Expected: FAIL — `ImportError: cannot import name 'moments' from 'loopi_brain.pipeline'`.

- [ ] **Step 3: Write the module and register it**

Create `brain/src/loopi_brain/pipeline/moments.py`:

```python
"""Marker moments and text embeddings of moments (spec §9.2 "Hợp nhất")."""
from __future__ import annotations

import logging

from pgvector import Vector

from .. import devices
from ..providers.base import ProviderUnavailable
from . import dispatch, jobs
from .context import PipelineContext, get_context
from .jobs import job_app
from .state import record_item_error

log = logging.getLogger(__name__)
MARKER_TEXT = "Marked moment"
MARKER_VERSION = "loopi:marker-v1"
EMBED_BATCH = 32


def marker_text(meta: dict) -> str:
    note = meta.get("note")
    return note.strip() if isinstance(note, str) and note.strip() else MARKER_TEXT


async def _index(ctx: PipelineContext, item_id: str) -> bool:
    pool = ctx.pool
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "SELECT device_id, kind, started_at, ended_at, meta, analysis_state FROM item WHERE id = %s FOR UPDATE",
            (item_id,),
        )
        item = await cur.fetchone()
        if item is None or item["kind"] != "marker" or item["analysis_state"] != "pending":
            return False
        await conn.execute("DELETE FROM moment WHERE item_id = %s AND kind = 'marker'", (item_id,))
        await conn.execute(
            "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, item_id, model_version)"
            " VALUES (%s, %s, %s, %s, 'marker', %s, %s, %s)",
            (devices.new_id(), item["device_id"], item["started_at"], item["ended_at"], marker_text(item["meta"]),
             item_id, MARKER_VERSION),
        )
        await conn.execute(
            "UPDATE item SET analysis_state = 'done', analyzed_at = now(), analysis_error = NULL WHERE id = %s",
            (item_id,),
        )
    await dispatch.defer_embed(pool)
    return True


async def index_marker(ctx: PipelineContext, item_id: str) -> bool:
    """Turn a pending marker item into one 'marker' moment; False if there was nothing to do."""
    try:
        return await _index(ctx, item_id)
    except Exception as exc:
        await record_item_error(ctx.pool, item_id, exc)
        raise


async def embed_pending(ctx: PipelineContext, batch: int = EMBED_BATCH) -> int:
    """Embed every moment that has no embedding yet, ``batch`` at a time; return how many were embedded.

    Documents carry no instruction prefix (queries will, in P2b). A model server outage is logged
    and ends the run: the dispatcher asks for another run once the server is back.
    """
    done = 0
    while True:
        async with ctx.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, text FROM moment WHERE embedding IS NULL ORDER BY created_at, id LIMIT %s", (batch,)
            )
            rows = await cur.fetchall()
        if not rows:
            return done
        try:
            result = await ctx.router.embed("embed", [r["text"] for r in rows])
        except ProviderUnavailable as exc:
            log.warning("embedding paused, model server unavailable: %s", exc)
            return done
        async with ctx.pool.connection() as conn, conn.transaction():
            for row, vector in zip(rows, result.vectors, strict=True):
                await conn.execute(
                    "UPDATE moment SET embedding = %s, embed_model = %s WHERE id = %s AND embedding IS NULL",
                    (Vector(vector), result.model_version, row["id"]),
                )
        done += len(rows)


@job_app.task(name=jobs.INDEX_MARKER, queue="light", retry=jobs.RETRY)
async def index_marker_job(item_id: str) -> None:
    await index_marker(get_context(), item_id)


# Deferred when moments are written, and by the dispatcher every minute while moments wait and the
# embedding model is available (so an outage creates no job at all).
@job_app.task(name=jobs.EMBED_MOMENTS, queue="light", queueing_lock="embed", retry=jobs.RETRY)
async def embed_moments_job() -> None:
    await embed_pending(get_context())
```

In `brain/src/loopi_brain/pipeline/jobs.py` set:

```python
TASK_MODULES = [
    "loopi_brain.pipeline.dispatch",
    "loopi_brain.pipeline.video",
    "loopi_brain.pipeline.moments",
]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_moments.py -v`
Expected: PASS (10 tests).

Run: `cd brain && uv run pytest -q`
Expected: the whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain/pipeline/moments.py brain/src/loopi_brain/pipeline/jobs.py brain/tests/test_moments.py
git commit -F - <<'EOF'
feat(brain): marker moments and batched text embeddings of moments

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Pre-flight rulings for this task (controller, 2026-10-15; they amend the steps above):**
- **R-F4:** `tests/test_moments.py` uses `helpers.insert_moment` (added in Task 3) instead of defining `_moment`; keep the same arguments at the call sites (`text=`, `ts=T0 + dt.timedelta(minutes=minute)`).
- **R-F6:** `moments.py` marks the marker item done with `state.mark_item_done(conn, item_id)` (added in Task 4) instead of the inline UPDATE.

---

### Task 6: Keyframe captions with MiniCPM-V

**Files:**
- Create: `brain/src/loopi_brain/pipeline/caption.py`
- Modify: `brain/src/loopi_brain/pipeline/jobs.py` (`TASK_MODULES`)
- Modify: `brain/tests/helpers.py` (append)
- Test: `brain/tests/test_caption.py`

**Interfaces:**
- Consumes: Task 2 `Router.chat`, `ProviderUnavailable`, `ProviderError`; Task 3 `dispatch.defer_embed`, `dispatch.dispatch_pending(pool, router)`, `dispatch.defer_keyframe`; Task 4 `state.error_text`, `state.record_keyframe_error`, `state.keyframe_exists`; Task 5 `embed_moments` task.
- Produces: `caption.Caption` (pydantic: `scene`, `activity`, `text_seen` cut to 300 chars, `people_count` clamped to 0–50, `objects` cut to 20 strings of ≤ 300 chars, `notable: bool`), `MAX_TEXT`, `MAX_OBJECTS`, `MAX_PEOPLE`, `CAPTION_MAX_TOKENS = 400`, `RETRY_TEMPERATURE = 0.3`, `CaptionInvalid`, `CAPTION_SCHEMA` (with `maxLength`/`maxItems`/`minimum`/`maximum`), `CAPTION_PROMPT`, `parse_caption(text) -> Caption`, `caption_text(c) -> str`, `prepare_image(path) -> bytes`, `async caption_keyframe(ctx, keyframe_id, attempt=0) -> bool`, task `caption_keyframe_job(context, keyframe_id)` (`pass_context=True`, passes `context.job.attempts`); helpers `async run_jobs(ctx)`, `write_jpeg(path, size, color)`, `async insert_keyframe(pool, settings, item_id, device_id, ts, *, size) -> str`.

Rulings in this task: a model server that cannot serve the caption (`ProviderUnavailable`: connection refused, DNS failure, connect timeout, or HTTP 404 because the model is not pulled) clears `keyframe.caption_job_id`, stores the error on the keyframe and returns normally — the job succeeds without burning a retry. This is the second line of defence: the dispatcher (Task 3) already defers no caption job while the server is not ready, and defers this keyframe again on the first tick it is. A looping model is bounded three ways: the request sets `options.num_predict = 400`, the JSON schema given to the decoder carries the length and range limits, and whatever comes back is cut to the limits instead of rejected. Only unparseable JSON, a missing field or a wrong type raises `CaptionInvalid`; read timeouts and other HTTP errors raise `ProviderError`; both use the normal retries and stay on the row. A retry samples at temperature 0.3 instead of repeating the same greedy answer (the task gets its attempt count through `pass_context`). A keyframe deleted before or during its job is a no-op (nothing written, no error raised). An all-empty caption becomes the moment text "Unremarkable scene." (no empty moment text reaches the embedder).

- [ ] **Step 1: Write the failing tests**

Append to `brain/tests/helpers.py`:

```python


async def run_jobs(ctx) -> None:
    """Run every due job with a real Procrastinate worker, then return (periodic tasks off).

    Needs the job app open on ``ctx.pool`` (the ``jobs_open`` fixture).
    """
    from procrastinate.periodic import PeriodicRegistry

    from loopi_brain.pipeline import context
    from loopi_brain.pipeline.jobs import job_app

    registry = job_app.periodic_registry
    job_app.periodic_registry = PeriodicRegistry()
    context.set_context(ctx)
    try:
        await job_app.run_worker_async(wait=False, concurrency=1, install_signal_handlers=False, listen_notify=False)
    finally:
        context.set_context(None)
        job_app.periodic_registry = registry


def write_jpeg(path, size=(1280, 720), color=(90, 120, 150)):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="JPEG")
    return path


async def insert_keyframe(pool, settings, item_id: str, device_id: str, ts: dt.datetime, *, size=(1280, 720)) -> str:
    """Insert an uncaptioned keyframe row with a real JPEG on disk; return its id."""
    kf_id = new_ulid()
    rel = f"keyframes/{device_id}/{ts:%Y/%m/%d}/{kf_id}.jpg"
    write_jpeg(settings.media_root / rel, size)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO keyframe (id, item_id, device_id, ts, frame_index, path, dhash, luma)"
            " VALUES (%s, %s, %s, %s, (SELECT count(*) FROM keyframe WHERE item_id = %s), %s, 0, 100)",
            (kf_id, item_id, device_id, ts, item_id, rel),
        )
    return kf_id
```

Create `brain/tests/test_caption.py`:

```python
import base64
import datetime as dt
import io
import json

import psycopg
import pytest
from PIL import Image

from helpers import (
    DEFAULT_CAPTION,
    T0,
    FakeOllama,
    fetch_all,
    fetch_one,
    insert_item,
    insert_keyframe,
    item_headers,
    jobs_by_lock,
    make_ctx,
    new_ulid,
    paired_device,
    run_jobs,
    write_jpeg,
)
from loopi_brain.pipeline import caption, dispatch
from loopi_brain.pipeline.caption import CAPTION_SCHEMA, Caption, CaptionInvalid, caption_text
from loopi_brain.providers.base import ProviderError


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
async def keyframe(pool, pki_settings, dev):
    item_id = await insert_item(pool, dev["device_id"], kind="video")
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (item_id,))
    return await insert_keyframe(pool, pki_settings, item_id, dev["device_id"], T0)


def test_caption_text_reads_like_a_sentence():
    c = Caption(scene="A kitchen.", activity="Someone is cooking", people_count=2, objects=["pan", " ", "kettle"],
                text_seen="EXIT", notable=False)
    assert caption_text(c) == 'A kitchen. Someone is cooking. 2 people. Objects: pan, kettle. Text: "EXIT".'
    one = Caption(scene="A desk", activity="nobody", people_count=1, objects=[], text_seen="", notable=True)
    assert caption_text(one) == "A desk. nobody. 1 person."
    empty = Caption(scene=" ", activity="", people_count=0, objects=[], text_seen="", notable=False)
    assert caption_text(empty) == "Unremarkable scene."


def test_prepare_image_caps_the_longer_side(tmp_path):
    big = caption.prepare_image(write_jpeg(tmp_path / "big.jpg", (1920, 1080)))
    small = caption.prepare_image(write_jpeg(tmp_path / "small.jpg", (640, 360)))
    with Image.open(io.BytesIO(big)) as img:
        assert (img.format, img.size) == ("JPEG", (896, 504))
    with Image.open(io.BytesIO(small)) as img:
        assert img.size == (640, 360)


async def test_caption_is_stored_with_its_model_and_a_visual_moment(pool, pki_settings, dev, keyframe, jobs_open):
    fake = FakeOllama()
    assert await caption.caption_keyframe(make_ctx(pool, pki_settings, ollama=fake), keyframe) is True
    kf = await fetch_one(pool, "SELECT caption, caption_model, captioned_at, caption_error FROM keyframe WHERE id = %s",
                         (keyframe,))
    assert kf["caption"] == DEFAULT_CAPTION and kf["caption_model"] == "ollama:minicpm-v4.6:1b"
    assert kf["captioned_at"] is not None and kf["caption_error"] is None
    [moment] = await fetch_all(pool, "SELECT * FROM moment")
    assert (moment["kind"], moment["lang"], moment["keyframe_id"], moment["device_id"]) == (
        "visual", "en", keyframe, dev["device_id"])
    assert moment["ts_start"] == moment["ts_end"] == T0
    assert moment["text"] == caption_text(Caption(**DEFAULT_CAPTION))
    assert moment["model_version"] == "ollama:minicpm-v4.6:1b" and moment["embedding"] is None
    [(path, body)] = fake.requests
    assert path == "/api/chat" and body["format"] == CAPTION_SCHEMA and body["model"] == "minicpm-v4.6:1b"
    assert body["options"] == {"temperature": 0.0, "num_predict": 400}  # greedy, bounded output
    assert CAPTION_SCHEMA["properties"]["objects"]["maxItems"] == 20
    with Image.open(io.BytesIO(base64.b64decode(body["messages"][0]["images"][0]))) as img:
        assert img.size == (896, 504)
    assert "embed" in await jobs_by_lock(pool)
    assert await caption.caption_keyframe(make_ctx(pool, pki_settings, ollama=fake), keyframe) is False  # done already
    assert len(fake.requests) == 1


async def test_overlong_answers_are_cut_to_the_limits_not_rejected(pool, pki_settings, keyframe):
    rambling = {**DEFAULT_CAPTION, "scene": "a room " * 100, "people_count": 99,
                "objects": [f"chair {i}" for i in range(25)]}
    ctx = make_ctx(pool, pki_settings, ollama=FakeOllama(caption=rambling))
    assert await caption.caption_keyframe(ctx, keyframe) is True
    stored = (await fetch_one(pool, "SELECT caption FROM keyframe WHERE id = %s", (keyframe,)))["caption"]
    assert len(stored["scene"]) == 300 and stored["people_count"] == 50
    assert stored["objects"] == [f"chair {i}" for i in range(20)]


@pytest.mark.parametrize(
    "answer",
    [
        "not json at all",
        {**DEFAULT_CAPTION, "people_count": "many"},
        {**DEFAULT_CAPTION, "objects": "chair"},
        {k: v for k, v in DEFAULT_CAPTION.items() if k != "notable"},
    ],
)
async def test_invalid_answers_raise_and_keep_the_job(pool, pki_settings, keyframe, jobs_open, answer):
    await dispatch.defer_keyframe(pool, keyframe)
    with pytest.raises(CaptionInvalid):
        await caption.caption_keyframe(make_ctx(pool, pki_settings, ollama=FakeOllama(caption=answer)), keyframe)
    kf = await fetch_one(pool, "SELECT caption, caption_job_id, caption_error FROM keyframe WHERE id = %s", (keyframe,))
    assert kf["caption"] is None and kf["caption_job_id"] is not None
    assert kf["caption_error"].startswith("CaptionInvalid: ")
    assert await fetch_all(pool, "SELECT id FROM moment") == []


async def test_a_retried_caption_job_samples_instead_of_repeating_itself(pool, pki_settings, keyframe, jobs_open):
    fake = FakeOllama()
    job_id = await dispatch.defer_keyframe(pool, keyframe)
    async with pool.connection() as conn:  # as after one failed attempt
        await conn.execute("UPDATE procrastinate_jobs SET attempts = 1 WHERE id = %s", (job_id,))
    await run_jobs(make_ctx(pool, pki_settings, ollama=fake))
    [(_path, body)] = [r for r in fake.requests if r[0] == "/api/chat"]
    assert body["options"]["temperature"] == 0.3


async def test_server_errors_use_the_normal_retries(pool, pki_settings, keyframe, jobs_open):
    await dispatch.defer_keyframe(pool, keyframe)
    fake = FakeOllama()
    fake.error = 500
    with pytest.raises(ProviderError, match="HTTP 500"):
        await caption.caption_keyframe(make_ctx(pool, pki_settings, ollama=fake), keyframe)
    kf = await fetch_one(pool, "SELECT caption_job_id, caption_error FROM keyframe WHERE id = %s", (keyframe,))
    assert kf["caption_job_id"] is not None and "HTTP 500" in kf["caption_error"]


async def test_missing_caption_model_waits_like_an_outage(pool, pki_settings, keyframe, jobs_open):
    await dispatch.defer_keyframe(pool, keyframe)
    ctx = make_ctx(pool, pki_settings, ollama=FakeOllama(models=[]))
    assert await caption.caption_keyframe(ctx, keyframe) is False  # no exception: no retry is burned
    kf = await fetch_one(pool, "SELECT caption, caption_job_id, caption_error FROM keyframe WHERE id = %s", (keyframe,))
    assert kf["caption"] is None and kf["caption_job_id"] is None
    assert kf["caption_error"].startswith("ProviderUnavailable: ") and "HTTP 404" in kf["caption_error"]


async def test_model_server_outage_dead_letters_nothing_and_captions_resume(
    client, pool, pki_settings, dev, keyframe, jobs_open
):
    fake = FakeOllama()
    fake.down = True
    ctx = make_ctx(pool, pki_settings, ollama=fake)
    await dispatch.defer_keyframe(pool, keyframe)  # as analyze_video does right after extracting it
    await run_jobs(ctx)  # the job meets the outage: it gives the keyframe back without burning a retry
    for _tick in range(3):  # three dispatcher ticks while the model server is down: no new job at all
        assert await dispatch.dispatch_pending(pool, ctx.router) == (0, 0)
        await run_jobs(ctx)
        kf = await fetch_one(pool, "SELECT caption, caption_job_id, caption_error FROM keyframe WHERE id = %s",
                             (keyframe,))
        assert kf["caption"] is None and kf["caption_job_id"] is None
        assert kf["caption_error"].startswith("ProviderUnavailable: ")
    jobs = await fetch_all(pool, "SELECT status FROM procrastinate_jobs WHERE task_name = 'caption_keyframe'")
    assert [j["status"] for j in jobs] == ["succeeded"]
    body = json.dumps({"note": "still recording"}).encode()
    r = await client.put(f"/v1/items/{new_ulid()}", content=body,
                         headers=item_headers(dev["fingerprint"], body, kind="marker", end="2026-10-14T01:00:00Z"))
    assert r.status_code == 201  # ingest does not care about the model server

    fake.down = False  # the model server is back: the next tick captions the keyframe
    assert await dispatch.dispatch_pending(pool, ctx.router) == (0, 1)
    await run_jobs(ctx)
    kf = await fetch_one(pool, "SELECT caption, caption_error FROM keyframe WHERE id = %s", (keyframe,))
    assert kf["caption"] == DEFAULT_CAPTION and kf["caption_error"] is None
    assert [r["kind"] for r in await fetch_all(pool, "SELECT kind FROM moment WHERE keyframe_id = %s", (keyframe,))] == ["visual"]
    failed = await fetch_all(pool, "SELECT id FROM procrastinate_jobs WHERE status = 'failed'")
    assert failed == []


async def test_keyframe_deleted_while_the_model_answers_stores_nothing(pool, pki_settings, keyframe, jobs_open, monkeypatch):
    ctx = make_ctx(pool, pki_settings)
    real_chat = ctx.router.chat

    async def chat_while_deleted(*args, **kwargs):
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM keyframe WHERE id = %s", (keyframe,))
        return await real_chat(*args, **kwargs)

    monkeypatch.setattr(ctx.router, "chat", chat_while_deleted)
    assert await caption.caption_keyframe(ctx, keyframe) is False
    assert await fetch_all(pool, "SELECT id FROM moment") == []
    assert "embed" not in await jobs_by_lock(pool)


async def test_keyframe_deleted_with_its_jpeg_is_a_no_op(pool, pki_settings, dev, keyframe, monkeypatch):
    fake = FakeOllama()
    ctx = make_ctx(pool, pki_settings, ollama=fake)
    row = await fetch_one(pool, "SELECT item_id, path FROM keyframe WHERE id = %s", (keyframe,))
    async with pool.connection() as conn:  # gone before the job starts
        await conn.execute("DELETE FROM keyframe WHERE id = %s", (keyframe,))
    (pki_settings.media_root / row["path"]).unlink()
    assert await caption.caption_keyframe(ctx, keyframe) is False
    later = await insert_keyframe(pool, pki_settings, row["item_id"], dev["device_id"], T0 + dt.timedelta(minutes=1))
    real_prepare = caption.prepare_image

    def vanish_then_read(path):  # gone while the job runs: the JPEG first, then the row
        path.unlink()
        with psycopg.connect(pki_settings.database_url, autocommit=True) as conn:
            conn.execute("DELETE FROM keyframe WHERE id = %s", (later,))
        return real_prepare(path)

    monkeypatch.setattr(caption, "prepare_image", vanish_then_read)
    assert await caption.caption_keyframe(ctx, later) is False  # FileNotFoundError, but the row is gone too
    assert fake.requests == [] and await fetch_all(pool, "SELECT id FROM moment") == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_caption.py -v`
Expected: FAIL — `ImportError: cannot import name 'caption' from 'loopi_brain.pipeline'`.

- [ ] **Step 3: Write the module and register it**

Create `brain/src/loopi_brain/pipeline/caption.py`:

```python
"""caption_keyframe: describe one keyframe with the vision model (spec §9.2 step 5)."""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Annotated

import anyio
from PIL import Image
from procrastinate import JobContext
from psycopg.types.json import Jsonb
from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError, field_validator

from .. import devices
from ..providers.base import ProviderUnavailable
from . import dispatch, jobs
from .context import PipelineContext, get_context
from .jobs import job_app
from .state import error_text, keyframe_exists, record_keyframe_error

log = logging.getLogger(__name__)
MAX_SIDE = 896
JPEG_QUALITY = 85
CAPTION_PROMPT = (
    "This is one frame from a camera that stands still in a home. Describe it for a personal diary, in English. "
    "Answer with JSON: scene = the place and what is visible, one short sentence; activity = what the people "
    "are doing, or 'nobody' when nobody is visible; people_count = number of people visible; objects = up to 10 "
    "notable objects; text_seen = legible text, or an empty string; notable = true only if something unusual "
    "or memorable is happening."
)
MAX_TEXT = 300
MAX_OBJECTS = 20
MAX_PEOPLE = 50
# Output tokens per caption: a model that loops ("chair", "chair", …) stops here instead of filling its context.
CAPTION_MAX_TOKENS = 400
RETRY_TEMPERATURE = 0.3  # a retry samples instead of repeating the same greedy answer
# Sent as Ollama's `format`: the decoder is constrained to this shape and these limits. Caption below
# applies the same limits to whatever comes back, truncating instead of rejecting.
CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "scene": {"type": "string", "maxLength": MAX_TEXT},
        "activity": {"type": "string", "maxLength": MAX_TEXT},
        "people_count": {"type": "integer", "minimum": 0, "maximum": MAX_PEOPLE},
        "objects": {"type": "array", "items": {"type": "string", "maxLength": MAX_TEXT}, "maxItems": MAX_OBJECTS},
        "text_seen": {"type": "string", "maxLength": MAX_TEXT},
        "notable": {"type": "boolean"},
    },
    "required": ["scene", "activity", "people_count", "objects", "text_seen", "notable"],
}


def _clip_text(value):
    return value[:MAX_TEXT] if isinstance(value, str) else value


Short = Annotated[str, BeforeValidator(_clip_text)]


class Caption(BaseModel):
    """A caption within the limits: over-long text and lists are cut, the people count is clamped;
    only unparseable JSON, a missing field or a wrong type is invalid."""

    model_config = ConfigDict(extra="ignore")

    scene: Short
    activity: Short
    people_count: int
    objects: list[Short]
    text_seen: Short
    notable: bool

    @field_validator("people_count")
    @classmethod
    def _clamp_people(cls, value: int) -> int:
        return min(max(value, 0), MAX_PEOPLE)

    @field_validator("objects")
    @classmethod
    def _cap_objects(cls, value: list[str]) -> list[str]:
        return value[:MAX_OBJECTS]


class CaptionInvalid(Exception):
    """The model answered, but not with a valid caption (retried like any job error)."""


def parse_caption(text: str) -> Caption:
    try:
        return Caption.model_validate(json.loads(text))
    except (ValueError, ValidationError) as exc:  # JSONDecodeError is a ValueError
        raise CaptionInvalid(f"{exc}; got {text[:200]!r}") from exc


def caption_text(c: Caption) -> str:
    """The searchable text of a visual moment."""
    parts = [c.scene.strip(), c.activity.strip()]
    if c.people_count:
        parts.append("1 person" if c.people_count == 1 else f"{c.people_count} people")
    objects = [o.strip() for o in c.objects if o.strip()]
    if objects:
        parts.append("Objects: " + ", ".join(objects))
    if c.text_seen.strip():
        parts.append(f'Text: "{c.text_seen.strip()}"')
    text = ". ".join(p.rstrip(".") for p in parts if p)
    return f"{text}." if text else "Unremarkable scene."


def prepare_image(path: Path) -> bytes:
    """The keyframe as JPEG (quality 85), downscaled so its longer side is at most 896 px."""
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


async def _caption(ctx: PipelineContext, keyframe_id: str, attempt: int) -> bool:
    pool = ctx.pool
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, item_id, device_id, ts, path, caption FROM keyframe WHERE id = %s", (keyframe_id,)
        )
        kf = await cur.fetchone()
    if kf is None or kf["caption"] is not None:
        return False
    image = await anyio.to_thread.run_sync(prepare_image, ctx.settings.media_root / kf["path"])
    try:
        result = await ctx.router.chat(
            "caption",
            [{"role": "user", "content": CAPTION_PROMPT}],
            images=[image],
            json_schema=CAPTION_SCHEMA,
            temperature=0.0 if attempt == 0 else RETRY_TEMPERATURE,
            max_tokens=CAPTION_MAX_TOKENS,
        )
    except ProviderUnavailable as exc:
        # The model server is down: give the job back without burning a retry; the
        # dispatcher defers this keyframe again on its next tick.
        log.warning("keyframe %s: model server unavailable, will retry later: %s", keyframe_id, exc)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE keyframe SET caption_job_id = NULL, caption_error = %s WHERE id = %s",
                (error_text(exc), keyframe_id),
            )
        return False
    caption = parse_caption(result.text)
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "UPDATE keyframe SET caption = %s, caption_model = %s, captioned_at = now(), caption_error = NULL"
            " WHERE id = %s AND caption IS NULL RETURNING id",
            (Jsonb(caption.model_dump()), result.model_version, keyframe_id),
        )
        if await cur.fetchone() is None:  # deleted (or captioned) while the model was answering
            return False
        await conn.execute(
            "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, lang, item_id, keyframe_id, model_version)"
            " VALUES (%s, %s, %s, %s, 'visual', %s, 'en', %s, %s, %s)",
            (devices.new_id(), kf["device_id"], kf["ts"], kf["ts"], caption_text(caption), kf["item_id"],
             keyframe_id, result.model_version),
        )
    await dispatch.defer_embed(pool)
    return True


async def caption_keyframe(ctx: PipelineContext, keyframe_id: str, attempt: int = 0) -> bool:
    """Caption one keyframe and add its visual moment; False if there was nothing to do (or no model server).

    ``attempt`` is the job's previous attempts: retries sample at a higher temperature.
    """
    try:
        return await _caption(ctx, keyframe_id, attempt)
    except Exception as exc:
        if not await keyframe_exists(ctx.pool, keyframe_id):  # deleted while we worked (e.g. its JPEG went first)
            log.info("keyframe %s was deleted during captioning: %s", keyframe_id, error_text(exc))
            return False
        await record_keyframe_error(ctx.pool, keyframe_id, exc)
        raise


@job_app.task(name=jobs.CAPTION_KEYFRAME, queue="caption", retry=jobs.RETRY, pass_context=True)
async def caption_keyframe_job(context: JobContext, keyframe_id: str) -> None:
    await caption_keyframe(get_context(), keyframe_id, attempt=context.job.attempts)
```

In `brain/src/loopi_brain/pipeline/jobs.py` set:

```python
TASK_MODULES = [
    "loopi_brain.pipeline.dispatch",
    "loopi_brain.pipeline.video",
    "loopi_brain.pipeline.moments",
    "loopi_brain.pipeline.caption",
]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_caption.py -v`
Expected: PASS (14 tests). The outage test runs a real Procrastinate worker five times.

Run: `cd brain && uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain/pipeline/caption.py brain/src/loopi_brain/pipeline/jobs.py \
  brain/tests/helpers.py brain/tests/test_caption.py
git commit -F - <<'EOF'
feat(brain): caption keyframes as validated JSON; model server outages burn no retries

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 7: Speech recognition with Silero VAD and faster-whisper

**Files:**
- Create: `brain/src/loopi_brain/pipeline/asr.py`
- Modify: `brain/src/loopi_brain/pipeline/jobs.py` (`TASK_MODULES`)
- Modify: `brain/tests/helpers.py` (append)
- Test: `brain/tests/test_asr.py`

**Interfaces:**
- Consumes: Task 2 `Router.model_version("asr")`; Task 3 `dispatch.defer_embed`, `jobs.*`; Task 4 `state.error_text`, `state.finish_item`, `state.item_exists`, `state.record_item_error`; `Settings.asr_language_list`, `whisper_cpu_threads`, `model_dir`.
- Produces: `asr.SAMPLE_RATE = 16000`, `MAX_REGION_S = 20.0`, `WHISPER_SIZE = "large-v3-turbo"`, `WHISPER_COMPUTE = "int8"`, `HALLUCINATIONS`, `HALLUCINATION_COVERAGE = 0.8`, `Region(start, end)`, `SegmentResult(start, end, text, avg_logprob, no_speech_prob, compression_ratio)`, `RegionResult(lang, lang_prob, segments)`, `Transcriber` protocol (`transcribe(chunk: np.ndarray) -> RegionResult`), `is_hallucination(text)`, `keep_segment(seg)`, `merge_regions(raw, sample_rate=16000) -> list[Region]`, `load_audio(path) -> np.ndarray`, `speech_regions(audio) -> list[Region]`, `pick_language(all_probs, allowed) -> (lang, prob)`, `WhisperTranscriber(settings)` (raises `ValueError` at construction — worker start — when `LOOPI_ASR_LANGUAGES` is empty or names a code Whisper does not know), `transcribe_file(path, transcriber)`, `async transcribe_audio(ctx, item_id) -> int`, task `transcribe_audio_job(item_id)`; helpers `make_opus(path, source, seconds)`, `FakeTranscriber(*results)`.

Rulings in this task: faster-whisper is imported lazily (inside the functions) so the API process never loads CTranslate2/onnxruntime. Hallucination phrases match after normalisation (NFC, lower case, every non-word character and all spaces removed), and a segment is dropped only when known phrases **cover at least 80 %** of it — real speech that merely contains one ("Thanks for watching the kids", "Anh đã đăng ký kênh K+ chưa?") is kept. Merged speech regions are capped at 20 s, split at the longest pause inside (the one nearest the middle among equals) or, for one unbroken stretch, into equal parts, because Whisper detects one language per region from its start: a Vietnamese–Korean conversation gets a language per ≤ 20 s region. The whole decode + VAD + transcription runs in one worker thread. A speech moment's `transcript_segment_id` points to the first segment of its region. Re-running replaces the item's transcript (moments go with their segments through `ON DELETE CASCADE`); the storing transaction first locks the item row, so an item deleted while Whisper ran makes the job a no-op, as does an error raised after the item disappeared. P2a never deletes the raw audio.

- [ ] **Step 1: Write the failing tests**

Append to `brain/tests/helpers.py`:

```python


def make_opus(path, source: str = "anullsrc=r=16000:cl=mono", seconds: float = 5):
    """Encode an ffmpeg lavfi audio ``source`` (default: silence) as Ogg/Opus, like the device does."""
    import shutil
    import subprocess

    import pytest

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required for audio tests")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source, "-t", str(seconds),
         "-c:a", "libopus", "-f", "ogg", str(path)],
        check=True,
    )
    return path


class FakeTranscriber:
    """Stands in for Whisper: returns the scripted RegionResults in order, cycling."""

    def __init__(self, *results):
        self.results = list(results)
        self.chunks: list[int] = []

    def transcribe(self, chunk):
        self.chunks.append(len(chunk))
        return self.results[(len(self.chunks) - 1) % len(self.results)]
```

Create `brain/tests/test_asr.py`:

```python
import datetime as dt
import os

import numpy as np
import psycopg
import pytest

from helpers import (
    T0,
    FakeTranscriber,
    fetch_all,
    fetch_one,
    insert_item,
    jobs_by_lock,
    make_ctx,
    make_opus,
    paired_device,
)
from loopi_brain.config import Settings
from loopi_brain.pipeline import asr
from loopi_brain.pipeline.asr import Region, RegionResult, SegmentResult

SR = asr.SAMPLE_RATE


def seg(text, start=0.0, end=2.0, logprob=-0.3, no_speech=0.05, ratio=1.2) -> SegmentResult:
    return SegmentResult(start, end, text, logprob, no_speech, ratio)


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


async def _audio_item(pool, settings, device_id, seconds=5, started_at=T0) -> str:
    item_id = await insert_item(pool, device_id, kind="audio", started_at=started_at, seconds=seconds)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    make_opus(settings.media_root / row["raw_path"], seconds=seconds)
    return item_id


def test_merge_regions_joins_short_gaps_and_drops_blips():
    raw = [
        {"start": 0, "end": 1 * SR},
        {"start": int(1.5 * SR), "end": 3 * SR},  # gap 0.5 s -> merged
        {"start": 5 * SR, "end": int(5.3 * SR)},  # 0.3 s blip alone -> dropped
        {"start": 8 * SR, "end": 10 * SR},
    ]
    assert asr.merge_regions(raw) == [Region(0.0, 3.0), Region(8.0, 10.0)]
    assert asr.merge_regions([]) == []


def test_a_minute_of_turn_taking_is_split_into_regions_of_at_most_20_s():
    # 12 turns of 4.4 s with 0.6 s pauses merge into one 60 s stretch; the 0.9 s pause after
    # turn 4 is the longest, so the first cut goes there.
    starts = [0.0, 5.0, 10.0, 15.0, 20.3, 25.3, 30.3, 35.3, 40.3, 45.3, 50.3, 55.3]
    raw = [{"start": int(s * SR), "end": int((s + 4.4) * SR)} for s in starts]
    regions = asr.merge_regions(raw)
    assert [(r.start, r.end) for r in regions] == [
        pytest.approx((0.0, 19.4), abs=0.01), pytest.approx((20.3, 39.7), abs=0.01),
        pytest.approx((40.3, 59.7), abs=0.01)]
    one_long_turn = asr.merge_regions([{"start": 0, "end": 60 * SR}])
    assert one_long_turn == [Region(0.0, 20.0), Region(20.0, 40.0), Region(40.0, 60.0)]


@pytest.mark.parametrize(
    "text",
    [
        "Hẹn gặp lại các bạn trong những video tiếp theo!",
        "Cảm ơn các bạn đã theo dõi.",
        "Hãy subscribe cho kênh Ghiền Mì Gõ để không bỏ lỡ những video hấp dẫn",
        "Cảm ơn các bạn đã theo dõi và hẹn gặp lại!",
        "시청해주셔서 감사합니다.",
        "Thanks for watching!",
        "Subtitles by the Amara.org community",
    ],
)
def test_known_hallucinations_are_dropped(text):
    assert asr.is_hallucination(text)
    assert not asr.keep_segment(seg(text))


@pytest.mark.parametrize(
    "text",
    ["Anh đã đăng ký kênh K+ chưa?", "Thanks for watching the kids yesterday!", "Hẹn gặp lại anh vào thứ Hai nhé"],
)
def test_real_speech_that_contains_a_phrase_is_kept(text):
    assert not asr.is_hallucination(text)
    assert asr.keep_segment(seg(text))


@pytest.mark.parametrize(
    ("segment", "kept"),
    [
        (seg("Mai mình đi chợ nhé"), True),
        (seg("내일 회의는 열 시예요"), True),
        (seg("   "), False),
        (seg("uh", logprob=-1.5, no_speech=0.8), False),
        (seg("probably real", logprob=-1.5, no_speech=0.4), True),
        (seg("la la la la la la la la", ratio=2.6), False),
    ],
)
def test_segment_filters(segment, kept):
    assert asr.keep_segment(segment) is kept


def test_pick_language_is_restricted_to_the_configured_ones():
    probs = [("zh", 0.6), ("ko", 0.25), ("vi", 0.1), ("en", 0.05)]
    assert asr.pick_language(probs, ["vi", "ko", "en"]) == ("ko", 0.25)
    assert asr.pick_language([("ja", 1.0)], ["vi", "ko"]) == ("vi", 0.0)


def test_silence_has_no_speech_regions(tmp_path):
    audio = asr.load_audio(make_opus(tmp_path / "silence.opus", seconds=5))
    assert audio.dtype == np.float32 and len(audio) == 5 * SR
    assert asr.speech_regions(audio) == []


async def test_speech_regions_become_segments_and_moments(pool, pki_settings, dev, jobs_open, monkeypatch):
    item_id = await _audio_item(pool, pki_settings, dev["device_id"])
    monkeypatch.setattr(asr, "speech_regions", lambda _audio: [Region(0.5, 2.5), Region(3.0, 4.5), Region(4.6, 4.9)])
    fake = FakeTranscriber(
        RegionResult("vi", 0.9, [seg("Mai mình đi chợ", 0.0, 1.0), seg("nhớ mua trứng nhé", 1.0, 2.0)]),
        RegionResult("ko", 0.8, [seg("네 알겠어요", 0.2, 1.4)]),
        RegionResult("en", 0.4, [seg("Thanks for watching!", 0.0, 0.3)]),
    )
    ctx = make_ctx(pool, pki_settings, transcriber=fake)
    assert await asr.transcribe_audio(ctx, item_id) == 2
    assert fake.chunks == [2 * SR, int(1.5 * SR), int(0.3 * SR)]
    segments = await fetch_all(pool, "SELECT * FROM transcript_segment ORDER BY start_ts")
    assert [(s["lang"], s["text"]) for s in segments] == [
        ("vi", "Mai mình đi chợ"), ("vi", "nhớ mua trứng nhé"), ("ko", "네 알겠어요")]
    assert segments[0]["start_ts"] == T0 + dt.timedelta(seconds=0.5)
    assert segments[2]["end_ts"] == T0 + dt.timedelta(seconds=4.4)
    assert {s["model_version"] for s in segments} == {"local:faster-whisper/large-v3-turbo-int8"}
    found = await fetch_all(pool, "SELECT * FROM moment ORDER BY ts_start")
    assert [(m["kind"], m["lang"], m["text"]) for m in found] == [
        ("speech", "vi", "Mai mình đi chợ nhớ mua trứng nhé"), ("speech", "ko", "네 알겠어요")]
    assert found[0]["transcript_segment_id"] == segments[0]["id"]
    assert (found[0]["ts_start"], found[0]["ts_end"]) == (T0 + dt.timedelta(seconds=0.5), T0 + dt.timedelta(seconds=2.5))
    item = await fetch_one(pool, "SELECT analysis_state, analysis_error FROM item WHERE id = %s", (item_id,))
    assert item == {"analysis_state": "done", "analysis_error": None}
    assert "embed" in await jobs_by_lock(pool)
    async with pool.connection() as conn:  # re-running replaces the transcript instead of duplicating it
        await conn.execute("UPDATE item SET analysis_state = 'pending' WHERE id = %s", (item_id,))
    assert await asr.transcribe_audio(ctx, item_id) == 2
    assert len(await fetch_all(pool, "SELECT id FROM transcript_segment")) == 3
    assert len(await fetch_all(pool, "SELECT id FROM moment")) == 2


async def test_hallucinated_text_on_noise_leaves_no_transcript(pool, pki_settings, dev, jobs_open, monkeypatch):
    item_id = await _audio_item(pool, pki_settings, dev["device_id"])
    monkeypatch.setattr(asr, "speech_regions", lambda _audio: [Region(0.0, 4.0)])
    tv_noise = RegionResult("vi", 0.5, [
        seg("Hẹn gặp lại các bạn trong những video tiếp theo", 0.0, 2.0),
        seg("Thanks for watching", 2.0, 3.0),
        seg("...", 3.0, 3.5, no_speech=0.9, logprob=-1.4),
    ])
    assert await asr.transcribe_audio(make_ctx(pool, pki_settings, transcriber=FakeTranscriber(tv_noise)), item_id) == 0
    assert await fetch_all(pool, "SELECT id FROM transcript_segment") == []
    assert await fetch_all(pool, "SELECT id FROM moment") == []
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "done"
    assert "embed" not in await jobs_by_lock(pool)


async def test_real_silence_is_done_without_calling_whisper(pool, pki_settings, dev, jobs_open):
    item_id = await _audio_item(pool, pki_settings, dev["device_id"])
    fake = FakeTranscriber()
    assert await asr.transcribe_audio(make_ctx(pool, pki_settings, transcriber=fake), item_id) == 0
    assert fake.chunks == []
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "done"


async def test_item_deleted_during_transcription_stores_nothing(pool, pki_settings, dev, jobs_open, monkeypatch):
    item_id = await _audio_item(pool, pki_settings, dev["device_id"])
    monkeypatch.setattr(asr, "speech_regions", lambda _audio: [Region(0.0, 2.0)])

    class DeletingTranscriber(FakeTranscriber):
        def transcribe(self, chunk):  # runs in the worker thread, like Whisper
            with psycopg.connect(pki_settings.database_url, autocommit=True) as conn:
                conn.execute("DELETE FROM item WHERE id = %s", (item_id,))
            return super().transcribe(chunk)

    speech = RegionResult("vi", 0.9, [seg("Mai mình đi chợ")])
    ctx = make_ctx(pool, pki_settings, transcriber=DeletingTranscriber(speech))
    assert await asr.transcribe_audio(ctx, item_id) == 0
    assert await fetch_all(pool, "SELECT id FROM transcript_segment") == []
    assert await fetch_all(pool, "SELECT id FROM moment") == []
    assert "embed" not in await jobs_by_lock(pool)


async def test_missing_file_is_skipped_and_corrupt_file_records_the_error(pool, pki_settings, dev):
    ctx = make_ctx(pool, pki_settings, transcriber=FakeTranscriber())
    gone = await insert_item(pool, dev["device_id"], kind="audio")
    assert await asr.transcribe_audio(ctx, gone) == 0
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (gone,)))["analysis_state"] == "skipped"
    corrupt = await insert_item(pool, dev["device_id"], kind="audio", started_at=T0 + dt.timedelta(minutes=1))
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (corrupt,))
    (pki_settings.media_root / row["raw_path"]).parent.mkdir(parents=True, exist_ok=True)
    (pki_settings.media_root / row["raw_path"]).write_bytes(b"OggS-fake")
    with pytest.raises(ValueError):  # PyAV's InvalidDataError
        await asr.transcribe_audio(ctx, corrupt)
    item = await fetch_one(pool, "SELECT analysis_state, analysis_error FROM item WHERE id = %s", (corrupt,))
    assert item["analysis_state"] == "pending" and item["analysis_error"]
    video = await insert_item(pool, dev["device_id"], kind="video")
    assert await asr.transcribe_audio(ctx, video) == 0


def test_whisper_transcriber_loads_once_and_restricts_the_language(monkeypatch, tmp_path):
    import faster_whisper

    created = []

    class StubSegment:
        start, end, text, avg_logprob, no_speech_prob, compression_ratio = 0.0, 1.5, " xin chào ", -0.2, 0.01, 1.1

    class StubModel:
        def __init__(self, size, **kwargs):
            created.append((size, kwargs))
            self.calls = []

        def detect_language(self, audio):
            return "zh", 0.7, [("zh", 0.7), ("vi", 0.2), ("en", 0.1)]

        def transcribe(self, audio, **kwargs):
            self.calls.append(kwargs)
            return iter([StubSegment()]), object()

    monkeypatch.setattr(faster_whisper, "WhisperModel", StubModel)
    settings = Settings(database_url="postgresql://x", model_dir=tmp_path, whisper_cpu_threads=3)
    whisper = asr.WhisperTranscriber(settings)
    chunk = np.zeros(SR, dtype=np.float32)
    first = whisper.transcribe(chunk)
    whisper.transcribe(chunk)
    assert created == [("large-v3-turbo", {"device": "cpu", "compute_type": "int8", "cpu_threads": 3,
                                           "download_root": str(tmp_path / "whisper")})]
    assert first == RegionResult("vi", 0.2, [SegmentResult(0.0, 1.5, "xin chào", -0.2, 0.01, 1.1)])
    assert whisper._model.calls[0] == {"language": "vi", "beam_size": 5, "condition_on_previous_text": False,
                                       "vad_filter": False}


@pytest.mark.parametrize("languages", ["vi,vn", " , "])
def test_unknown_or_missing_languages_stop_the_worker_at_start(languages):
    with pytest.raises(ValueError, match="LOOPI_ASR_LANGUAGES"):
        asr.WhisperTranscriber(Settings(database_url="postgresql://x", asr_languages=languages))


@pytest.mark.skipif(os.environ.get("LOOPI_TEST_WHISPER") != "1", reason="set LOOPI_TEST_WHISPER=1 (downloads 1.6 GB once)")
def test_real_whisper_model_transcribes_a_chunk(tmp_path):
    model_dir = os.environ.get("LOOPI_MODEL_DIR", str(tmp_path / "models"))
    whisper = asr.WhisperTranscriber(Settings(database_url="postgresql://x", model_dir=model_dir))
    tone = asr.load_audio(make_opus(tmp_path / "tone.opus", "sine=frequency=440:sample_rate=16000", 3))
    result = whisper.transcribe(tone)
    assert result.lang in {"vi", "ko", "en"} and 0.0 <= result.lang_prob <= 1.0
    assert all(isinstance(s, SegmentResult) for s in result.segments)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_asr.py -v`
Expected: FAIL — `ImportError: cannot import name 'asr' from 'loopi_brain.pipeline'`.

- [ ] **Step 3: Write the module and register it**

Create `brain/src/loopi_brain/pipeline/asr.py`:

```python
"""transcribe_audio: Silero VAD + faster-whisper per speech region (spec §9.2 audio steps 1-2).

Step 3 (deleting raw audio after transcription) is retention, which lands in P2b: P2a deletes no media.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import anyio
import numpy as np

from .. import devices
from ..config import Settings
from . import dispatch, jobs
from .context import PipelineContext, get_context
from .jobs import job_app
from .state import error_text, finish_item, item_exists, record_item_error

log = logging.getLogger(__name__)
SAMPLE_RATE = 16_000
MERGE_GAP_S = 1.0
MIN_REGION_S = 0.5
# Whisper detects one language per region from its start, so regions stay short enough for a
# Vietnamese–Korean conversation to change language between them.
MAX_REGION_S = 20.0
WHISPER_SIZE = "large-v3-turbo"
WHISPER_COMPUTE = "int8"
# Whisper invents these on silence, music or TV noise. A segment is dropped only when such phrases
# make up most of it (HALLUCINATION_COVERAGE of its normalised text), never because real speech
# merely contains one ("thanks for watching the kids").
HALLUCINATIONS = (
    "hẹn gặp lại các bạn trong những video tiếp theo",
    "hãy subscribe cho kênh ghiền mì gõ để không bỏ lỡ những video hấp dẫn",
    "cảm ơn các bạn đã theo dõi",
    "hãy subscribe cho kênh",
    "đăng ký kênh",
    "hẹn gặp lại",
    "시청해 주셔서 감사합니다",
    "구독과 좋아요",
    "thanks for watching",
    "thank you for watching",
    "subtitles by the amara.org community",
)
HALLUCINATION_COVERAGE = 0.8


@dataclass(frozen=True)
class Region:
    start: float  # seconds from the start of the file
    end: float


@dataclass(frozen=True)
class SegmentResult:
    start: float  # seconds from the start of the region
    end: float
    text: str
    avg_logprob: float
    no_speech_prob: float
    compression_ratio: float


@dataclass(frozen=True)
class RegionResult:
    lang: str
    lang_prob: float
    segments: list[SegmentResult]


class Transcriber(Protocol):
    def transcribe(self, chunk: np.ndarray) -> RegionResult: ...


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFC", text).lower()
    return re.sub(r"[\W_]+", "", text)


_HALLUCINATIONS = tuple(sorted({_normalise(p) for p in HALLUCINATIONS}, key=len, reverse=True))


def is_hallucination(text: str) -> bool:
    """True when known hallucinated phrases cover at least 80 % of the segment (lower case,
    NFC, punctuation and spaces removed)."""
    norm = _normalise(text)
    if not norm:
        return False
    covered = [False] * len(norm)
    for phrase in _HALLUCINATIONS:
        start = norm.find(phrase)
        while start != -1:
            covered[start: start + len(phrase)] = [True] * len(phrase)
            start = norm.find(phrase, start + 1)
    return sum(covered) >= HALLUCINATION_COVERAGE * len(norm)


def keep_segment(seg: SegmentResult) -> bool:
    """Drop empty text, likely non-speech, repetition loops and known hallucinations."""
    if not seg.text.strip():
        return False
    if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
        return False
    if seg.compression_ratio > 2.4:
        return False
    return not is_hallucination(seg.text)


def _cap(chunks: list[tuple[float, float]]) -> list[Region]:
    """One merged group of speech chunks -> regions of at most MAX_REGION_S: split at the longest
    silence between chunks (of equally long ones, the one nearest the middle) while possible,
    else (one long chunk) into equal parts."""
    start, end = chunks[0][0], max(c[1] for c in chunks)
    if end - start <= MAX_REGION_S:
        return [Region(start, end)]
    if len(chunks) > 1:
        middle = (start + end) / 2

        def preference(k: int) -> tuple[float, float]:
            return round(chunks[k][0] - chunks[k - 1][1], 1), -abs(chunks[k][0] - middle)

        cut = max(range(1, len(chunks)), key=preference)
        return _cap(chunks[:cut]) + _cap(chunks[cut:])
    parts = int(-(-(end - start) // MAX_REGION_S))  # ceil
    step = (end - start) / parts
    return [Region(start + k * step, start + (k + 1) * step) for k in range(parts)]


def merge_regions(raw: list[dict], sample_rate: int = SAMPLE_RATE) -> list[Region]:
    """Silero sample ranges -> regions in seconds: gaps under 1 s merged, regions longer than 20 s
    split again, regions under 0.5 s dropped."""
    groups: list[list[tuple[float, float]]] = []
    for chunk in raw:
        span = (chunk["start"] / sample_rate, chunk["end"] / sample_rate)
        if groups and span[0] - max(c[1] for c in groups[-1]) < MERGE_GAP_S:
            groups[-1].append(span)
        else:
            groups.append([span])
    regions = [region for group in groups for region in _cap(group)]
    return [r for r in regions if r.end - r.start >= MIN_REGION_S]


def load_audio(path: Path) -> np.ndarray:
    """Decode any audio file (Ogg/Opus here) to 16 kHz mono float32 (PyAV, no ffmpeg binary needed)."""
    from faster_whisper import decode_audio

    return decode_audio(str(path), sampling_rate=SAMPLE_RATE)


def speech_regions(audio: np.ndarray) -> list[Region]:
    """Where someone speaks, by the Silero VAD model bundled with faster-whisper (no download)."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    raw = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=500, speech_pad_ms=200), SAMPLE_RATE)
    return merge_regions(raw)


def pick_language(all_probs: list[tuple[str, float]], allowed: list[str]) -> tuple[str, float]:
    """The most likely language among ``allowed`` (Whisper's detector knows ~100)."""
    candidates = [(lang, prob) for lang, prob in all_probs if lang in allowed]
    return max(candidates, key=lambda c: c[1]) if candidates else (allowed[0], 0.0)


class WhisperTranscriber:
    """faster-whisper large-v3-turbo, int8 on CPU; the model loads on first use (≈ 1.6 GB download once)."""

    def __init__(self, settings: Settings):
        from faster_whisper.tokenizer import _LANGUAGE_CODES  # Whisper's language codes

        unknown = [code for code in settings.asr_language_list if code not in _LANGUAGE_CODES]
        if not settings.asr_language_list or unknown:  # fail at worker start, not on every audio job
            raise ValueError(f"LOOPI_ASR_LANGUAGES must list Whisper language codes; unknown: {unknown or 'none set'}")
        self._languages = settings.asr_language_list
        self._threads = settings.whisper_cpu_threads
        self._root = settings.model_dir / "whisper"
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    WHISPER_SIZE, device="cpu", compute_type=WHISPER_COMPUTE,
                    cpu_threads=self._threads, download_root=str(self._root),
                )
            return self._model

    def transcribe(self, chunk: np.ndarray) -> RegionResult:
        model = self._load()
        _lang, _prob, all_probs = model.detect_language(chunk)
        lang, prob = pick_language(all_probs, self._languages)
        segments, _info = model.transcribe(
            chunk, language=lang, beam_size=5, condition_on_previous_text=False, vad_filter=False
        )
        return RegionResult(lang, prob, [
            SegmentResult(s.start, s.end, s.text.strip(), s.avg_logprob, s.no_speech_prob, s.compression_ratio)
            for s in segments
        ])


def transcribe_file(path: Path, transcriber: Transcriber) -> list[tuple[Region, RegionResult]]:
    """Blocking: decode, find speech, transcribe each region. Segment times stay region-relative."""
    audio = load_audio(path)
    results = []
    for region in speech_regions(audio):
        chunk = audio[int(region.start * SAMPLE_RATE): int(region.end * SAMPLE_RATE)]
        results.append((region, transcriber.transcribe(chunk)))
    return results


async def _transcribe(ctx: PipelineContext, item_id: str) -> int:
    pool = ctx.pool
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT device_id, kind, started_at, raw_path, analysis_state FROM item WHERE id = %s", (item_id,)
        )
        item = await cur.fetchone()
    if item is None or item["kind"] != "audio" or item["analysis_state"] != "pending":
        return 0
    path = ctx.settings.media_root / item["raw_path"]
    if not path.is_file():
        await finish_item(pool, item_id, "skipped")
        return 0
    results = await anyio.to_thread.run_sync(transcribe_file, path, ctx.transcriber)
    version = ctx.router.model_version("asr")
    start: dt.datetime = item["started_at"]
    moments = 0
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "SELECT 1 FROM item WHERE id = %s AND analysis_state = 'pending' FOR UPDATE", (item_id,)
        )
        if await cur.fetchone() is None:  # deleted while Whisper was running: nothing to store
            log.info("item %s was deleted during its transcription; nothing stored", item_id)
            return 0
        # Re-running replaces the item's transcript (moments go with their segments: ON DELETE CASCADE).
        await conn.execute("DELETE FROM transcript_segment WHERE item_id = %s", (item_id,))
        for region, result in results:
            kept = [s for s in result.segments if keep_segment(s)]
            if not kept:
                continue
            segment_ids = []
            for seg in kept:
                segment_id = devices.new_id()
                segment_ids.append(segment_id)
                await conn.execute(
                    "INSERT INTO transcript_segment (id, item_id, device_id, start_ts, end_ts, lang, text,"
                    " avg_logprob, no_speech_prob, model_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (segment_id, item_id, item["device_id"],
                     start + dt.timedelta(seconds=region.start + seg.start),
                     start + dt.timedelta(seconds=region.start + seg.end),
                     result.lang, seg.text.strip(), seg.avg_logprob, seg.no_speech_prob, version),
                )
            await conn.execute(
                "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, lang, item_id,"
                " transcript_segment_id, model_version) VALUES (%s, %s, %s, %s, 'speech', %s, %s, %s, %s, %s)",
                (devices.new_id(), item["device_id"],
                 start + dt.timedelta(seconds=region.start + kept[0].start),
                 start + dt.timedelta(seconds=region.start + kept[-1].end),
                 " ".join(s.text.strip() for s in kept), result.lang, item_id, segment_ids[0], version),
            )
            moments += 1
        await conn.execute(
            "UPDATE item SET analysis_state = 'done', analyzed_at = now(), analysis_error = NULL WHERE id = %s",
            (item_id,),
        )
    if moments:
        await dispatch.defer_embed(pool)
    log.info("item %s: %d speech regions, %d kept", item_id, len(results), moments)
    return moments


async def transcribe_audio(ctx: PipelineContext, item_id: str) -> int:
    """Transcribe a pending audio item; return the number of speech moments written."""
    try:
        return await _transcribe(ctx, item_id)
    except Exception as exc:
        if not await item_exists(ctx.pool, item_id):  # deleted while we worked (e.g. its file went first)
            log.info("item %s was deleted during its transcription: %s", item_id, error_text(exc))
            return 0
        await record_item_error(ctx.pool, item_id, exc)
        raise


@job_app.task(name=jobs.TRANSCRIBE_AUDIO, queue="audio", retry=jobs.RETRY)
async def transcribe_audio_job(item_id: str) -> None:
    await transcribe_audio(get_context(), item_id)
```

In `brain/src/loopi_brain/pipeline/jobs.py` set:

```python
TASK_MODULES = [
    "loopi_brain.pipeline.dispatch",
    "loopi_brain.pipeline.video",
    "loopi_brain.pipeline.moments",
    "loopi_brain.pipeline.caption",
    "loopi_brain.pipeline.asr",
]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_asr.py -v`
Expected: PASS (28 tests, 1 skipped: the real-model test). The silence tests run the real Silero VAD bundled with faster-whisper (no download).

Run: `cd brain && uv run pytest -q`
Expected: the whole suite passes.

Optional, downloads ≈ 1.6 GB once: `cd brain && LOOPI_TEST_WHISPER=1 LOOPI_MODEL_DIR=$HOME/.cache/loopi-models uv run pytest tests/test_asr.py -k real_whisper -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain/pipeline/asr.py brain/src/loopi_brain/pipeline/jobs.py \
  brain/tests/helpers.py brain/tests/test_asr.py
git commit -F - <<'EOF'
feat(brain): transcribe speech regions with Silero VAD and faster-whisper, drop hallucinations

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Pre-flight rulings for this task (controller, 2026-10-15; they amend the steps above):**
- **R-F6:** `asr.py` uses `state.lock_pending_item(conn, item_id)` and `state.mark_item_done(conn, item_id)` (added in Task 4) instead of the inline SELECT … FOR UPDATE and UPDATE.

---

### Task 8: The worker process and its CLI commands

**Files:**
- Create: `brain/src/loopi_brain/pipeline/worker.py`, `brain/src/loopi_brain/health.py`
- Modify: `brain/src/loopi_brain/cli.py`
- Test: `brain/tests/test_worker.py`

**Interfaces:**
- Consumes: everything above (`migrate`, `make_pool`, `load_providers`, `build_router`, `OllamaProvider.pull`, `WhisperTranscriber`, `set_context`, `open_jobs`).
- Produces: `health.HEARTBEAT_MAX_AGE_S = 60`, `health.worker_alive(database_url, max_age_s=60) -> bool` (module imports psycopg only); `worker.run_worker(settings, **options)` (async; recovers stalled jobs, then options go to `App.run_worker_async`, default `concurrency=settings.worker_concurrency`), `worker.download_whisper(settings)`, `async pull_models(settings, client=None, download=download_whisper, report=print) -> list[str]`; CLI `loopi-brain worker`, `loopi-brain worker-health` (exit 0/1), `loopi-brain models pull`.

Rulings in this task: the worker also runs `migrate()` (the advisory lock makes it safe next to the API) and stops at startup on a broken `providers.yaml`. `worker-health` passes when any Procrastinate worker heartbeat is younger than 60 s; Docker runs it every 30 s inside the worker's cgroup, so it lives in `health.py` (psycopg only) and `cli.py` imports each subcommand's modules inside that subcommand (measured: 0.1 s CPU per call instead of 1.1 s with numpy and Procrastinate). At start the worker first runs `recover_stalled` once, so jobs of a worker that died more than 30 s ago run again at once. `models pull` pulls every Ollama model named by any route or fallback, then downloads Whisper into `<model_dir>/whisper` (the same cache `WhisperModel(download_root=...)` reads).

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_worker.py`:

```python
import datetime as dt
import os
import subprocess
import sys

import pytest
from procrastinate.periodic import PeriodicRegistry

from helpers import (
    DEFAULT_OLLAMA_MODELS,
    T0,
    TWO_SCENES,
    FakeOllama,
    FakeTranscriber,
    fetch_all,
    fetch_one,
    insert_item,
    make_ctx,
    make_h264,
    make_opus,
    paired_device,
    run_jobs,
)
from loopi_brain import cli, health
from loopi_brain.config import Settings
from loopi_brain.pipeline import asr, context, dispatch, jobs, worker
from loopi_brain.pipeline.asr import Region, RegionResult, SegmentResult


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


async def _with_file(pool, settings, item_id, write):
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    write(settings.media_root / row["raw_path"])


def test_every_pipeline_task_is_registered():
    jobs.job_app.perform_import_paths()
    assert set(jobs.PIPELINE_TASKS) | {jobs.DISPATCH_PENDING, jobs.RETRY_STALLED, jobs.REMOVE_OLD_JOBS} <= set(
        jobs.job_app.tasks
    )


async def test_a_real_worker_runs_every_stage(pool, pki_settings, dev, jobs_open, monkeypatch):
    device_id = dev["device_id"]
    video = await insert_item(pool, device_id, kind="video", seconds=80)
    await _with_file(pool, pki_settings, video, lambda p: make_h264(p, TWO_SCENES, 80))
    audio = await insert_item(pool, device_id, kind="audio", started_at=T0 + dt.timedelta(minutes=2), seconds=5)
    await _with_file(pool, pki_settings, audio, lambda p: make_opus(p, seconds=5))
    marker = await insert_item(pool, device_id, kind="marker", started_at=T0 + dt.timedelta(minutes=3), seconds=0,
                               meta={"note": "chìa khoá"})
    monkeypatch.setattr(asr, "speech_regions", lambda _audio: [Region(1.0, 3.0)])
    speech = RegionResult("vi", 0.9, [SegmentResult(0.0, 2.0, "Chìa khoá ở trên bàn", -0.2, 0.02, 1.1)])
    ctx = make_ctx(pool, pki_settings, ollama=FakeOllama(), transcriber=FakeTranscriber(speech))

    assert await dispatch.dispatch_pending(pool, ctx.router) == (3, 0)
    await run_jobs(ctx)

    states = await fetch_all(pool, "SELECT id, analysis_state FROM item")
    assert {r["id"]: r["analysis_state"] for r in states} == {video: "done", audio: "done", marker: "done"}
    assert [r["caption"] is not None for r in await fetch_all(pool, "SELECT caption FROM keyframe")] == [True, True]
    found = await fetch_all(pool, "SELECT kind, text, embedding FROM moment ORDER BY ts_start")
    assert [m["kind"] for m in found] == ["visual", "visual", "speech", "marker"]
    assert all(m["embedding"] is not None for m in found)
    statuses = await fetch_all(pool, "SELECT task_name, status FROM procrastinate_jobs")
    assert {r["status"] for r in statuses} == {"succeeded"}
    assert {r["task_name"] for r in statuses} == set(jobs.PIPELINE_TASKS)


async def test_run_worker_builds_its_context_and_recovers_stalled_jobs(pool, pki_settings, dev, jobs_open, monkeypatch):
    marker = await insert_item(pool, dev["device_id"], kind="marker", seconds=0)
    job_id = await dispatch.defer_item(pool, marker)
    async with pool.connection() as conn:  # the previous worker died while running it
        cur = await conn.execute(
            "INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now() - interval '5 minutes') RETURNING id"
        )
        await conn.execute(
            "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
            ((await cur.fetchone())["id"], job_id),
        )
    monkeypatch.setattr(jobs.job_app, "periodic_registry", PeriodicRegistry())
    settings = pki_settings.model_copy(update={"ollama_url": "http://127.0.0.1:9", "worker_concurrency": 1})
    await worker.run_worker(settings, wait=False, install_signal_handlers=False, listen_notify=False)
    moment = await fetch_one(pool, "SELECT kind, embedding FROM moment")
    assert moment == {"kind": "marker", "embedding": None}  # no model server: embedding waits
    job = await fetch_one(pool, "SELECT status, attempts FROM procrastinate_jobs WHERE id = %s", (job_id,))
    assert job == {"status": "succeeded", "attempts": 2}  # one lost run, one that finished
    with pytest.raises(RuntimeError):
        context.get_context()


async def test_worker_health_follows_the_heartbeat(pool, database_url, monkeypatch, capsys):
    monkeypatch.setenv("LOOPI_DATABASE_URL", database_url)
    assert health.worker_alive(database_url) is False
    assert cli.main(["worker-health"]) == 1
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now() - interval '5 minutes')")
    assert health.worker_alive(database_url) is False
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now())")
    assert health.worker_alive(database_url) is True
    assert cli.main(["worker-health"]) == 0
    assert "worker alive" in capsys.readouterr().out


def test_worker_health_command_imports_only_psycopg(pool, database_url):  # pool: empty tables
    code = (
        "import sys; from loopi_brain import cli; rc = cli.main(['worker-health']); heavy = sorted(m for m in"
        " ('numpy', 'procrastinate', 'cryptography', 'pgvector', 'pydantic', 'PIL', 'faster_whisper')"
        " if m in sys.modules); print(rc, heavy)"
    )
    out = subprocess.run([sys.executable, "-c", code], env={**os.environ, "LOOPI_DATABASE_URL": database_url},
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip().endswith("1 []")  # no worker in the test database, nothing heavy imported


async def test_models_pull_fetches_every_configured_model(pki_settings):
    fake = FakeOllama(models=[])
    downloads, lines = [], []
    pulled = await worker.pull_models(pki_settings, fake.client(), download=downloads.append, report=lines.append)
    assert [body["model"] for _, body in fake.requests] == DEFAULT_OLLAMA_MODELS
    assert pulled == [*DEFAULT_OLLAMA_MODELS, "faster-whisper/large-v3-turbo"]
    assert downloads == [pki_settings]
    assert len(lines) == len(DEFAULT_OLLAMA_MODELS) + 1


def test_models_pull_command(monkeypatch, capsys):
    async def fake_pull(settings):
        return ["qwen3:8b", "faster-whisper/large-v3-turbo"]

    monkeypatch.setenv("LOOPI_DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(worker, "pull_models", fake_pull)
    assert cli.main(["models", "pull"]) == 0
    assert "ready: qwen3:8b, faster-whisper/large-v3-turbo" in capsys.readouterr().out


def test_download_whisper_uses_the_model_dir(monkeypatch, tmp_path):
    import faster_whisper

    calls = []
    monkeypatch.setattr(faster_whisper, "download_model", lambda size, cache_dir: calls.append((size, cache_dir)))
    worker.download_whisper(Settings(database_url="postgresql://x", model_dir=tmp_path))
    assert calls == [("large-v3-turbo", str(tmp_path / "whisper"))]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_worker.py -v`
Expected: FAIL — `ImportError: cannot import name 'health' from 'loopi_brain'`.

- [ ] **Step 3: Write the health check and the worker module**

Create `brain/src/loopi_brain/health.py`:

```python
"""`loopi-brain worker-health`: is a pipeline worker alive?

Docker runs this every 30 s inside the worker's container, so it imports nothing but psycopg
(no numpy, Procrastinate or cryptography): its CPU time counts against the worker.
"""
import psycopg

HEARTBEAT_MAX_AGE_S = 60


def worker_alive(database_url: str, max_age_s: int = HEARTBEAT_MAX_AGE_S) -> bool:
    """True if some worker sent a Procrastinate heartbeat in the last ``max_age_s`` seconds."""
    with psycopg.connect(database_url, connect_timeout=5) as conn:
        row = conn.execute(
            "SELECT count(*) FROM procrastinate_workers WHERE last_heartbeat > now() - make_interval(secs => %s)",
            (max_age_s,),
        ).fetchone()
    return row[0] > 0
```

Create `brain/src/loopi_brain/pipeline/worker.py`:

```python
"""The `loopi-brain worker` process: context, job app and the Procrastinate worker loop."""
from __future__ import annotations

import logging
from collections.abc import Callable

import anyio
import httpx

from ..config import Settings
from ..db import make_pool, migrate
from ..providers.config import load_providers
from ..providers.ollama import DEFAULT_TIMEOUT, OllamaProvider
from ..providers.router import build_router
from .asr import WHISPER_SIZE, WhisperTranscriber
from .context import PipelineContext, set_context
from .dispatch import recover_stalled
from .jobs import open_jobs

log = logging.getLogger(__name__)


async def run_worker(settings: Settings, **options) -> None:
    """Migrate, build the pipeline context and run jobs until stopped.

    ``options`` go to ``App.run_worker_async`` (tests pass ``wait=False`` to return once no job is left).
    """
    await anyio.to_thread.run_sync(migrate, settings.database_url)
    config = load_providers(settings.providers_file)  # a bad providers.yaml stops the worker here
    http = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    pool = make_pool(settings.database_url)
    await pool.open()
    try:
        set_context(PipelineContext(pool, settings, build_router(config, settings, http), WhisperTranscriber(settings)))
        async with open_jobs(pool) as app:
            await recover_stalled(pool)  # jobs of a worker that died more than 30 s ago run again now
            await app.run_worker_async(**{"concurrency": settings.worker_concurrency, **options})
    finally:
        set_context(None)
        await pool.close()
        await http.aclose()


def download_whisper(settings: Settings) -> None:
    from faster_whisper import download_model

    download_model(WHISPER_SIZE, cache_dir=str(settings.model_dir / "whisper"))


async def pull_models(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    download: Callable[[Settings], None] = download_whisper,
    report: Callable[[str], None] = print,
) -> list[str]:
    """Pull every Ollama model the providers config names, then download the Whisper model."""
    config = load_providers(settings.providers_file)
    ollama = OllamaProvider(settings.ollama_url, keep_alive=settings.ollama_keep_alive, client=client)
    pulled: list[str] = []
    try:
        for model in config.ollama_models():
            report(f"pulling {model} …")
            await ollama.pull(model)
            pulled.append(model)
    finally:
        if client is None:
            await ollama.aclose()
    report(f"downloading faster-whisper {WHISPER_SIZE} into {settings.model_dir / 'whisper'} …")
    await anyio.to_thread.run_sync(download, settings)
    pulled.append(f"faster-whisper/{WHISPER_SIZE}")
    return pulled
```

- [ ] **Step 4: Add the CLI commands**

Replace `brain/src/loopi_brain/cli.py` with:

```python
"""Command line entry point: ``loopi-brain <command>``."""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys
from pathlib import Path

# Subcommands import what they need: `worker-health` runs every 30 s and must stay light.
DEFAULT_PKI_DIR = "/srv/loopi/pki"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loopi-brain")
    sub = parser.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init-pki", help="create the brain CA and server certificate")
    init.add_argument("--pki-dir", default=os.environ.get("LOOPI_PKI_DIR", DEFAULT_PKI_DIR))
    init.add_argument("--host", action="append", default=[], help="DNS name (repeatable)")
    init.add_argument("--ip", action="append", default=[], help="IP address (repeatable)")
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("serve", help="apply migrations, then serve the API on 0.0.0.0:8000")
    sub.add_parser("pair-token", help="print a new one-time pairing token")
    sub.add_parser("worker", help="apply migrations, then run the pipeline worker")
    sub.add_parser("worker-health", help="exit 0 if a worker sent a heartbeat in the last 60 s")
    models = sub.add_parser("models", help="AI models")
    models_sub = models.add_subparsers(dest="models_cmd", required=True)
    models_sub.add_parser("pull", help="download the Ollama models in providers.yaml and the Whisper model")
    return parser


def _init_pki(args: argparse.Namespace) -> int:
    from . import pki

    pki_dir = Path(args.pki_dir)
    try:
        pki.init_pki(pki_dir, args.host, args.ip)
    except pki.PkiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"CA fingerprint: {pki.fingerprint(pki.ca_cert_pem(pki_dir))}")
    return 0


def _migrate() -> int:
    from .config import Settings
    from .db import migrate

    applied = migrate(Settings().database_url)
    print(f"applied: {', '.join(applied)}" if applied else "database up to date")
    return 0


def _serve() -> int:  # pragma: no cover - blocks serving; exercised by deploy/smoke.sh
    import uvicorn

    from .app import create_app
    from .config import Settings
    from .db import migrate

    settings = Settings()
    migrate(settings.database_url)
    uvicorn.run(create_app(settings), host="0.0.0.0", port=8000, proxy_headers=False)
    return 0


async def _pair_token() -> int:
    from . import devices, pki
    from .config import Settings
    from .db import make_pool

    settings = Settings()
    pool = make_pool(settings.database_url)
    await pool.open()
    try:
        token = await devices.create_pair_token(
            pool, dt.timedelta(minutes=settings.pair_token_ttl_minutes)
        )
    finally:
        await pool.close()
    print(token["token"])
    print(f"expires: {token['expires_at'].astimezone(settings.zone):%Y-%m-%d %H:%M %Z}")
    print(f"ca_fingerprint: {pki.fingerprint(pki.ca_cert_pem(settings.pki_dir))}")
    return 0


def _worker() -> int:  # pragma: no cover - runs until stopped; exercised by deploy/smoke.sh
    from .config import Settings
    from .pipeline.worker import run_worker

    asyncio.run(run_worker(Settings()))
    return 0


def _worker_health() -> int:
    from .health import worker_alive

    alive = worker_alive(os.environ["LOOPI_DATABASE_URL"])
    print("worker alive" if alive else "no worker heartbeat in the last 60 s")
    return 0 if alive else 1


def _models_pull() -> int:
    from .config import Settings
    from .pipeline.worker import pull_models

    pulled = asyncio.run(pull_models(Settings()))
    print(f"ready: {', '.join(pulled)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "init-pki":
        return _init_pki(args)
    if args.cmd == "migrate":
        return _migrate()
    if args.cmd == "serve":
        return _serve()
    if args.cmd == "pair-token":
        return asyncio.run(_pair_token())
    if args.cmd == "worker":
        return _worker()
    if args.cmd == "worker-health":
        return _worker_health()
    if args.cmd == "models":
        return _models_pull()
    return 2
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_worker.py tests/test_cli.py -v`
Expected: PASS (8 + 3 tests). `test_a_real_worker_runs_every_stage` takes a video, an audio file and a marker through every stage with a real Procrastinate worker.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: all pass, coverage ≥ 80 % (the prototype reached 98 %).

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/pipeline/worker.py brain/src/loopi_brain/health.py brain/src/loopi_brain/cli.py \
  brain/tests/test_worker.py
git commit -F - <<'EOF'
feat(brain): loopi-brain worker, worker-health and models pull

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 9: Timeline enrichment and the "Xử lý" status page

**Files:**
- Create: `brain/src/loopi_brain/pipeline/status.py`
- Create: `brain/src/loopi_brain/web/templates/pipeline.html`, `brain/src/loopi_brain/web/templates/_retried.html`
- Modify: `brain/src/loopi_brain/app.py` (`http_client`), `brain/src/loopi_brain/web/routes.py`, `brain/src/loopi_brain/web/templates/base.html`, `brain/src/loopi_brain/web/templates/timeline.html`
- Test: `brain/tests/test_web_pipeline.py`

**Interfaces:**
- Consumes: Task 1–8 tables; Task 2 `load_providers`, `build_router`, `Router.health`; Task 3 `job_app` (opened by the lifespan / `jobs_open`); helpers `insert_keyframe`, `FakeOllama`.
- Produces:
  - `create_app(settings, pool=None, http_client=None)`; `app.state.http` (a 5 s-timeout `httpx.AsyncClient` when none is given, closed by the lifespan).
  - `status.fmt_duration(seconds) -> str`, `async pipeline_status(pool) -> dict` (`pending` — without items whose job is dead-lettered, `keyframes_waiting`, `moments_waiting`, `latency.video|audio = {n, avg, p95}`, `worker_alive`, `failed` (newest 100, each with `retryable`), `failed_total`), `async model_health(settings, client) -> dict` (`error`, `url`, `reachable`, `missing`), `async retry_failed_job(pool, job_id) -> bool`, `async retry_all_failed(pool) -> int`, `TASK_LABELS`, `NOT_RETRIED = ("embed_moments",)`.
  - `GET /` (keyframes, speech, markers per local hour; raw segments in `<details>`; a pending item whose job is dead-lettered shows "Lỗi phân tích"), `GET /pipeline`, `POST /pipeline/jobs/{job_id}/retry` and `POST /pipeline/jobs/retry-failed` (`HX-Request` required; a single retry answers 409 when the job is not `failed`).

Rulings in this task: the API reads `providers.yaml` only on the status page, so a broken file is shown there instead of stopping ingest (the worker refuses to start on it). Latency (last 24 h): video = `keyframe.captioned_at − item.ended_at`, audio = `item.analyzed_at − item.ended_at`. The page also shows whether a worker heartbeat is younger than 60 s. A retry of a job that is no longer `failed` (or unknown) answers 409, explained in Vietnamese by the HTMX error handler. "Xử lý lại tất cả" retries every dead letter at once (a mass failure must not need hundreds of clicks); the embedding run has no retry button, because the dispatcher requests a new one every minute. The failed-job list joins item and keyframe on the job's arguments (indexed primary keys).

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_web_pipeline.py`:

```python
import datetime as dt

import httpx
import pytest
from psycopg.types.json import Jsonb

from helpers import (
    DEFAULT_CAPTION,
    FakeOllama,
    fetch_all,
    fetch_one,
    insert_item,
    insert_keyframe,
    new_ulid,
    paired_device,
)
from loopi_brain.app import create_app
from loopi_brain.pipeline import dispatch
from loopi_brain.pipeline.status import fmt_duration

HX = {"HX-Request": "true"}
DAY = "2026-10-14"
UTC_9AM_SEOUL = dt.datetime(2026, 10, 14, 0, 30, tzinfo=dt.UTC)  # 09:30 in Seoul


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def ollama():
    return FakeOllama()


@pytest.fixture
async def web(pool, pki_settings, jobs_open, ollama):
    app = create_app(pki_settings, pool=pool, http_client=ollama.client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


async def _moment(pool, device_id, kind, ts, text, lang=None):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, lang, model_version)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, 'test')",
            (new_ulid(), device_id, ts, ts, kind, text, lang),
        )


def test_durations_read_in_vietnamese():
    assert [fmt_duration(s) for s in (None, 4.6, 200, 7500)] == ["—", "5 giây", "3 phút 20 giây", "2 giờ 5 phút"]


async def test_timeline_shows_keyframes_speech_and_markers_by_local_hour(web, pool, pki_settings, dev):
    device_id = dev["device_id"]
    item_id = await insert_item(pool, device_id, kind="video", started_at=UTC_9AM_SEOUL)
    kf = await insert_keyframe(pool, pki_settings, item_id, device_id, UTC_9AM_SEOUL + dt.timedelta(seconds=10))
    waiting = await insert_keyframe(pool, pki_settings, item_id, device_id, UTC_9AM_SEOUL + dt.timedelta(seconds=50))
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption = %s WHERE id = %s", (Jsonb(DEFAULT_CAPTION), kf))
    await _moment(pool, device_id, "speech", UTC_9AM_SEOUL + dt.timedelta(minutes=5), "Mai mình đi chợ <b>nhé</b>", "vi")
    await _moment(pool, device_id, "marker", UTC_9AM_SEOUL + dt.timedelta(minutes=7), "chìa khoá")
    text = (await web.get("/", params={"day": DAY})).text
    assert "<h2>09:00</h2>" in text
    assert f'id="kf-{kf}"' in text and 'loading="lazy"' in text
    row = await fetch_one(pool, "SELECT path FROM keyframe WHERE id = %s", (kf,))
    assert f'src="/media/{row["path"]}"' in text
    assert f"{DEFAULT_CAPTION['scene']} — {DEFAULT_CAPTION['activity']}" in text
    assert f'id="kf-{waiting}"' in text and "Đang chờ mô tả…" in text
    assert '<span class="lang">VI</span> Mai mình đi chợ &lt;b&gt;nhé&lt;/b&gt;' in text
    assert "09:37:00</time> ★ chìa khoá" in text
    assert "<details>" in text and f'id="item-{item_id}"' in text and "Đang phân tích…" in text


async def test_analysed_items_are_not_marked_pending(web, pool, dev):
    item_id = await insert_item(pool, dev["device_id"], kind="audio", started_at=UTC_9AM_SEOUL)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (item_id,))
    text = (await web.get("/", params={"day": DAY})).text
    assert f'id="item-{item_id}"' in text and "Đang phân tích…" not in text


async def test_pipeline_page_shows_backlog_latency_models_and_dead_letters(web, pool, pki_settings, dev, ollama):
    ollama.models.remove("qwen3:4b")
    device_id = dev["device_id"]
    now = dt.datetime.now(dt.UTC)
    for kind in ("video", "video", "audio"):
        await insert_item(pool, device_id, kind=kind, started_at=now - dt.timedelta(hours=2))
    done = await insert_item(pool, device_id, kind="video", started_at=now - dt.timedelta(minutes=11), seconds=60)
    kf = await insert_keyframe(pool, pki_settings, done, device_id, now - dt.timedelta(minutes=10, seconds=30))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (done,))
        await conn.execute("UPDATE keyframe SET caption = '{}', captioned_at = now() WHERE id = %s", (kf,))
    broken = await insert_item(pool, device_id, kind="marker", started_at=now - dt.timedelta(hours=3), seconds=0)
    job_id = await dispatch.defer_item(pool, broken)
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 4 WHERE id = %s", (job_id,))
        await conn.execute("UPDATE item SET analysis_error = 'RuntimeError: bad marker' WHERE id = %s", (broken,))
    text = (await web.get("/pipeline")).text
    assert '<a href="/pipeline">Xử lý</a>' in text
    assert "Video chờ phân tích: 2" in text and "Âm thanh chờ nhận dạng: 1" in text
    assert "Đánh dấu chờ ghi nhận: 0" in text and "Khung hình chờ mô tả: 0" in text  # the failed one is not waiting
    assert "Ghi hình → mô tả: trung bình 10 phút" in text and "(1 mẫu)" in text
    assert "Ghi âm → transcript: chưa có dữ liệu" in text
    assert "Thiếu model: qwen3:4b" in text and "loopi-brain models pull" in text
    assert "Worker: <span class=\"err\">không thấy tín hiệu</span>" in text
    assert f'id="job-{job_id}"' in text and "Ghi nhận đánh dấu" in text and "RuntimeError: bad marker" in text
    assert f'hx-post="/pipeline/jobs/{job_id}/retry"' in text and "Xử lý lại" in text
    assert "Job lỗi (1)" in text and 'hx-post="/pipeline/jobs/retry-failed"' in text
    broken_day = (now - dt.timedelta(hours=3)).astimezone(pki_settings.zone).date().isoformat()
    timeline = (await web.get("/", params={"day": broken_day})).text
    assert f'id="item-{broken}"' in timeline and "Lỗi phân tích" in timeline


async def test_pipeline_page_reports_a_stopped_model_server_and_a_live_worker(web, pool, ollama):
    ollama.down = True
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now())")
    text = (await web.get("/pipeline")).text
    assert "Không kết nối được Ollama (http://models:11434)" in text
    assert '<span class="online">đang chạy</span>' in text and "Không có job lỗi." in text


async def test_pipeline_page_reports_a_broken_providers_file(pool, pki_settings, jobs_open, tmp_path):
    bad = tmp_path / "providers.yaml"
    bad.write_text("tasks: {}\n")
    app = create_app(pki_settings.model_copy(update={"providers_file": bad}), pool=pool,
                     http_client=FakeOllama().client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        text = (await c.get("/pipeline")).text
    assert "Cấu hình provider lỗi" in text


async def test_retry_button_requeues_a_dead_lettered_job_once(web, pool, dev):
    item_id = await insert_item(pool, dev["device_id"], kind="marker", seconds=0)
    job_id = await dispatch.defer_item(pool, item_id)
    assert (await web.post(f"/pipeline/jobs/{job_id}/retry", headers=HX)).status_code == 409  # still todo
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 4 WHERE id = %s", (job_id,))
    assert (await web.post(f"/pipeline/jobs/{job_id}/retry")).status_code == 403
    r = await web.post(f"/pipeline/jobs/{job_id}/retry", headers=HX)
    assert r.status_code == 200 and f"Đã đưa job {job_id} vào hàng đợi xử lý lại." in r.text
    job = await fetch_one(pool, "SELECT status FROM procrastinate_jobs WHERE id = %s", (job_id,))
    assert job["status"] == "todo"
    assert (await web.post(f"/pipeline/jobs/{job_id}/retry", headers=HX)).status_code == 409
    assert (await web.post("/pipeline/jobs/999999/retry", headers=HX)).status_code == 409
    # the page carries the Vietnamese explanation the retry button shows for a 409
    assert "409: \"Job không còn ở trạng thái lỗi" in (await web.get("/pipeline")).text


async def test_retry_all_requeues_every_dead_letter_but_the_embedding_run(web, pool, dev):
    job_ids = []
    for minute in range(2):
        item_id = await insert_item(pool, dev["device_id"], kind="marker", seconds=0,
                                    started_at=UTC_9AM_SEOUL + dt.timedelta(minutes=minute))
        job_ids.append(await dispatch.defer_item(pool, item_id))
    await dispatch.defer_embed(pool)
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 4")
    text = (await web.get("/pipeline")).text
    assert "Job lỗi (3)" in text and "tự chạy lại mỗi phút" in text
    assert (await web.post("/pipeline/jobs/retry-failed")).status_code == 403
    r = await web.post("/pipeline/jobs/retry-failed", headers=HX)
    assert r.status_code == 200 and "Đã đưa 2 job vào hàng đợi xử lý lại." in r.text
    statuses = {r["task_name"]: r["status"] for r in await fetch_all(pool, "SELECT task_name, status FROM procrastinate_jobs")}
    assert statuses == {"index_marker": "todo", "embed_moments": "failed"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_web_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'loopi_brain.pipeline.status'`.

- [ ] **Step 3: Write the status queries**

Create `brain/src/loopi_brain/pipeline/status.py`:

```python
"""Numbers for the /pipeline page: backlog, latency, model server, dead-letter jobs."""
from __future__ import annotations

import datetime as dt
import logging

import httpx
from procrastinate.exceptions import ConnectorException
from psycopg_pool import AsyncConnectionPool

from ..config import Settings
from ..providers.config import ProvidersConfigError, load_providers
from ..providers.router import build_router
from .jobs import job_app

log = logging.getLogger(__name__)
FAILED_LIMIT = 100
TASK_LABELS = {
    "analyze_video": "Phân tích video",
    "caption_keyframe": "Mô tả khung hình",
    "transcribe_audio": "Nhận dạng giọng nói",
    "index_marker": "Ghi nhận đánh dấu",
    "embed_moments": "Tạo embedding",
}
_LATENCY = (
    "SELECT count(*) AS n, avg(s) AS avg, percentile_cont(0.95) WITHIN GROUP (ORDER BY s) AS p95 FROM ({}) t"
)
# Recording -> caption: from the end of the video segment to its keyframe's caption.
VIDEO_LATENCY = _LATENCY.format(
    "SELECT extract(epoch FROM k.captioned_at - i.ended_at)::float8 AS s FROM keyframe k"
    " JOIN item i ON i.id = k.item_id WHERE k.captioned_at > now() - interval '24 hours'"
)
# Recording -> transcript: from the end of the audio segment to the end of its transcription.
AUDIO_LATENCY = _LATENCY.format(
    "SELECT extract(epoch FROM analyzed_at - ended_at)::float8 AS s FROM item"
    " WHERE kind = 'audio' AND analysis_state = 'done' AND analyzed_at > now() - interval '24 hours'"
)
FAILED_JOBS = """
SELECT j.id, j.task_name, j.attempts,
       COALESCE(j.args->>'item_id', j.args->>'keyframe_id') AS target,
       COALESCE(i.analysis_error, k.caption_error) AS error
  FROM procrastinate_jobs j
  LEFT JOIN item i ON i.id = j.args->>'item_id'
  LEFT JOIN keyframe k ON k.id = j.args->>'keyframe_id'
 WHERE j.status = 'failed'
 ORDER BY j.id DESC
 LIMIT %s
"""
# Items still pending whose job is on the dead-letter list are "failed", not "waiting".
PENDING_ITEMS = """
SELECT i.kind, count(*) AS n FROM item i
  LEFT JOIN procrastinate_jobs j ON j.id = i.analysis_job_id AND j.status = 'failed'
 WHERE i.analysis_state = 'pending' AND j.id IS NULL
 GROUP BY i.kind
"""
# The embedding run is re-requested by the dispatcher every minute: it needs no retry button.
NOT_RETRIED = ("embed_moments",)


def fmt_duration(seconds: float | None) -> str:
    """Vietnamese duration: 45 giây, 3 phút 20 giây, 2 giờ 5 phút."""
    if seconds is None:
        return "—"
    total = max(0, round(seconds))
    if total < 60:
        return f"{total} giây"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} phút {secs} giây"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} giờ {minutes} phút"


async def pipeline_status(pool: AsyncConnectionPool) -> dict:
    async with pool.connection() as conn:
        cur = await conn.execute(PENDING_ITEMS)
        pending = {"video": 0, "audio": 0, "marker": 0} | {r["kind"]: r["n"] for r in await cur.fetchall()}
        cur = await conn.execute("SELECT count(*) AS n FROM procrastinate_jobs WHERE status = 'failed'")
        failed_total = (await cur.fetchone())["n"]
        cur = await conn.execute("SELECT count(*) AS n FROM keyframe WHERE caption IS NULL")
        keyframes_waiting = (await cur.fetchone())["n"]
        cur = await conn.execute("SELECT count(*) AS n FROM moment WHERE embedding IS NULL")
        moments_waiting = (await cur.fetchone())["n"]
        latency = {}
        for name, sql in (("video", VIDEO_LATENCY), ("audio", AUDIO_LATENCY)):
            latency[name] = await (await conn.execute(sql)).fetchone()
        cur = await conn.execute("SELECT max(last_heartbeat) AS seen FROM procrastinate_workers")
        worker_seen = (await cur.fetchone())["seen"]
        cur = await conn.execute(FAILED_JOBS, (FAILED_LIMIT,))
        failed = [
            {**r, "label": TASK_LABELS.get(r["task_name"], r["task_name"]), "retryable": r["task_name"] not in NOT_RETRIED}
            for r in await cur.fetchall()
        ]
    worker_alive = worker_seen is not None and dt.datetime.now(dt.UTC) - worker_seen < dt.timedelta(seconds=60)
    return {
        "pending": pending,
        "keyframes_waiting": keyframes_waiting,
        "moments_waiting": moments_waiting,
        "latency": latency,
        "worker_alive": worker_alive,
        "failed": failed,
        "failed_total": failed_total,
    }


async def model_health(settings: Settings, client: httpx.AsyncClient) -> dict:
    """Is the Ollama server reachable, and does it have every model providers.yaml names?"""
    try:
        config = load_providers(settings.providers_file)
    except ProvidersConfigError as exc:
        return {"error": str(exc)[:500], "url": settings.ollama_url, "reachable": False, "missing": []}
    health = await build_router(config, settings, client).health()
    return {"error": None, "url": settings.ollama_url, "reachable": health.reachable, "missing": health.missing}


async def retry_failed_job(pool: AsyncConnectionPool, job_id: int) -> bool:
    """Put a dead-lettered job back in its queue (it runs once more); False if it is not 'failed'."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT status FROM procrastinate_jobs WHERE id = %s", (job_id,))
        row = await cur.fetchone()
    if row is None or row["status"] != "failed":
        return False
    try:
        await job_app.job_manager.retry_job_by_id_async(job_id, retry_at=dt.datetime.now(dt.UTC))
    except ConnectorException:  # retried by someone else in between
        log.info("job %s was no longer failed", job_id)
        return False
    return True


async def retry_all_failed(pool: AsyncConnectionPool) -> int:
    """Retry every dead-lettered job (but the embedding run); return how many went back to the queue."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM procrastinate_jobs WHERE status = 'failed' AND NOT (task_name = ANY(%s)) ORDER BY id",
            (list(NOT_RETRIED),),
        )
        job_ids = [r["id"] for r in await cur.fetchall()]
    return sum([await retry_failed_job(pool, job_id) for job_id in job_ids])
```

- [ ] **Step 4: Give the app an HTTP client for the status page**

Replace `brain/src/loopi_brain/app.py` with:

```python
"""FastAPI application factory."""
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from psycopg_pool import AsyncConnectionPool

from . import media
from .api.device_routes import router as device_router
from .config import Settings
from .db import make_pool
from .pipeline.jobs import open_jobs
from .web.routes import router as web_router

log = logging.getLogger(__name__)


# The status page asks the model server for its models; it must answer fast or be reported as down.
STATUS_HTTP_TIMEOUT = httpx.Timeout(5.0)


def create_app(
    settings: Settings,
    pool: AsyncConnectionPool | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    owns_pool = pool is None
    active_pool = pool if pool is not None else make_pool(settings.database_url)
    owns_http = http_client is None
    http = http_client if http_client is not None else httpx.AsyncClient(timeout=STATUS_HTTP_TIMEOUT)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if owns_pool:
            await active_pool.open()
        try:
            async with open_jobs(active_pool):  # the API defers jobs and retries failed ones
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
            if owns_http:
                await http.aclose()
            if owns_pool:
                await active_pool.close()

    app = FastAPI(title="Loopi Brain", lifespan=lifespan)
    app.state.settings = settings
    app.state.pool = active_pool
    app.state.http = http

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(device_router)
    app.include_router(web_router)
    settings.media_root.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=settings.media_root), name="media")
    app.mount(
        "/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static"
    )
    return app
```

- [ ] **Step 5: Write the routes**

Replace `brain/src/loopi_brain/web/routes.py` with:

```python
"""Server-rendered web UI (Jinja + HTMX). Protected by Caddy basic auth."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import devices, pki
from ..pipeline import status

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["duration"] = status.fmt_duration
ONLINE_WINDOW = dt.timedelta(seconds=45)
COMMAND_LABELS = {
    "pause": "Tạm dừng ghi",
    "resume": "Tiếp tục ghi",
    "audio-on": "Bật ghi âm",
    "audio-off": "Tắt ghi âm",
}
COMMANDS: dict[str, tuple[str, dict]] = {
    "pause": ("pause", {}),
    "resume": ("resume", {}),
    "audio-on": ("set_config", {"audio_enabled": True}),
    "audio-off": ("set_config", {"audio_enabled": False}),
}

router = APIRouter()


def require_htmx(hx_request: str | None = Header(default=None)) -> None:
    """CSRF guard: cross-site forms cannot set custom headers."""
    if hx_request != "true":
        raise HTTPException(status_code=403, detail="missing HX-Request header")


def day_bounds_utc(day: dt.date, zone: ZoneInfo) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, dt.time.min, tzinfo=zone)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=zone)
    return start.astimezone(dt.UTC), end.astimezone(dt.UTC)


def _hour() -> dict[str, list]:
    return {"keyframes": [], "speech": [], "markers": [], "segments": []}


async def _day_by_hour(pool, day: dt.date, zone: ZoneInfo) -> dict[int, dict[str, list]]:
    """Keyframes, speech, markers and raw segments of one local day, grouped by local hour."""
    start, end = day_bounds_utc(day, zone)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT i.id, i.kind, i.started_at, i.ended_at, i.status, i.view_path, i.meta, i.analysis_state,"
            " j.status IS NOT DISTINCT FROM 'failed' AS analysis_failed"
            " FROM item i LEFT JOIN procrastinate_jobs j ON j.id = i.analysis_job_id"
            " WHERE i.started_at >= %s AND i.started_at < %s ORDER BY i.started_at",
            (start, end),
        )
        items = await cur.fetchall()
        cur = await conn.execute(
            "SELECT id, ts, path, caption FROM keyframe WHERE ts >= %s AND ts < %s ORDER BY ts", (start, end)
        )
        keyframes = await cur.fetchall()
        cur = await conn.execute(
            "SELECT id, kind, ts_start, lang, text FROM moment"
            " WHERE kind IN ('speech', 'marker') AND ts_start >= %s AND ts_start < %s ORDER BY ts_start",
            (start, end),
        )
        moments = await cur.fetchall()
    hours: dict[int, dict[str, list]] = defaultdict(_hour)
    for row in items:
        local_start = row["started_at"].astimezone(zone)
        hours[local_start.hour]["segments"].append(
            {**row, "local_start": local_start, "local_end": row["ended_at"].astimezone(zone)}
        )
    for row in keyframes:
        local_ts = row["ts"].astimezone(zone)
        hours[local_ts.hour]["keyframes"].append({**row, "local_ts": local_ts})
    for row in moments:
        local_ts = row["ts_start"].astimezone(zone)
        hours[local_ts.hour]["speech" if row["kind"] == "speech" else "markers"].append({**row, "local_ts": local_ts})
    return dict(sorted(hours.items()))


def _ca_fingerprint(request: Request) -> str:
    return pki.fingerprint(pki.ca_cert_pem(request.app.state.settings.pki_dir))


@router.get("/", response_class=HTMLResponse)
async def timeline(request: Request, day: dt.date | None = Query(default=None)):
    zone = request.app.state.settings.zone
    day = day or dt.datetime.now(zone).date()
    hours = await _day_by_hour(request.app.state.pool, day, zone)
    return TEMPLATES.TemplateResponse(
        request,
        "timeline.html",
        {"day": day, "prev_day": day - dt.timedelta(days=1), "next_day": day + dt.timedelta(days=1), "hours": hours},
    )


@router.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request):
    zone = request.app.state.settings.zone
    now = dt.datetime.now(dt.UTC)
    rows = []
    for row in await devices.list_devices(request.app.state.pool):
        seen = row["last_heartbeat_at"]
        rows.append({
            **row,
            "online": seen is not None and now - seen < ONLINE_WINDOW,
            "hb": row["last_heartbeat"] or {},
            "seen_local": seen.astimezone(zone) if seen else None,
        })
    return TEMPLATES.TemplateResponse(
        request, "devices.html", {"devices": rows, "ca_fingerprint": _ca_fingerprint(request)}
    )


@router.post("/devices/pair-token", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def new_pair_token(request: Request):
    settings = request.app.state.settings
    token = await devices.create_pair_token(
        request.app.state.pool, dt.timedelta(minutes=settings.pair_token_ttl_minutes)
    )
    return TEMPLATES.TemplateResponse(
        request,
        "_pair_token.html",
        {
            "token": token["token"],
            "expires_at": token["expires_at"].astimezone(settings.zone),
            "ca_fingerprint": _ca_fingerprint(request),
            "brain_url": f"https://{request.headers.get('host', request.url.netloc)}",
        },
    )


@router.post(
    "/devices/{device_id}/commands/{action}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_htmx)],
)
async def send_command(request: Request, device_id: str, action: str):
    if action not in COMMANDS:
        raise HTTPException(status_code=404, detail="unknown action")
    type_, payload = COMMANDS[action]
    try:
        await devices.enqueue_command(request.app.state.pool, device_id, type_, payload)
    except psycopg.errors.ForeignKeyViolation as exc:
        raise HTTPException(status_code=404, detail="unknown device") from exc
    return TEMPLATES.TemplateResponse(request, "_queued.html", {"label": COMMAND_LABELS[action], "revoked": False})


@router.post("/devices/{device_id}/revoke", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def revoke(request: Request, device_id: str):
    if not await devices.revoke_device(request.app.state.pool, device_id):
        raise HTTPException(status_code=404, detail="unknown device")
    return TEMPLATES.TemplateResponse(request, "_queued.html", {"label": None, "revoked": True})


@router.get("/pipeline", response_class=HTMLResponse)
async def pipeline_page(request: Request):
    state = request.app.state
    return TEMPLATES.TemplateResponse(
        request,
        "pipeline.html",
        {
            **await status.pipeline_status(state.pool),
            "health": await status.model_health(state.settings, state.http),
        },
    )


@router.post("/pipeline/jobs/retry-failed", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def retry_all_jobs(request: Request):
    count = await status.retry_all_failed(request.app.state.pool)
    return TEMPLATES.TemplateResponse(request, "_retried.html", {"count": count})


@router.post("/pipeline/jobs/{job_id}/retry", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def retry_job(request: Request, job_id: int):
    if not await status.retry_failed_job(request.app.state.pool, job_id):
        raise HTTPException(status_code=409, detail="job is not in the failed state")
    return TEMPLATES.TemplateResponse(request, "_retried.html", {"job_id": job_id})
```

- [ ] **Step 6: Write the templates**

Replace `brain/src/loopi_brain/web/templates/base.html` with:

```html
<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Loopi</title>
  <script src="/static/htmx.min.js"></script>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
    body { max-width: 960px; margin: 0 auto; padding: 16px; }
    header nav a { margin-right: 16px; }
    .hour h2 { font-size: 1rem; border-bottom: 1px solid #8884; }
    .item { display: flex; gap: 12px; align-items: center; padding: 4px 0; list-style: none; }
    .item video { max-width: 320px; }
    .frames { display: flex; flex-wrap: wrap; gap: 8px; }
    .frames figure { margin: 0; width: 224px; }
    .frames img { width: 224px; height: auto; border-radius: 4px; }
    .frames figcaption, .muted { font-size: 0.85rem; color: #888; }
    .lang { font-size: 0.75rem; border: 1px solid #8886; border-radius: 4px; padding: 0 4px; }
    table { border-collapse: collapse; width: 100%; }
    td, th { border-bottom: 1px solid #8884; padding: 4px; text-align: left; vertical-align: top; }
    .err { color: #c33; }
    .card { border: 1px solid #8886; border-radius: 8px; padding: 12px; margin: 12px 0; }
    .online { color: #2a2; } .offline { color: #999; }
    code { word-break: break-all; }
  </style>
</head>
<body>
  <header><nav><a href="/">Dòng thời gian</a><a href="/pipeline">Xử lý</a><a href="/devices">Thiết bị</a></nav></header>
  <div id="hx-error" class="err" role="alert"></div>
  <main>{% block content %}{% endblock %}</main>
  <script>
    const HX_ERRORS = {403: "Yêu cầu không hợp lệ", 404: "Không tìm thấy thiết bị",
                       409: "Job không còn ở trạng thái lỗi, hãy tải lại trang"};
    document.body.addEventListener("htmx:responseError", (e) => {
      document.getElementById("hx-error").textContent =
        HX_ERRORS[e.detail.xhr.status] || "Có lỗi, thử lại";
    });
    document.body.addEventListener("htmx:sendError", () => {
      document.getElementById("hx-error").textContent = "Có lỗi, thử lại";
    });
    document.body.addEventListener("htmx:beforeRequest", () => {
      document.getElementById("hx-error").textContent = "";
    });
  </script>
</body>
</html>
```

Replace `brain/src/loopi_brain/web/templates/timeline.html` with:

```html
{% extends "base.html" %}
{% block content %}
<nav class="days">
  <a href="/?day={{ prev_day }}">← {{ prev_day }}</a>
  <strong>{{ day }}</strong>
  <a href="/?day={{ next_day }}">{{ next_day }} →</a>
</nav>
{% if not hours %}<p>Không có dữ liệu cho ngày này.</p>{% endif %}
{% for hour, h in hours.items() %}
<section class="hour">
  <h2>{{ "%02d:00"|format(hour) }}</h2>
  {% if h.keyframes %}
  <div class="frames">
  {% for kf in h.keyframes %}
    <figure id="kf-{{ kf.id }}">
      <img src="/media/{{ kf.path }}" loading="lazy" alt="{{ kf.caption.scene if kf.caption else 'Khung hình' }}">
      <figcaption><time>{{ kf.local_ts.strftime("%H:%M:%S") }}</time>
        {% if kf.caption %}{{ kf.caption.scene }} — {{ kf.caption.activity }}
        {% else %}Đang chờ mô tả…{% endif %}</figcaption>
    </figure>
  {% endfor %}
  </div>
  {% endif %}
  {% for m in h.speech %}
  <p class="speech"><time>{{ m.local_ts.strftime("%H:%M:%S") }}</time> <span class="lang">{{ m.lang|upper }}</span> {{ m.text }}</p>
  {% endfor %}
  {% for m in h.markers %}
  <p class="marker"><time>{{ m.local_ts.strftime("%H:%M:%S") }}</time> ★ {{ m.text }}</p>
  {% endfor %}
  <details>
    <summary>{{ h.segments|length }} đoạn ghi</summary>
    <ul>
    {% for it in h.segments %}
      <li class="item {{ it.kind }}" id="item-{{ it.id }}">
        <time>{{ it.local_start.strftime("%H:%M:%S") }}–{{ it.local_end.strftime("%H:%M:%S") }}</time>
        {% if it.kind == "video" %}
          {% if it.status == "ready" %}<video controls preload="none" src="/media/{{ it.view_path }}"></video>
          {% elif it.status == "error" %}<span class="err">Không chuyển được video</span>
          {% else %}<span>Đang xử lý…</span>{% endif %}
        {% elif it.kind == "audio" %}
          <audio controls preload="none" src="/media/{{ it.view_path }}"></audio>
        {% else %}
          <span>★ {{ it.meta.get("note", "Đánh dấu") }}</span>
        {% endif %}
        {% if it.analysis_state == "pending" %}
          {% if it.analysis_failed %}<span class="err">Lỗi phân tích</span>
          {% else %}<span class="muted">Đang phân tích…</span>{% endif %}
        {% endif %}
        <small>{{ it.id }}</small>
      </li>
    {% endfor %}
    </ul>
  </details>
</section>
{% endfor %}
{% endblock %}
```

Create `brain/src/loopi_brain/web/templates/pipeline.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Xử lý</h1>
<div class="card">
  <h2>Hàng đợi</h2>
  <ul>
    <li>Video chờ phân tích: {{ pending.video }}</li>
    <li>Âm thanh chờ nhận dạng: {{ pending.audio }}</li>
    <li>Đánh dấu chờ ghi nhận: {{ pending.marker }}</li>
    <li>Khung hình chờ mô tả: {{ keyframes_waiting }}</li>
    <li>Khoảnh khắc chờ embedding: {{ moments_waiting }}</li>
  </ul>
  <p>Worker: {% if worker_alive %}<span class="online">đang chạy</span>{% else %}<span class="err">không thấy tín hiệu</span>{% endif %}</p>
</div>
<div class="card">
  <h2>Độ trễ 24 giờ qua</h2>
  <ul>
  {% for name, label in [("video", "Ghi hình → mô tả"), ("audio", "Ghi âm → transcript")] %}
    {% set l = latency[name] %}
    <li>{{ label }}: {% if l.n %}trung bình {{ l.avg|duration }} · p95 {{ l.p95|duration }} ({{ l.n }} mẫu){% else %}chưa có dữ liệu{% endif %}</li>
  {% endfor %}
  </ul>
</div>
<div class="card">
  <h2>Máy chủ model</h2>
  {% if health.error %}<p class="err">Cấu hình provider lỗi: {{ health.error }}</p>
  {% elif not health.reachable %}<p class="err">Không kết nối được Ollama ({{ health.url }}). Việc xử lý sẽ tự tiếp tục khi máy chủ chạy lại.</p>
  {% elif health.missing %}<p class="err">Thiếu model: {{ health.missing|join(", ") }}. Chạy <code>loopi-brain models pull</code>.</p>
  {% else %}<p class="online">Ollama sẵn sàng, đủ model.</p>{% endif %}
</div>
<div class="card">
  <h2>Job lỗi ({{ failed_total }})</h2>
  {% if failed_total > failed|length %}<p>Hiện {{ failed|length }} job mới nhất.</p>{% endif %}
  {% if failed %}
  <button hx-post="/pipeline/jobs/retry-failed" hx-target="#retry-all"
          hx-confirm="Xử lý lại tất cả job lỗi?">Xử lý lại tất cả</button>
  <div id="retry-all"></div>
  <table>
    <tr><th>Job</th><th>Tác vụ</th><th>Đối tượng</th><th>Số lần chạy</th><th>Lỗi</th><th></th></tr>
    {% for j in failed %}
    <tr id="job-{{ j.id }}">
      <td>{{ j.id }}</td><td>{{ j.label }}</td><td><code>{{ j.target or "—" }}</code></td><td>{{ j.attempts }}</td>
      <td><code>{{ j.error or "—" }}</code></td>
      <td>{% if j.retryable %}<button hx-post="/pipeline/jobs/{{ j.id }}/retry" hx-target="#retry-{{ j.id }}">Xử lý lại</button>
          <div id="retry-{{ j.id }}"></div>{% else %}<span class="muted">tự chạy lại mỗi phút</span>{% endif %}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}<p>Không có job lỗi.</p>{% endif %}
</div>
{% endblock %}
```

Create `brain/src/loopi_brain/web/templates/_retried.html`:

```html
{% if count is defined %}<span>Đã đưa {{ count }} job vào hàng đợi xử lý lại.</span>
{% else %}<span>Đã đưa job {{ job_id }} vào hàng đợi xử lý lại.</span>{% endif %}
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_web_pipeline.py tests/test_web.py tests/test_media.py -v`
Expected: PASS — the 8 new tests and every P1 web/media test (item ids and times are still on the timeline, inside `<details>`).

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: all pass, coverage ≥ 80 %.

- [ ] **Step 8: Commit**

```bash
git add brain/src/loopi_brain/pipeline/status.py brain/src/loopi_brain/app.py brain/src/loopi_brain/web \
  brain/tests/test_web_pipeline.py
git commit -F - <<'EOF'
feat(brain): keyframes, speech and markers on the timeline; pipeline status page with retry

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Pre-flight rulings for this task (controller, 2026-10-15; they amend the steps above):**
- **R-F2:** add `jobs.DISPATCH_PENDING`, `jobs.RETRY_STALLED` and `jobs.REMOVE_OLD_JOBS` to `NOT_RETRIED` (the scheduler starts a new tick by itself, so a failed tick gets no "Xử lý lại" button), give them Vietnamese labels in `TASK_LABELS`, and write `TASK_LABELS`/`NOT_RETRIED` keys with the `jobs.*` constants rather than string literals. Add one assertion to the status-page test: a failed `dispatch_pending` job is listed without a retry button.
- **R-F3 (dead-lettered captions):** a keyframe whose caption job is `failed` is not waiting any more. In `pipeline_status`, count "Khung hình chờ mô tả" with the same `LEFT JOIN procrastinate_jobs j ON j.id = k.caption_job_id AND j.status = 'failed' … AND j.id IS NULL` pattern `PENDING_ITEMS` uses; in `_day_by_hour`, select `j.status IS NOT DISTINCT FROM 'failed' AS caption_failed` and show "Lỗi mô tả" instead of "Đang chờ mô tả…" for such a keyframe. Add a test: a keyframe with a failed caption job is not counted as waiting and the timeline shows "Lỗi mô tả".
- **R-F4:** `tests/test_web_pipeline.py` uses `helpers.insert_moment` (added in Task 3) instead of defining `_moment`.

---

### Task 10: Compose `worker` and `models` services, smoke test, README

**Files:**
- Modify: `deploy/docker-compose.yml`, `deploy/smoke.sh`, `deploy/README.md`
- Test: `brain/tests/test_deploy.py`

**Interfaces:**
- Consumes: CLI `worker`, `worker-health`, `models pull` (Task 8); `deploy/providers.yaml` (Task 2).
- Produces: compose services `worker` (same build, user, data volume and database URL as `api`, plus `LOOPI_OLLAMA_URL`, `LOOPI_PROVIDERS_FILE`, `LOOPI_MODEL_DIR`, `HF_HOME`; healthcheck `loopi-brain worker-health`; depends on `db` healthy and `api` healthy) and `models` (`ollama/ollama:0.35.1`, volume `ollama`, no published port, healthcheck `ollama list`); `api` gets the Ollama URL and the providers file; `smoke.sh` checks that an uploaded test video reaches `analysis_state='done'` with a keyframe and that the worker is healthy, then removes everything the smoke device created and revokes it.

**Do not run `docker compose …` or `deploy/smoke.sh` in this task** (Global Constraints: project `deploy` is the live stack); verify with `bash -n` and `tests/test_deploy.py` only.

Rulings in this task: the Brain `Dockerfile` does not change (the new wheels come from `uv.lock`; ffmpeg is already installed). The smoke test makes its test video with the api image's ffmpeg (the host may have none) and waits `SMOKE_ANALYSIS_TIMEOUT` seconds (default 90) for its analysis. As soon as the smoke device's id is known, an `EXIT` trap guarantees that on **every** exit — passed or failed — it deletes that device's moments and items (keyframes and transcripts cascade), removes its `raw/`, `view/` and `keyframes/` directories under the media root (inside the api container, only for an id that is a valid ULID), then revokes the device; jobs still queued for those rows finish as no-ops (Tasks 4, 6, 7). The worker's healthcheck runs every 30 s; `stop_grace_period: 2m` lets running jobs finish on a stop. The worker sets `OMP_NUM_THREADS=1` and `OPENBLAS_NUM_THREADS=1`: CTranslate2 takes its thread count from `cpu_threads` (it overrides `OMP_NUM_THREADS`), and on this host a turbo-sized check with the tiny model ran faster with the limits (0.37 s vs 0.52 s, half the CPU) because nothing else oversubscribes the cores; re-check on the owner's PC in Task 11. Models are pulled before the full stack starts so the backlog is captioned as soon as it is analysed; without them captions and embeddings only wait.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_deploy.py`:

```python
import subprocess
from pathlib import Path

import yaml

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
COMPOSE = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())
SERVICES = COMPOSE["services"]
PROVIDERS_MOUNT = "./providers.yaml:/etc/loopi/providers.yaml:ro"


def test_worker_runs_the_pipeline_with_the_api_image_and_data():
    api, worker = SERVICES["api"], SERVICES["worker"]
    assert worker["build"] == api["build"] == "../brain"
    assert worker["command"] == ["loopi-brain", "worker"]
    assert worker["user"] == api["user"]
    env = worker["environment"]
    assert env["LOOPI_DATABASE_URL"] == api["environment"]["LOOPI_DATABASE_URL"]
    assert env["LOOPI_MEDIA_ROOT"] == "/srv/loopi/media"
    assert env["LOOPI_OLLAMA_URL"] == "http://models:11434"
    assert env["LOOPI_PROVIDERS_FILE"] == "/etc/loopi/providers.yaml"
    assert env["LOOPI_MODEL_DIR"] == "/srv/loopi/models" and env["HF_HOME"] == "/srv/loopi/models/hf"
    assert worker["volumes"] == ["${LOOPI_DATA_DIR:-/srv/loopi}:/srv/loopi", PROVIDERS_MOUNT]
    assert worker["healthcheck"]["test"] == ["CMD", "loopi-brain", "worker-health"]
    assert worker["healthcheck"]["interval"] == "30s" and worker["stop_grace_period"] == "2m"
    assert env["OMP_NUM_THREADS"] == env["OPENBLAS_NUM_THREADS"] == "1"
    assert worker["depends_on"] == {"db": {"condition": "service_healthy"}, "api": {"condition": "service_healthy"}}
    assert "ports" not in worker


def test_api_reads_the_same_providers_file_and_model_server():
    api = SERVICES["api"]
    assert api["environment"]["LOOPI_OLLAMA_URL"] == "http://models:11434"
    assert api["environment"]["LOOPI_PROVIDERS_FILE"] == "/etc/loopi/providers.yaml"
    assert PROVIDERS_MOUNT in api["volumes"]


def test_models_service_is_a_pinned_private_ollama():
    models = SERVICES["models"]
    assert models["image"] == "ollama/ollama:0.35.1"
    assert models["environment"] == {"OLLAMA_KEEP_ALIVE": "30m", "OLLAMA_MAX_LOADED_MODELS": "2",
                                     "OLLAMA_NUM_PARALLEL": "1"}
    assert models["volumes"] == ["ollama:/root/.ollama"] and "ollama" in COMPOSE["volumes"]
    assert models["healthcheck"]["test"] == ["CMD", "ollama", "list"]
    assert "ports" not in models  # reachable only inside the compose network


def test_every_image_is_pinned():
    for name, service in SERVICES.items():
        if "image" in service:
            tag = service["image"].rsplit(":", 1)
            assert len(tag) == 2 and tag[1] not in {"latest", ""}, name


def test_smoke_script_is_valid_bash():
    subprocess.run(["bash", "-n", str(DEPLOY / "smoke.sh")], check=True)


def test_smoke_script_removes_what_it_created_on_every_exit():
    script = (DEPLOY / "smoke.sh").read_text()
    delete = "DELETE FROM moment WHERE device_id = '$DEVICE_ID'; DELETE FROM item WHERE device_id = '$DEVICE_ID';"
    check = '[[ "$DEVICE_ID" =~ ^[0-9A-HJKMNP-TV-Z]{26}$ ]] || return 0'
    remove = 'rm -rf "/srv/loopi/media/raw/$DEVICE_ID" "/srv/loopi/media/view/$DEVICE_ID"'
    assert delete in script and remove in script and '"/srv/loopi/media/keyframes/$DEVICE_ID"' in script
    # The id is validated before anything is deleted, and the device is revoked last.
    assert script.index(check) < script.index(delete) < script.index(remove) < script.index("SET revoked = TRUE")
    # The exit trap is armed before the device exists, and the device id is known before the first upload.
    first_upload = script.index('expect 201 "${MTLS[@]}"')
    assert script.index("trap on_exit EXIT") < script.index("/v1/pair") < script.index("DEVICE_ID=\"$(") < first_upload
    assert 'SMOKE_ANALYSIS_TIMEOUT="${SMOKE_ANALYSIS_TIMEOUT:-90}"' in script
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_deploy.py -v`
Expected: FAIL — `KeyError: 'worker'` in the new service tests (`test_every_image_is_pinned` and `test_smoke_script_is_valid_bash` already pass on the P1 files).

- [ ] **Step 3: Add the services**

Replace `deploy/docker-compose.yml` with:

```yaml
services:
  db:
    image: pgvector/pgvector:0.8.1-pg17
    environment:
      POSTGRES_USER: loopi
      POSTGRES_PASSWORD: ${LOOPI_DB_PASSWORD:?set LOOPI_DB_PASSWORD in deploy/.env}
      POSTGRES_DB: loopi
    volumes:
      - db:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U loopi"]
      interval: 5s
      retries: 20
    restart: unless-stopped

  api:
    build: ../brain
    user: "${LOOPI_UID:-1000}:${LOOPI_GID:-1000}"
    environment:
      LOOPI_DATABASE_URL: postgresql://loopi:${LOOPI_DB_PASSWORD}@db:5432/loopi
      LOOPI_MEDIA_ROOT: /srv/loopi/media
      LOOPI_PKI_DIR: /srv/loopi/pki
      LOOPI_TZ: ${LOOPI_TZ:-Asia/Seoul}
      LOOPI_OLLAMA_URL: http://models:11434
      LOOPI_PROVIDERS_FILE: /etc/loopi/providers.yaml
    volumes:
      - ${LOOPI_DATA_DIR:-/srv/loopi}:/srv/loopi
      - ./providers.yaml:/etc/loopi/providers.yaml:ro
    healthcheck:
      # Passes only once `serve` has applied migrations and uvicorn is listening.
      test: ["CMD", "python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
      interval: 5s
      timeout: 5s
      retries: 20
      start_period: 10s
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped

  worker:
    build: ../brain
    command: ["loopi-brain", "worker"]
    user: "${LOOPI_UID:-1000}:${LOOPI_GID:-1000}"
    environment:
      LOOPI_DATABASE_URL: postgresql://loopi:${LOOPI_DB_PASSWORD}@db:5432/loopi
      LOOPI_MEDIA_ROOT: /srv/loopi/media
      LOOPI_PKI_DIR: /srv/loopi/pki
      LOOPI_TZ: ${LOOPI_TZ:-Asia/Seoul}
      LOOPI_OLLAMA_URL: http://models:11434
      LOOPI_PROVIDERS_FILE: /etc/loopi/providers.yaml
      LOOPI_MODEL_DIR: /srv/loopi/models
      # The container user has no home directory; Hugging Face caches go next to the models.
      HF_HOME: /srv/loopi/models/hf
      # Whisper sets its own thread count (LOOPI_WHISPER_CPU_THREADS); keep every other library
      # single-threaded so nothing oversubscribes the CPU (measured: CTranslate2 is not slower).
      OMP_NUM_THREADS: "1"
      OPENBLAS_NUM_THREADS: "1"
    volumes:
      - ${LOOPI_DATA_DIR:-/srv/loopi}:/srv/loopi
      - ./providers.yaml:/etc/loopi/providers.yaml:ro
    healthcheck:
      # A Procrastinate worker heartbeat younger than 60 s (a light command: psycopg only).
      test: ["CMD", "loopi-brain", "worker-health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 30s
    # On `docker compose stop/up`, running jobs (a Whisper run takes < 1 min) finish before the kill.
    stop_grace_period: 2m
    depends_on:
      db:
        condition: service_healthy
      api:
        condition: service_healthy  # api applies the migrations first
    restart: unless-stopped

  models:
    image: ollama/ollama:0.35.1
    environment:
      OLLAMA_KEEP_ALIVE: 30m
      OLLAMA_MAX_LOADED_MODELS: "2"
      OLLAMA_NUM_PARALLEL: "1"
    volumes:
      - ollama:/root/.ollama
    healthcheck:
      test: ["CMD", "ollama", "list"]
      interval: 15s
      timeout: 10s
      retries: 5
      start_period: 20s
    restart: unless-stopped

  proxy:
    image: caddy:2.10.2
    ports:
      - "8443:8443"
    environment:
      LOOPI_WEB_USER: ${LOOPI_WEB_USER:?set LOOPI_WEB_USER}
      LOOPI_WEB_PASSWORD_HASH: ${LOOPI_WEB_PASSWORD_HASH:?set LOOPI_WEB_PASSWORD_HASH}
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - ${LOOPI_DATA_DIR:-/srv/loopi}/pki/server.crt:/pki/server.crt:ro
      - ${LOOPI_DATA_DIR:-/srv/loopi}/pki/server.key:/pki/server.key:ro
      - ${LOOPI_DATA_DIR:-/srv/loopi}/pki/ca.crt:/pki/ca.crt:ro
      - caddy_data:/data
    depends_on:
      api:
        condition: service_healthy
    restart: unless-stopped

volumes:
  db: {}
  caddy_data: {}
  ollama: {}
```

- [ ] **Step 4: Extend the smoke test**

Replace `deploy/smoke.sh` with (P1 checks unchanged; new: `SMOKE_ANALYSIS_TIMEOUT`, the cleanup functions and `EXIT` trap, the device-id lookup after pairing, `new_ulid()`, the pipeline block and the final cleanup):

```bash
#!/usr/bin/env bash
# End-to-end check of a running brain: pairing, mTLS upload, duplicate, spoofing, heartbeat, web auth,
# the same stack reached through an IP-literal URL (no TLS SNI), which is how devices are configured,
# and the pipeline worker turning an uploaded test video into keyframes. It removes everything it created.
set -euo pipefail
cd "$(dirname "$0")"

if [[ -z "${LOOPI_DATA_DIR:-}" && -f .env ]]; then
  LOOPI_DATA_DIR="$(sed -n 's/^LOOPI_DATA_DIR=//p' .env | tail -n1 | tr -d "'\"\r")"
fi
BASE="${LOOPI_SMOKE_URL:-https://localhost:8443}"
PKI="${LOOPI_DATA_DIR:-/srv/loopi}/pki"
# IP-literal URLs send no TLS SNI (RFC 6066); devices and browsers use https://<LAN IP>:8443, so smoke checks that too.
# LOOPI_SMOKE_IP must be an IPv4 address listed in the server cert (README step 3); default: the first LAN address.
if [[ -z "${LOOPI_SMOKE_IP:-}" ]]; then
  LOOPI_SMOKE_IP="$(hostname -I 2>/dev/null | cut -d' ' -f1 || true)"
  [[ -n "$LOOPI_SMOKE_IP" ]] || LOOPI_SMOKE_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"  # macOS
fi
if ! [[ "$LOOPI_SMOKE_IP" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
  echo "FAIL: LOOPI_SMOKE_IP must be an IPv4 address (got '${LOOPI_SMOKE_IP}'); set it to the PC's LAN IP" >&2
  exit 1
fi
IP_BASE="https://$LOOPI_SMOKE_IP:8443"
# How long the worker may take to analyse the test video; behind a backlog it needs longer (README step 6).
SMOKE_ANALYSIS_TIMEOUT="${SMOKE_ANALYSIS_TIMEOUT:-90}"
if ! [[ "$SMOKE_ANALYSIS_TIMEOUT" =~ ^[0-9]+$ ]]; then
  echo "FAIL: SMOKE_ANALYSIS_TIMEOUT must be a number of seconds (got '$SMOKE_ANALYSIS_TIMEOUT')" >&2
  exit 1
fi
WORK="$(mktemp -d)"
DEVICE_ID=""  # set once the smoke device exists; from then on every exit removes what it created

remove_smoke_data() {  # delete the smoke device's rows and media files, then revoke it
  [[ "$DEVICE_ID" =~ ^[0-9A-HJKMNP-TV-Z]{26}$ ]] || return 0  # never delete anything for a malformed id
  # Moments and items of that device only (keyframes and transcripts go with their items).
  docker compose exec -T db psql -U loopi -d loopi -v ON_ERROR_STOP=1 -q -c \
    "DELETE FROM moment WHERE device_id = '$DEVICE_ID'; DELETE FROM item WHERE device_id = '$DEVICE_ID';" >/dev/null &&
  # Paths inside the api container (LOOPI_MEDIA_ROOT).
  docker compose exec -T api rm -rf "/srv/loopi/media/raw/$DEVICE_ID" "/srv/loopi/media/view/$DEVICE_ID" \
    "/srv/loopi/media/keyframes/$DEVICE_ID" &&
  docker compose exec -T db psql -U loopi -d loopi -v ON_ERROR_STOP=1 -q -c \
    "UPDATE device SET revoked = TRUE WHERE id = '$DEVICE_ID'" >/dev/null
}

on_exit() {
  local status=$?
  if [[ -n "$DEVICE_ID" ]]; then
    remove_smoke_data || echo "WARN: test data of smoke device $DEVICE_ID was not fully removed" >&2
  fi
  rm -rf "$WORK"
  exit "$status"
}
trap on_exit EXIT

sha256_hex() {  # reads stdin, prints lowercase hex sha256 (Linux and macOS)
  if command -v sha256sum >/dev/null; then sha256sum | cut -d' ' -f1; else shasum -a 256 | cut -d' ' -f1; fi
}

expect() {  # expect <http-code> <curl args...>
  local want="$1"; shift
  local got
  got="$(curl -s -o "$WORK/body" -w '%{http_code}' --cacert "$PKI/ca.crt" "$@")"
  if [[ "$got" != "$want" ]]; then
    echo "FAIL: expected $want, got $got for: $*" >&2
    cat "$WORK/body" >&2
    exit 1
  fi
}

# Wait (max 60 s) until Caddy reaches a migrated api: an unauthenticated device call answers 401, not 502/refused.
for _ in $(seq 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --cacert "$PKI/ca.crt" "$BASE/v1/heartbeat" || true)"
  [[ "$code" == "401" || "$code" == "405" ]] && break
  sleep 2
done
if [[ "$code" != "401" && "$code" != "405" ]]; then echo "FAIL: stack not ready (last HTTP code $code)" >&2; exit 1; fi

# No-SNI checks first, before any device exists: the server cert must list the IP, then both route families
# (device API without a client cert -> 401 from the api, web UI -> basic-auth challenge) must answer through it.
if ! openssl x509 -in "$PKI/server.crt" -noout -ext subjectAltName | grep -qF "IP Address:$LOOPI_SMOKE_IP"; then
  echo "FAIL: $PKI/server.crt has no IP SAN $LOOPI_SMOKE_IP (re-run init-pki with --ip, or set LOOPI_SMOKE_IP)" >&2
  exit 1
fi
expect 401 -X POST -H 'Content-Type: application/json' --data '{}' "$IP_BASE/v1/heartbeat"
expect 401 "$IP_BASE/"

TOKEN="$(docker compose exec -T api loopi-brain pair-token | head -n1 | tr -d '\r')"
openssl ecparam -name prime256v1 -genkey -noout -out "$WORK/device.key"
openssl req -new -key "$WORK/device.key" -subj "/CN=smoke" -out "$WORK/device.csr"
python3 - "$TOKEN" "$WORK/device.csr" > "$WORK/pair.json" <<'PY'
import json, sys
print(json.dumps({"token": sys.argv[1], "csr_pem": open(sys.argv[2]).read(),
                  "hw_info": {"model": "smoke", "serial": "0", "agent_version": "0.1.0"}}))
PY
expect 201 -H 'Content-Type: application/json' --data @"$WORK/pair.json" "$BASE/v1/pair"
python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["cert_pem"], end="")' "$WORK/body" > "$WORK/device.crt"
FP="$(openssl x509 -in "$WORK/device.crt" -outform DER | sha256_hex)"
MTLS=(--cert "$WORK/device.crt" --key "$WORK/device.key")
DEVICE_ID="$(docker compose exec -T db psql -U loopi -d loopi -tAc \
  "SELECT id FROM device WHERE cert_fingerprint = '$FP'" | head -n1 | tr -d '\r')"
[[ "$DEVICE_ID" =~ ^[0-9A-HJKMNP-TV-Z]{26}$ ]] || { echo "FAIL: smoke device not found (got '$DEVICE_ID')" >&2; exit 1; }

new_ulid() {
  python3 -c 'import secrets,time
A="0123456789ABCDEFGHJKMNPQRSTVWXYZ"; t=int(time.time()*1000)
print("".join(A[(t>>(5*i))&31] for i in range(9,-1,-1))+"".join(secrets.choice(A) for _ in range(16)))'
}
ID="$(new_ulid)"
printf '{"note":"smoke"}' > "$WORK/marker.json"
SHA="$(sha256_hex < "$WORK/marker.json")"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ITEM=(-X PUT -H "Content-Type: application/json" -H "X-Loopi-Kind: marker"
      -H "X-Loopi-Start: $NOW" -H "X-Loopi-End: $NOW" -H "X-Loopi-Sha256: $SHA"
      --data-binary @"$WORK/marker.json")

expect 201 "${MTLS[@]}" "${ITEM[@]}" "$BASE/v1/items/$ID"
expect 200 "${MTLS[@]}" "${ITEM[@]}" "$BASE/v1/items/$ID"
# Spoof: no client cert, forged fingerprint of the real device -> must be rejected.
expect 401 -H "X-Loopi-Client-Fingerprint: $FP" "${ITEM[@]}" "$BASE/v1/items/$ID"

HB='{"agent_version":"0.1.0","uptime_s":1,"disk_used_pct":1.0,"queue_depth":0,"clock_synced":true,"paused":false,"audio_enabled":true,"recording_video":false,"recording_audio":false,"acked_command_ids":[]}'
expect 200 "${MTLS[@]}" -H 'Content-Type: application/json' --data "$HB" "$BASE/v1/heartbeat"
expect 401 "$BASE/"
# Same device through the IP-literal URL: the client-cert fingerprint must reach the api without SNI too.
expect 200 "${MTLS[@]}" -H 'Content-Type: application/json' --data "$HB" "$IP_BASE/v1/heartbeat"
# Pipeline: a 5 s test video (made by the api image's ffmpeg) must be analysed by the worker within
# SMOKE_ANALYSIS_TIMEOUT seconds. Keyframe extraction needs no AI model.
docker compose exec -T api ffmpeg -nostdin -loglevel error -f lavfi -i testsrc2=size=320x240:rate=2 \
  -frames:v 10 -c:v libx264 -g 4 -bsf:v h264_mp4toannexb -f h264 - > "$WORK/video.h264"
VID="$(new_ulid)"
VSHA="$(sha256_hex < "$WORK/video.h264")"
expect 201 "${MTLS[@]}" -X PUT -H "Content-Type: video/h264" -H "X-Loopi-Kind: video" -H "X-Loopi-Video-Fps: 2" \
  -H "X-Loopi-Start: $NOW" -H "X-Loopi-End: $NOW" -H "X-Loopi-Sha256: $VSHA" \
  --data-binary @"$WORK/video.h264" "$BASE/v1/items/$VID"
STATE=""
DEADLINE=$((SECONDS + SMOKE_ANALYSIS_TIMEOUT))
while (( SECONDS < DEADLINE )); do
  STATE="$(docker compose exec -T db psql -U loopi -d loopi -tAc \
    "SELECT analysis_state FROM item WHERE id = '$VID'" | tr -d '\r')"
  [[ "$STATE" == "done" ]] && break
  sleep 2
done
if [[ "$STATE" != "done" ]]; then
  echo "FAIL: test video not analysed within $SMOKE_ANALYSIS_TIMEOUT s (state '$STATE');" \
    "a backlog delays it (raise SMOKE_ANALYSIS_TIMEOUT); see: docker compose logs worker" >&2
  exit 1
fi
KEYFRAMES="$(docker compose exec -T db psql -U loopi -d loopi -tAc \
  "SELECT count(*) FROM keyframe WHERE item_id = '$VID'" | tr -d '\r')"
[[ "$KEYFRAMES" -ge 1 ]] || { echo "FAIL: test video produced no keyframe" >&2; exit 1; }
docker compose exec -T worker loopi-brain worker-health >/dev/null \
  || { echo "FAIL: the worker sent no heartbeat in the last 60 s" >&2; exit 1; }
# Leave nothing behind (the exit trap does the same after a failure), then check the revocation.
remove_smoke_data
REMOVED="$DEVICE_ID"
DEVICE_ID=""
expect 401 "${MTLS[@]}" -H 'Content-Type: application/json' --data "$HB" "$BASE/v1/heartbeat"
echo "Removed the smoke data and revoked smoke device $REMOVED"
echo "OK: smoke test passed (device fingerprint $FP)"
```

Run (from the repo root): `bash -n deploy/smoke.sh`
Expected: no output.

- [ ] **Step 5: Update the README**

Replace `deploy/README.md` with:

```markdown
# Loopi brain — deployment

Requirements: Docker with Compose v2 on the Linux PC, port 8443 reachable on the LAN.

1. `cp deploy/.env.example deploy/.env` and fill in the values (password hash command is in the file).
   Generate `LOOPI_DB_PASSWORD` with `openssl rand -hex 24` (hex only: it is embedded unencoded in the database URL, so `/`, `@` or `:` would break it).
   Set `LOOPI_UID` / `LOOPI_GID` to the output of `id -u` / `id -g`: the api container runs as that user, so files in the data dir belong to you.
2. Create the data dir: `sudo mkdir -p /srv/loopi && sudo chown "$USER" /srv/loopi`.
3. Create the CA and server cert (use the PC's LAN IP; keep `localhost` for the smoke test):
   `cd deploy && docker compose run --rm --no-deps api loopi-brain init-pki --host localhost --host brain.lan --ip "$(hostname -I | cut -d' ' -f1)"`
   (The `hostname -I` detection is Linux-only; on macOS use `--ip "$(ipconfig getifaddr en0)"`.)
   Write down the printed CA fingerprint.
4. Download the AI models before the pipeline starts (≈ 11.5 GB, once): `minicpm-v4.6:1b` 1.6 GB, `qwen3:8b` 5.2 GB,
   `qwen3:4b` 2.5 GB and `qwen3-embedding:0.6b` 0.6 GB go into the `ollama` volume, Whisper large-v3-turbo
   (≈ 1.6 GB) into `$LOOPI_DATA_DIR/models/whisper`. `deploy/providers.yaml` (committed, no secrets) names them.
   `docker compose up -d models && docker compose run --rm --no-deps worker loopi-brain models pull`
   (Without them the worker still extracts keyframes and the first transcription downloads Whisper itself;
   captions and embeddings simply wait, and the "Xử lý" page lists the missing models.)
5. `docker compose up -d --build` — starts `db`, `api`, `worker` (pipeline jobs), `models` (Ollama) and `proxy`.
6. `./smoke.sh` (waits up to 60 s for the stack to become ready) → must print `OK: smoke test passed`.
   (`LOOPI_DATA_DIR` is read from `deploy/.env` if not exported; falls back to `/srv/loopi`.)
   It also checks the stack through an IP-literal URL (`https://<LAN IP>:8443`, no TLS SNI), the way devices and browsers
   connect. The IP defaults to the first `hostname -I` address, as in step 3; set `LOOPI_SMOKE_IP` if the cert holds another one.
   It also uploads a 5 s test video and waits for the worker to extract its keyframe (90 s; behind a backlog of
   older recordings set e.g. `SMOKE_ANALYSIS_TIMEOUT=600`). On every exit, passed or failed, it deletes what the
   smoke device created (rows and media files) and revokes that device.
7. Browser: import `/srv/loopi/pki/ca.crt` into the OS trust store, then open `https://<LAN IP>:8443/`
   and log in with `LOOPI_WEB_USER`.
8. Devices page → "Tạo token ghép cặp" → copy `pair_token` and `ca_fingerprint` into the device's `loopi.toml`.

## Pipeline (P2a)

- The "Xử lý" page (`/pipeline`) shows the backlog, the recording → caption/transcript latency of the last 24 h,
  whether Ollama is reachable and has every model, and the jobs that failed 4 times, each with a "Xử lý lại" button
  (the embedding run has none: it restarts by itself every minute).
- Upgrading from P1: the migration marks every existing recording as pending, so the worker analyses the
  recordings made before the upgrade too; on a CPU-only PC that backlog drains over hours.
- Model server down: captions and embeddings wait and resume by themselves; uploads are never affected.
- `docker compose logs -f worker` follows the pipeline; `docker compose exec worker loopi-brain worker-health`
  checks its heartbeat.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_deploy.py -v`
Expected: PASS (6 tests).

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: all pass (≈ 284 passed, 1 skipped), coverage ≥ 80 %.

- [ ] **Step 7: Commit**

```bash
git add deploy/docker-compose.yml deploy/smoke.sh deploy/README.md brain/tests/test_deploy.py
git commit -F - <<'EOF'
feat(deploy): worker and Ollama services; smoke test checks the pipeline

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Pre-flight rulings for this task (controller, 2026-10-15; they amend the steps above):**
- **R-F15:** between the pairing call and the device-id lookup the `EXIT` trap cannot clean up yet; in the lookup's failure branch (and wherever the script exits before `DEVICE_ID` is set but after `FP` is known), revoke the smoke device by fingerprint (`UPDATE device SET revoked = TRUE WHERE cert_fingerprint = '$FP'`), so a failed smoke run never leaves an active smoke device. `bash -n` still passes; `test_deploy.py` counts do not change.

---

### Task 11: Real-data run and measurements (controller-executed)

This task replaces the live P1 stack with P2a on the owner's PC and processes the owner's real recordings. It is executed by the controller, not by a subagent, and only after the owner confirms that the P1c hardware soak is over. **Stop and ask the owner** before Step 3 (it migrates the live database) and before committing any transcript text.

**Files:**
- Create: `docs/measurements/2026-10-p2a-pipeline.md`
- Modify (only if tuning is needed): `brain/src/loopi_brain/config.py` keyframe defaults and `brain/tests/test_config.py`
- Modify: `docs/HANDOFF.md` (§1 status row for P2a, where the live stack runs from); create or update `docs/superpowers/ledgers/p2a-progress.md` (the controller commits a first copy after the Tasks 1–10 final review)
- Possibly: a merge commit of `feat/phase1` into `feat/phase2` (Step 1)

**Interfaces:**
- Consumes: the whole branch (Tasks 1–10), the live compose project `deploy` (volumes `deploy_db`, `deploy_caddy_data`; data dir from `/home/soul/loopi/deploy/.env`).
- Produces: the P2a stack running as compose project `deploy`, a measurement report, possibly tuned keyframe defaults.

Facts: `/home/soul/loopi/deploy` and `/home/soul/loopi-p2/deploy` both default to compose project name `deploy`, so starting P2a from the worktree recreates the live containers and keeps the live volumes (the owner's data). Every compose command below names the project explicitly (`-p deploy`). From then on the live stack builds from, and bind-mounts `Caddyfile` and `providers.yaml` from, the worktree: the worktree must not be removed before the stack is redeployed from `/home/soul/loopi` (Step 9). Never pass `-v` to `down`, never prune. Rollback = `docker compose -p deploy up -d --build --remove-orphans` from `/home/soul/loopi/deploy`: it rebuilds P1 and removes only the `worker` and `models` containers this task created (volumes stay); P1 ignores the new tables and columns.

- [ ] **Step 1: Preconditions**

Ask the owner: "Soak P1c đã xong chưa? Mình sẽ dừng stack P1 và chạy P2a trên dữ liệu thật." Continue only on yes.

Make sure deploying from the worktree cannot revert anything the live checkout runs (e.g. P1c fixes made during the soak):

```bash
git -C /home/soul/loopi status --short               # must print nothing: no uncommitted live changes
git -C /home/soul/loopi rev-parse --abbrev-ref HEAD  # expected: feat/phase1
LIVE="$(git -C /home/soul/loopi rev-parse HEAD)"
cd /home/soul/loopi-p2 && git merge-base --is-ancestor "$LIVE" HEAD && echo "live code is already in feat/phase2"
```

If the live checkout has uncommitted changes or is on another branch, stop and ask the owner. If the last command printed nothing (the live commit is not an ancestor), merge `feat/phase1` and test before committing:

```bash
cd /home/soul/loopi-p2 && git merge --no-ff --no-commit feat/phase1
cd brain && uv run pytest -q          # all pass; resolve any conflict test-first and record it in the ledger
cd .. && git commit -F - <<'EOF'
chore: merge feat/phase1 (P1c fixes) into feat/phase2 before the P2a deploy

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
git merge-base --is-ancestor "$LIVE" HEAD && echo "live code is now in feat/phase2"
```

Run: `cd /home/soul/loopi-p2/brain && uv run pytest -q`
Expected: all pass on the branch head.

- [ ] **Step 2: Back up the live database and copy the env file (never print it)**

```bash
mkdir -p ~/loopi-backups
cd /home/soul/loopi/deploy && docker compose -p deploy exec -T db pg_dump -U loopi -Fc loopi > ~/loopi-backups/p1-$(date +%F-%H%M).dump
ls -l ~/loopi-backups/
install -m 600 /home/soul/loopi/deploy/.env /home/soul/loopi-p2/deploy/.env
cd /home/soul/loopi-p2 && git status --short deploy/.env   # must print nothing (.env is git-ignored)
```

Expected: a non-empty `.dump` file; `git status` silent.

Restore path, only if the migrated database itself must be thrown away (rolling back to P1 does **not** need it, because P1 ignores the new tables and columns): stop `api` and `worker`, then `docker compose -p deploy exec -T db pg_restore -U loopi -d loopi --clean --if-exists < ~/loopi-backups/<file>.dump`.

- [ ] **Step 3: Pull the models, then start P2a**

```bash
cd /home/soul/loopi-p2/deploy
docker compose -p deploy up -d models
docker compose -p deploy run --rm --no-deps worker loopi-brain models pull     # ≈ 11.5 GB, once
docker compose -p deploy up -d --build
docker compose -p deploy ps        # db, api, worker, models, proxy: running / healthy
COMPOSE_PROJECT_NAME=deploy SMOKE_ANALYSIS_TIMEOUT=600 ./smoke.sh   # → OK: smoke test passed (removes its own data)
```

The backlog of P1 recordings starts at the same time, so the smoke video can wait behind up to ~20 queued video jobs: hence 600 s. If `smoke.sh` fails on the pipeline block: `docker compose -p deploy logs --tail 100 worker` (its exit trap has already removed the smoke data). Record any fix as a ruling in the ledger.

- [ ] **Step 4: Check the first captions and transcripts**

After ~10 minutes:

```bash
docker compose -p deploy exec -T db psql -U loopi -d loopi -c "
SELECT kind, analysis_state, count(*) FROM item GROUP BY 1, 2 ORDER BY 1, 2;
SELECT count(*) FILTER (WHERE caption IS NOT NULL) AS captioned, count(*) FILTER (WHERE caption_error IS NOT NULL) AS with_error FROM keyframe;
SELECT left(caption_error, 200) FROM keyframe WHERE caption_error IS NOT NULL LIMIT 5;
SELECT task_name, status, count(*) FROM procrastinate_jobs GROUP BY 1, 2 ORDER BY 1, 2;"
```

Expected: `captioned` grows; no `failed` jobs. If every caption fails with an Ollama error about `think` or `format`, stop and report to the owner (a provider ruling would change).

Every P1 smoke run left a revoked "smoke" device and a `{"note":"smoke"}` marker, which now become ★ "smoke" moments on the owner's timeline. List them:

```bash
docker compose -p deploy exec -T db psql -U loopi -d loopi -c "
SELECT d.id, d.created_at, d.revoked, count(i.id) AS items FROM device d LEFT JOIN item i ON i.device_id = d.id
 WHERE d.hw_info->>'model' = 'smoke' GROUP BY d.id ORDER BY d.created_at;"
```

Ask the owner: "Có N thiết bị 'smoke' cũ từ P1 với M mục thử; xoá dữ liệu thử đó khỏi dòng thời gian không?" Only on yes, for each listed id (a 26-character ULID): `docker compose -p deploy exec -T db psql -U loopi -d loopi -c "DELETE FROM moment WHERE device_id = '<id>'; DELETE FROM item WHERE device_id = '<id>';"` and `docker compose -p deploy exec -T api rm -rf /srv/loopi/media/raw/<id> /srv/loopi/media/view/<id> /srv/loopi/media/keyframes/<id>`.

- [ ] **Step 5: Let the backlog drain, sampling CPU and memory**

The backlog is every P1 recording (now `pending`). Open `https://<LAN IP>:8443/pipeline` to follow it. When the pending counts reach 0, start a 24 h measurement window of live data:

```bash
cd /home/soul/loopi-p2/deploy
for s in worker models; do
  echo "$s $(date -u +%FT%TZ) $(docker compose -p deploy exec -T $s cat /sys/fs/cgroup/cpu.stat | grep usage_usec) \
started=$(docker inspect -f '{{.State.StartedAt}}' deploy-$s-1)"
done | tee -a ~/loopi-backups/p2a-cpu-samples.txt
```

Run the same loop again 24 h later. CPU seconds/day = Δ`usage_usec` / 10⁶ per service (discard the window if `started=` changed: a restart resets the counters). Memory peak since start:

```bash
for s in worker models; do echo "$s $(docker compose -p deploy exec -T $s cat /sys/fs/cgroup/memory.peak)"; done
```

- [ ] **Step 6: Measure**

```bash
docker compose -p deploy exec -T db psql -U loopi -d loopi <<'SQL'
-- Keyframes per local day, next to the hours of video recorded that day (budget: 100–300/day).
SELECT d.day, d.video_hours, coalesce(k.keyframes, 0) AS keyframes FROM
  (SELECT (started_at AT TIME ZONE 'Asia/Seoul')::date AS day,
          round(sum(extract(epoch FROM ended_at - started_at)) / 3600.0, 1) AS video_hours
     FROM item WHERE kind = 'video' GROUP BY 1) d
  LEFT JOIN (SELECT (ts AT TIME ZONE 'Asia/Seoul')::date AS day, count(*) AS keyframes FROM keyframe GROUP BY 1) k
  USING (day) ORDER BY day;
-- Caption seconds per keyframe (real caption runs only).
SELECT count(*) AS runs, round(avg(secs)::numeric, 1) AS avg_s,
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY secs))::numeric, 1) AS p95_s
  FROM (SELECT extract(epoch FROM s.at - st.at)::float8 AS secs
          FROM keyframe kf JOIN procrastinate_jobs j ON j.id = kf.caption_job_id
          JOIN LATERAL (SELECT max(at) AS at FROM procrastinate_events WHERE job_id = j.id AND type = 'started') st ON true
          JOIN LATERAL (SELECT max(at) AS at FROM procrastinate_events WHERE job_id = j.id AND type = 'succeeded') s ON true
         WHERE kf.caption IS NOT NULL AND s.at IS NOT NULL) t;
-- ASR real-time factor (transcription seconds / audio seconds) over every transcribed item.
SELECT count(*) AS items,
       round((sum(extract(epoch FROM s.at - st.at)) / nullif(sum(extract(epoch FROM i.ended_at - i.started_at)), 0))::numeric, 3) AS rtf
  FROM item i JOIN procrastinate_jobs j ON j.id = i.analysis_job_id
  JOIN LATERAL (SELECT max(at) AS at FROM procrastinate_events WHERE job_id = j.id AND type = 'started') st ON true
  JOIN LATERAL (SELECT max(at) AS at FROM procrastinate_events WHERE job_id = j.id AND type = 'succeeded') s ON true
 WHERE i.kind = 'audio' AND i.analysis_state = 'done' AND s.at IS NOT NULL;
-- Latency of live data (uploaded within 5 min of recording) recorded 08:00–22:00 Seoul time (target: avg ≤ 15 min).
SELECT 'video→caption' AS path, count(*), round((avg(lat) / 60)::numeric, 1) AS avg_min,
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY lat) / 60)::numeric, 1) AS p95_min
  FROM (SELECT extract(epoch FROM k.captioned_at - i.ended_at)::float8 AS lat FROM keyframe k JOIN item i ON i.id = k.item_id
         WHERE k.captioned_at IS NOT NULL AND i.received_at - i.ended_at < interval '5 minutes'
           AND extract(hour FROM i.started_at AT TIME ZONE 'Asia/Seoul') BETWEEN 8 AND 21) t
UNION ALL
SELECT 'audio→transcript', count(*), round((avg(lat) / 60)::numeric, 1),
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY lat) / 60)::numeric, 1)
  FROM (SELECT extract(epoch FROM analyzed_at - ended_at)::float8 AS lat FROM item
         WHERE kind = 'audio' AND analysis_state = 'done' AND received_at - ended_at < interval '5 minutes'
           AND extract(hour FROM started_at AT TIME ZONE 'Asia/Seoul') BETWEEN 8 AND 21) t;
-- Transcript volume per language, and dead letters.
SELECT lang, count(*) AS segments, round(sum(extract(epoch FROM end_ts - start_ts)) / 60.0, 1) AS minutes
  FROM transcript_segment GROUP BY 1 ORDER BY 2 DESC;
SELECT task_name, count(*) FROM procrastinate_jobs WHERE status = 'failed' GROUP BY 1;
SQL
```

Samples to look at (show them to the owner; nothing private goes into the repository):

```bash
docker compose -p deploy exec -T db psql -U loopi -d loopi -c "
SELECT (ts AT TIME ZONE 'Asia/Seoul')::timestamp(0) AS at, caption->>'scene' AS scene, caption->>'activity' AS activity
  FROM keyframe WHERE caption IS NOT NULL ORDER BY random() LIMIT 10;
SELECT (start_ts AT TIME ZONE 'Asia/Seoul')::timestamp(0) AS at, lang, left(text, 120) AS text
  FROM transcript_segment ORDER BY random() LIMIT 10;"
```

- [ ] **Step 7: Tune the keyframe budget if needed**

If keyframes per full day of recording are far outside 100–300, change the defaults in `brain/src/loopi_brain/config.py` (`keyframe_hash_threshold` up/down first, then `keyframe_min_gap_s`) and the same values in `frames.KeyframeParams` (`brain/src/loopi_brain/pipeline/frames.py`), update the matching assertions in `brain/tests/test_config.py`, run `cd brain && uv run pytest -q`, redeploy with `docker compose -p deploy up -d --build worker`, and re-measure a day. Commit:

```bash
git add brain/src/loopi_brain/config.py brain/src/loopi_brain/pipeline/frames.py brain/tests/test_config.py
git commit -F - <<'EOF'
fix(brain): tune keyframe defaults from a day of real recordings

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

- [ ] **Step 8: Write the measurement report**

Create `docs/measurements/2026-10-p2a-pipeline.md` with these sections, filled from Steps 5–7: **Setup** (PC CPU model and core count, RAM, P2a commit, models and versions, measurement window); **Backlog** (items processed, hours to drain); **Keyframes** (per day vs video hours, vs the 100–300 budget, tuning done); **Captions** (avg/p95 seconds per keyframe, dead letters); **ASR** (real-time factor, minutes per language, dead letters); **Latency 08:00–22:00** (video→caption and audio→transcript avg/p95 vs the ≤ 15 min target; P2a has no job priorities by ruling — if the target is missed, record it so P2c adds priorities for live uploads over the backlog); **CPU and memory** (CPU seconds/day of `worker` and `models`, memory peaks, vs spec §9.6: < 1 h CPU/day for captions, 0.5–3 h for ASR); **Samples** (at most a few neutral captions; transcript text only if the owner approves each line); **Open issues** (for P2b/P2c).

Ask the owner: "Mình có thể đưa các câu transcript mẫu này vào báo cáo trong repo không?" Remove any line the owner does not approve.

- [ ] **Step 9: Update the handoff and the ledger, then commit**

In `docs/HANDOFF.md` §1, add a row for P2a (plan file, state "complete", commit range) and a one-line pointer to the measurement report. In `docs/HANDOFF.md` and in `docs/superpowers/ledgers/p2a-progress.md`, add this note:

> The live stack (compose project `deploy`) now runs from the worktree `/home/soul/loopi-p2/deploy`: it builds from there and bind-mounts `Caddyfile` and `providers.yaml` from there. **Before the worktree is ever removed** (e.g. by `finishing-a-development-branch`), redeploy from the main checkout on a branch that contains P2: `cd /home/soul/loopi && git checkout <branch with P2> && cd deploy && docker compose -p deploy up -d --build`, then `COMPOSE_PROJECT_NAME=deploy ./smoke.sh`.

```bash
git add docs/measurements/2026-10-p2a-pipeline.md docs/HANDOFF.md docs/superpowers/ledgers/p2a-progress.md
git commit -F - <<'EOF'
docs: P2a pipeline measurements on real recordings

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

## Deferred minors

From the pre-flight review; each is deliberately left out of P2a.

- **Keyframe history read out of order** (M6): fast-path uploads, the oldest-first backlog and retried old videos can run one device's items out of `started_at` order, so an earlier item never sees a later item's keyframes — at most an occasional near-duplicate keyframe across that boundary. Reason: ordering every device's videos strictly would need priorities or a serial queue. Cost: a few extra keyframes per backlog (Task 11 measures keyframes per day). Record it in the ledger.
- **Orphan JPEGs** (M7): a worker killed after `extract_jpeg` but before the commit leaves JPEGs with no row; the re-run writes new ones. Reason: harmless and rare; P2b's retention sweep can remove files that have no row. Cost: a few hundred KB per crash.
- **Timeline loads full-size JPEGs** (M8): each 224 px thumbnail downloads the 1080p keyframe (~300 KB), tens of MB on a busy day over the LAN. Reason: P2b rebuilds the Day page by chapters and can write thumbnails there. Cost: a slower timeline on a phone until then.
- **Memory headroom and offline model loading** (M14): no `mem_limit`; P2b's `qwen3:8b` with `OLLAMA_MAX_LOADED_MODELS=2` needs ~8 GB more next to the host's other containers; each worker start also asks huggingface.co whether Whisper changed (`HF_HUB_OFFLINE=1` would stop that, but would also break `models pull` in the same container and Whisper's first-use download). Reason: P2a's models fit (~3 GB in Ollama, ~1.5 GB for Whisper) and Task 11 measures the real peak. Cost: P2b must size memory before enabling the 8B models.

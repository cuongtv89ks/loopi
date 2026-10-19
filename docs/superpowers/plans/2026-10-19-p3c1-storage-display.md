# P3c-1: storage and display — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep one copy of each video segment (the MP4), turn retention on by default (7/7 days), give every device
an editable location name, and show that name on the merged timeline (with a camera filter), in search, chat, MCP
and in the text the chapter and recap models read.

**Architecture:** Everything is in the brain (`brain/`). A new periodic job (`pipeline/one_copy.py`) deletes a
segment's raw H.264 once its MP4 exists and its analysis is done, and records the time in a new column. A migration
switches retention on. A new module `places.py` owns the location name (validation, saving, "which devices recorded
this day", the " at <place>" phrase of model text). The timeline routes, search, chat, MCP and the chapter/recap
prompt builders join `device.location` and name the place only when the rows come from two or more devices, so a
brain with one camera looks and prompts exactly as today.

**Tech Stack:** Python 3.13, FastAPI + Jinja + HTMX, psycopg 3 (async), Procrastinate 3.10, Postgres 17
(testcontainers in tests), uv.

**Spec:** `docs/superpowers/specs/2026-10-19-p3c-multicam-design.md` (approved 2026-10-19; D30–D36; this plan is
its P3c-1, §9.1) and the binding spec `docs/superpowers/specs/2026-10-14-loopi-design.md` (D37 on this branch:
BLE presence and WiFi by QR are in the backlog, so the P3c design's mentions of "P4c limits audio on every camera" no
longer apply). Read §1 (D32, D34), §4.1, §4.2, §4.4, §4.5 and §8 of the P3c design before any task.

## Global Constraints

- Brain only. No change to `agent/`, `os/`, `docs/protocol/v1.openapi.yaml` or the device↔brain contract.
- Python 3.13; run tests with `cd brain && uv sync && uv run pytest --cov=loopi_brain` (Docker is needed for
  testcontainers). Brain package coverage stays ≥ 80 % (it is 99 % today).
- TDD for every task: write the test, watch it fail, implement, watch it pass.
- Conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`). Each commit ends with
  `Co-Authored-By: <your model name> <noreply@anthropic.com>` and nothing else.
- Code, comments, commit messages in English. Every text the web shows is Vietnamese.
- New migrations, in this order: `009_one_copy.sql` (Task 1), `010_retention_on.sql` (Task 2),
  `011_device_location.sql` (Task 3). `brain/tests/test_db.py` hard-codes the applied-migration lists in three
  tests (`test_migrate_is_idempotent_and_creates_schema`, `test_p1_rows_become_pending_after_upgrade`,
  `test_concurrent_migrations_apply_each_step_once`): the task that adds a migration adds its name there.
- Every POST from the web needs the `HX-Request: true` header (`Depends(require_htmx)` from `web/routes.py`).
- Media files are deleted only through `media.remove_files` (it refuses paths outside the media root and counts a
  missing file as gone).
- Logs carry counts and ids only, never moment text, captions, transcripts or location names.
- Implementers never run `docker compose`, `deploy/smoke.sh`, or touch `/home/soul/loopi-p2`,
  `/home/soul/loopi-data`, the live database or the device. Task 8 is done by the controller.
- The retention change (D32) and the raw-H.264 deletion delete data on the live brain once deployed. Nothing is
  deployed before the owner says yes in Task 8.

## Review Focus

1. A video row that says `status='ready'` with a `view_path`, but whose MP4 file is missing or empty on disk: its
   raw H.264 must stay (Task 1, `test_a_segment_whose_mp4_is_missing_or_empty_keeps_its_raw_file`).
2. A segment whose analysis never finished (job failed, `analysis_state='pending'`) or whose remux failed
   (`status='error'`): its raw H.264 must stay (Task 1, `test_segments_not_ready_for_one_copy_keep_their_raw_file`).
3. A location name that is blank, longer than 60 characters, carries a control character, or contains HTML: refused
   with a Vietnamese message, or escaped on every page; an unknown or revoked device answers 404 (Task 3).
4. The Day page asked for an unknown device id answers 422; a revoked device's past moments still show, labelled
   with its location (Task 4, `test_an_unknown_device_filter_is_refused` and
   `test_a_revoked_devices_moments_keep_their_label`).
5. A brain where the owner already stored retention days of their own keeps those days when migration 010 turns
   retention on (Task 2, `test_the_migration_turns_retention_on_and_keeps_the_owners_days`).

## File structure

| File | Responsibility | Task |
|---|---|---|
| `brain/src/loopi_brain/migrations/009_one_copy.sql` | `item.h264_deleted_at` + the due index | 1 |
| `brain/src/loopi_brain/pipeline/one_copy.py` | the periodic "drop the raw H.264" job | 1 |
| `brain/src/loopi_brain/pipeline/jobs.py`, `pipeline/status.py` | register the job, its label | 1 |
| `brain/src/loopi_brain/migrations/010_retention_on.sql` | switch retention on in a running brain | 2 |
| `brain/src/loopi_brain/store.py`, `pipeline/retention.py`, `deploy/README.md`, templates | default on, wording | 2 |
| `brain/src/loopi_brain/migrations/011_device_location.sql` | `device.location` | 3 |
| `brain/src/loopi_brain/places.py` | location validation/saving, places of a day, " at <place>" | 3, 4 |
| `brain/src/loopi_brain/devices.py`, `web/routes.py`, `templates/devices.html`, `templates/_location.html` | edit the name | 3 |
| `brain/src/loopi_brain/web/day_routes.py`, `web/routes.py` (hours), `templates/day.html`, `templates/timeline.html`, `templates/_places.html` | labels + filter | 4 |
| `brain/src/loopi_brain/search.py`, `web/search_routes.py`, `templates/_hit.html`, `chat.py`, `mcp/memory.py` | labels in search, chat, MCP | 5 |
| `brain/src/loopi_brain/pipeline/chapters.py`, `pipeline/recap.py` | " at <place>" in model text | 6 |
| `docs/superpowers/specs/2026-10-14-loopi-design.md`, `docs/superpowers/specs/2026-10-19-p3c-multicam-design.md` | spec amendments | 7 |

---

### Task 1: One copy per video segment

Once a video segment has its MP4 (remuxed from the raw H.264 with `-c copy`, `status='ready'`, `view_path` set) and
its analysis is done (`analysis_state IN ('done', 'skipped')`), its raw H.264 is deleted and `item.h264_deleted_at`
records when. Only keyframe extraction (`pipeline/video.py`) reads the raw H.264, and it runs before
`analysis_state` leaves `'pending'`; the vlog, the hours page, forget and retention read or delete the MP4 by
`view_path` and its fixed name (`media.item_files`), and treat a missing raw file as gone. `raw_deleted_at` keeps its
meaning ("the recording expired; nothing plays any more") and is not touched here.

**Files:**
- Create: `brain/src/loopi_brain/migrations/009_one_copy.sql`
- Create: `brain/src/loopi_brain/pipeline/one_copy.py`
- Modify: `brain/src/loopi_brain/pipeline/jobs.py` (constant `DROP_RAW_VIDEO`, `TASK_MODULES`, `AUTO_RERUN_TASKS`)
- Modify: `brain/src/loopi_brain/pipeline/status.py` (`TASK_LABELS`)
- Test: `brain/tests/test_one_copy.py` (create); `brain/tests/test_db.py` (add `009_one_copy.sql` to the three lists)

**Interfaces:**
- Produces: `one_copy.drop_raw_video(pool, settings) -> OneCopyResult` (frozen dataclass `OneCopyResult(dropped: int
  = 0, kept: int = 0)`); periodic task `jobs.DROP_RAW_VIDEO = "drop_raw_video"`, cron `*/10 * * * *`, queue
  `light`, queueing lock `drop_raw_video`; column `item.h264_deleted_at TIMESTAMPTZ` (NULL until the raw H.264 of a
  video segment was deleted because its MP4 is the kept copy).

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_one_copy.py`:

```python
import asyncio
import datetime as dt

import pytest

from helpers import fetch_one, insert_item, make_ctx, paired_device, write_media
from loopi_brain import forget, store
from loopi_brain.pipeline import context, jobs, one_copy, retention, status

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


async def _segment(pool, settings, device_id, *, start=NINE, state="done", status="ready", mp4=b"mp4") -> dict:
    """A video segment with its raw H.264 on disk and, unless ``mp4`` is None, its MP4 view; analysis in ``state``."""
    item = await insert_item(pool, device_id, kind="video", started_at=start, status=status)
    raw = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item,)))["raw_path"]
    write_media(settings, raw, b"h264")
    view = raw.replace("raw/", "view/", 1).replace(".h264", ".mp4")
    if mp4 is not None:
        write_media(settings, view, mp4)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE item SET analysis_state = %s, view_path = %s WHERE id = %s",
            (state, view if status == "ready" else None, item),
        )
    return {"id": item, "raw": raw, "view": view}


def _on_disk(settings, rel: str) -> bool:
    return (settings.media_root / rel).exists()


async def test_an_analysed_segment_with_its_mp4_loses_its_raw_h264(pool, pki_settings, dev):
    seg = await _segment(pool, pki_settings, dev["device_id"])
    assert await one_copy.drop_raw_video(pool, pki_settings) == one_copy.OneCopyResult(dropped=1, kept=0)
    assert not _on_disk(pki_settings, seg["raw"]) and _on_disk(pki_settings, seg["view"])
    row = await fetch_one(pool, "SELECT h264_deleted_at, raw_deleted_at, view_path, status FROM item WHERE id = %s",
                          (seg["id"],))
    assert row["h264_deleted_at"] is not None and row["raw_deleted_at"] is None
    assert (row["view_path"], row["status"]) == (seg["view"], "ready")
    assert await one_copy.drop_raw_video(pool, pki_settings) == one_copy.OneCopyResult()  # nothing left to do


async def test_a_skipped_analysis_counts_as_done(pool, pki_settings, dev):
    seg = await _segment(pool, pki_settings, dev["device_id"], state="skipped")
    assert (await one_copy.drop_raw_video(pool, pki_settings)).dropped == 1
    assert not _on_disk(pki_settings, seg["raw"])


@pytest.mark.parametrize(("state", "status"), [("pending", "ready"), ("done", "stored"), ("done", "error")])
async def test_segments_not_ready_for_one_copy_keep_their_raw_file(pool, pki_settings, dev, state, status):
    seg = await _segment(pool, pki_settings, dev["device_id"], state=state, status=status)
    assert await one_copy.drop_raw_video(pool, pki_settings) == one_copy.OneCopyResult()
    assert _on_disk(pki_settings, seg["raw"])
    assert (await fetch_one(pool, "SELECT h264_deleted_at FROM item WHERE id = %s", (seg["id"],)))[
        "h264_deleted_at"] is None


@pytest.mark.parametrize("mp4", [None, b""])
async def test_a_segment_whose_mp4_is_missing_or_empty_keeps_its_raw_file(pool, pki_settings, dev, mp4):
    seg = await _segment(pool, pki_settings, dev["device_id"], mp4=mp4)
    assert await one_copy.drop_raw_video(pool, pki_settings) == one_copy.OneCopyResult(dropped=0, kept=1)
    assert _on_disk(pki_settings, seg["raw"])
    assert (await fetch_one(pool, "SELECT h264_deleted_at FROM item WHERE id = %s", (seg["id"],)))[
        "h264_deleted_at"] is None


async def test_audio_markers_and_expired_segments_are_not_touched(pool, pki_settings, dev):
    audio = await insert_item(pool, dev["device_id"], kind="audio", started_at=NINE)
    raw_audio = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (audio,)))["raw_path"]
    write_media(pki_settings, raw_audio)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (audio,))
    expired = await _segment(pool, pki_settings, dev["device_id"], start=NINE + dt.timedelta(minutes=1))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE id = %s", (expired["id"],))
    assert await one_copy.drop_raw_video(pool, pki_settings) == one_copy.OneCopyResult()
    assert _on_disk(pki_settings, raw_audio) and _on_disk(pki_settings, expired["raw"])


async def test_a_raw_file_that_is_already_gone_is_recorded_as_dropped(pool, pki_settings, dev):
    seg = await _segment(pool, pki_settings, dev["device_id"])
    (pki_settings.media_root / seg["raw"]).unlink()  # a run that died after the unlink, before the update
    assert (await one_copy.drop_raw_video(pool, pki_settings)).dropped == 1
    assert (await fetch_one(pool, "SELECT h264_deleted_at FROM item WHERE id = %s", (seg["id"],)))[
        "h264_deleted_at"] is not None


async def test_a_raw_file_that_cannot_be_deleted_is_tried_again_later(pool, pki_settings, dev, monkeypatch):
    seg = await _segment(pool, pki_settings, dev["device_id"])
    monkeypatch.setattr(one_copy.media, "remove_files", lambda root, rels: (0, list(rels)))
    assert await one_copy.drop_raw_video(pool, pki_settings) == one_copy.OneCopyResult(dropped=0, kept=1)
    monkeypatch.undo()
    assert (await one_copy.drop_raw_video(pool, pki_settings)).dropped == 1  # the next run
    assert not _on_disk(pki_settings, seg["raw"])


async def test_a_backlog_goes_in_batches_in_one_run(pool, pki_settings, dev, monkeypatch):
    monkeypatch.setattr(one_copy, "BATCH", 2)
    segs = [await _segment(pool, pki_settings, dev["device_id"], start=NINE + dt.timedelta(minutes=n))
            for n in range(5)]
    assert (await one_copy.drop_raw_video(pool, pki_settings)).dropped == 5
    assert not any(_on_disk(pki_settings, s["raw"]) for s in segs)


async def test_a_segment_someone_else_holds_is_left_for_the_next_run(pool, pki_settings, dev):
    held = await _segment(pool, pki_settings, dev["device_id"])
    free = await _segment(pool, pki_settings, dev["device_id"], start=NINE + dt.timedelta(minutes=1))
    async with pool.connection() as conn, conn.transaction():  # e.g. "Quên khoảng thời gian" is deleting it
        await conn.execute("SELECT 1 FROM item WHERE id = %s FOR UPDATE", (held["id"],))
        result = await asyncio.wait_for(one_copy.drop_raw_video(pool, pki_settings), 10)
    assert result.dropped == 1 and _on_disk(pki_settings, held["raw"]) and not _on_disk(pki_settings, free["raw"])


async def test_the_mp4_still_plays_on_the_hours_page(client, pool, pki_settings, dev):
    seg = await _segment(pool, pki_settings, dev["device_id"])
    await one_copy.drop_raw_video(pool, pki_settings)
    text = (await client.get("/hours", params={"day": "2026-10-14"})).text
    assert f'<video controls preload="none" src="/media/{seg["view"]}"></video>' in text


async def test_retention_and_forget_still_delete_a_segment_whose_raw_file_went(pool, pki_settings, dev):
    old = await _segment(pool, pki_settings, dev["device_id"])
    later = await _segment(pool, pki_settings, dev["device_id"], start=NINE + dt.timedelta(days=1))
    await one_copy.drop_raw_video(pool, pki_settings)
    await store.set_retention(pool, store.Retention(enabled=True))
    result = await retention.apply_retention(pool, pki_settings, now=NINE + dt.timedelta(days=8))
    assert result.video == 1 and result.files_failed == 0 and not _on_disk(pki_settings, old["view"])
    await forget.forget_range(pool, pki_settings, NINE + dt.timedelta(days=1), NINE + dt.timedelta(days=1, hours=1))
    assert not _on_disk(pki_settings, later["view"])
    assert await fetch_one(pool, "SELECT id FROM item WHERE id = %s", (later["id"],)) is None
    pending = await fetch_one(pool, "SELECT pending_files FROM forget_log ORDER BY id DESC LIMIT 1")
    assert pending["pending_files"] == []


async def test_the_job_runs_every_ten_minutes_and_has_a_label(pool, pki_settings, dev):
    periodic = {(p.task.name, p.cron) for p in jobs.job_app.periodic_registry.periodic_tasks.values()}
    assert (jobs.DROP_RAW_VIDEO, "*/10 * * * *") in periodic
    assert jobs.DROP_RAW_VIDEO in jobs.AUTO_RERUN_TASKS
    assert status.TASK_LABELS[jobs.DROP_RAW_VIDEO] == "Bỏ bản H.264 thô"
    seg = await _segment(pool, pki_settings, dev["device_id"])
    context.set_context(make_ctx(pool, pki_settings))
    try:
        await one_copy.drop_raw_video_job(timestamp=int(NINE.timestamp()))
    finally:
        context.set_context(None)
    assert not _on_disk(pki_settings, seg["raw"])
```

In `brain/tests/test_db.py`, add `"009_one_copy.sql"` after `"008_ota.sql"` in the three hard-coded lists.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_one_copy.py tests/test_db.py -q`
Expected: FAIL — `ImportError: cannot import name 'one_copy'` and the migration lists missing `009_one_copy.sql`.

- [ ] **Step 3: Write the migration**

Create `brain/src/loopi_brain/migrations/009_one_copy.sql`:

```sql
-- P3c-1 (design 2026-10-19-p3c-multicam-design.md §4.2, D32): one copy per video segment. Once a segment has its
-- MP4 (remuxed with -c copy) and its analysis is done, its raw H.264 is deleted and this says when; the MP4 is the
-- copy kept until retention. Not raw_deleted_at, which means the whole recording expired and nothing plays any more.
ALTER TABLE item ADD COLUMN h264_deleted_at TIMESTAMPTZ;
CREATE INDEX item_one_copy_due ON item (started_at)
    WHERE kind = 'video' AND status = 'ready' AND h264_deleted_at IS NULL AND raw_deleted_at IS NULL;
```

- [ ] **Step 4: Write the job**

Create `brain/src/loopi_brain/pipeline/one_copy.py`:

```python
"""One copy per video segment (P3c design §4.2, D32).

A video segment arrives as raw H.264; the API remuxes it to MP4 without re-encoding (``media.remux_item``) and the
worker extracts its keyframes from the raw file (``pipeline.video``). Once both are done (``status='ready'`` with a
``view_path``, ``analysis_state`` 'done' or 'skipped') the raw H.264 is only a second copy of the same picture: this
job deletes it and records ``h264_deleted_at``; the MP4 stays until retention. Nothing else reads the raw H.264 after
the analysis: the hours page, the vlog, forget and retention use the MP4 (by ``view_path`` and by its fixed name) and
count a missing raw file as gone.

A raw file is deleted only when its MP4 is on disk and not empty. Rows are taken ``FOR UPDATE SKIP LOCKED`` (a row
someone else holds, a forget deleting it say, waits for the next run) and marked in the same transaction, after their
raw files are gone; a run that dies in between leaves the row unmarked with its raw file already gone, and the next
run marks it (a missing raw file counts as gone). Logs carry counts and ids only.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import anyio
from psycopg_pool import AsyncConnectionPool

from .. import media
from ..config import Settings
from . import jobs
from .context import get_context
from .jobs import job_app

log = logging.getLogger(__name__)
BATCH = 500
MAX_BATCHES = 40  # per run; a bigger backlog takes more runs (one every 10 minutes)

_DUE = (
    "kind = 'video' AND status = 'ready' AND view_path IS NOT NULL AND analysis_state IN ('done', 'skipped')"
    " AND raw_deleted_at IS NULL AND h264_deleted_at IS NULL"
)


@dataclass(frozen=True)
class OneCopyResult:
    dropped: int = 0  # segments whose raw H.264 is gone (their MP4 is the kept copy)
    kept: int = 0  # segments left with their raw file this run (no usable MP4 on disk, or the raw file stayed)


def _has_mp4(media_root: Path, view_path: str) -> bool:
    try:
        return os.path.getsize(media_root / view_path) > 0
    except OSError:
        return False


def _drop(media_root: Path, rows: list[dict]) -> tuple[list[str], list[str]]:
    """Delete the raw H.264 of each row whose MP4 is usable (blocking): (ids whose raw file is gone, ids kept)."""
    gone, kept = [], []
    for row in rows:
        if not _has_mp4(media_root, row["view_path"]):
            log.warning("segment %s: its MP4 is missing or empty; the raw H.264 stays", row["id"])
            kept.append(row["id"])
            continue
        _, failed = media.remove_files(media_root, [row["raw_path"]])
        (kept if failed else gone).append(row["id"])
    return gone, kept


async def drop_raw_video(pool: AsyncConnectionPool, settings: Settings) -> OneCopyResult:
    """Delete the raw H.264 of every analysed segment that has its MP4, in batches; return the counts."""
    dropped = kept = 0
    skip: list[str] = []  # kept in this run: not taken again until the next one
    for _ in range(MAX_BATCHES):
        async with pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                f"SELECT id, raw_path, view_path FROM item WHERE {_DUE} AND NOT (id = ANY(%s))"
                " ORDER BY started_at, id LIMIT %s FOR UPDATE SKIP LOCKED",
                (skip, BATCH),
            )
            rows = await cur.fetchall()
            if not rows:
                break
            gone, left = await anyio.to_thread.run_sync(_drop, settings.media_root, rows)
            if gone:
                await conn.execute("UPDATE item SET h264_deleted_at = now() WHERE id = ANY(%s)", (gone,))
        skip.extend(left)
        dropped += len(gone)
        kept += len(left)
    if dropped or kept:
        log.info("one copy per segment: %d raw H.264 files deleted, %d kept", dropped, kept)
    return OneCopyResult(dropped, kept)


@job_app.periodic(cron="*/10 * * * *")
@job_app.task(name=jobs.DROP_RAW_VIDEO, queue="light", queueing_lock="drop_raw_video")
async def drop_raw_video_job(timestamp: int | None = None) -> None:
    """Every 10 minutes: delete the raw H.264 of the segments that now have their MP4 and their keyframes."""
    ctx = get_context()
    await drop_raw_video(ctx.pool, ctx.settings)
```

- [ ] **Step 5: Register the job**

In `brain/src/loopi_brain/pipeline/jobs.py`:
- add `"loopi_brain.pipeline.one_copy",` to `TASK_MODULES` after `"loopi_brain.pipeline.retention",`;
- add the constant `DROP_RAW_VIDEO = "drop_raw_video"` after `APPLY_RETENTION = "apply_retention"`;
- add `DROP_RAW_VIDEO` to `AUTO_RERUN_TASKS` after `APPLY_RETENTION` (each tick starts it again, like retention).

In `brain/src/loopi_brain/pipeline/status.py`, add `jobs.DROP_RAW_VIDEO: "Bỏ bản H.264 thô",` to `TASK_LABELS`
after the `jobs.APPLY_RETENTION` line.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_one_copy.py tests/test_db.py tests/test_retention.py tests/test_forget.py -q`
Expected: PASS. Then the whole suite: `cd brain && uv run pytest --cov=loopi_brain -q` — every test passes. If a
test elsewhere pins the full list of periodic tasks or task labels, add `drop_raw_video` there.

- [ ] **Step 7: Commit**

```bash
git add brain/src/loopi_brain/migrations/009_one_copy.sql brain/src/loopi_brain/pipeline/one_copy.py \
  brain/src/loopi_brain/pipeline/jobs.py brain/src/loopi_brain/pipeline/status.py brain/tests/test_one_copy.py \
  brain/tests/test_db.py
git commit -m "feat(brain): one copy per video segment — drop the raw H.264 once the MP4 and the keyframes exist (P3c-1, D32)"
```

---

### Task 2: Retention on by default

D32 changes D20: retention is **on** by default, 7 days of raw video and audio (the MP4 and the playback copies go
with them); keyframes, transcripts, captions, moments, chapters, recaps and vlogs stay for ever. A brain that runs
already gets it switched on by a migration, keeping any kept days the owner stored. The Cài đặt page still lets the
owner change the days or turn it off, and still asks before turning it on.

**Files:**
- Create: `brain/src/loopi_brain/migrations/010_retention_on.sql`
- Modify: `brain/src/loopi_brain/store.py` (`Retention.enabled = True`, docstring)
- Modify: `brain/src/loopi_brain/pipeline/retention.py` (module docstring: "it ships off" → on by default, D32)
- Modify: `deploy/README.md` (the retention bullet)
- Test: `brain/tests/test_retention_default.py` (create); update the tests that pin the old default:
  `tests/test_settings_page.py` (the `Retention()` default), `tests/test_web_pipeline.py` (the "đang tắt" line),
  `tests/test_mcp_memory.py` (the `enabled is False` assert), `tests/test_retention.py` (the `on` fixture docstring
  and the off-by-default test); `tests/test_db.py` (add `010_retention_on.sql` to the three lists)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `store.Retention()` == `Retention(video_days=7, audio_days=7, enabled=True)`.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_retention_default.py`:

```python
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
```

Update the tests that pin the old default (read each one first; change only what the new default changes):
- `tests/test_settings_page.py`: the default is now `Retention(video_days=7, audio_days=7, enabled=True)`.
- `tests/test_web_pipeline.py`: with no `setting` row the pipeline page now says
  "đang bật, giữ video thô 7 ngày, audio thô 7 ngày"; the "đang tắt" case needs an explicit
  `store.set_retention(pool, store.Retention(enabled=False))` first.
- `tests/test_mcp_memory.py` (~line 237): whatever that test checks about retention, it must now hold for
  `enabled is True` by default (if it asserts "an MCP call never turns retention on", store `enabled=False` first and
  assert it stays `False`).
- `tests/test_retention.py`: the `on` fixture's docstring ("it ships off") becomes "on by default since D32; the
  fixture keeps the tests explicit"; the off-by-default test (~line 351) becomes
  `test_retention_turned_off_in_cai_dat_deletes_nothing`, storing `Retention(enabled=False)` first and keeping its
  asserts. Every other retention test stays as is.
- `tests/test_db.py`: add `"010_retention_on.sql"` after `"009_one_copy.sql"` in the three lists.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_retention_default.py tests/test_db.py -q`
Expected: FAIL — the default is still `enabled=False`, and `010_retention_on.sql` does not exist.

- [ ] **Step 3: Write the migration and the default**

Create `brain/src/loopi_brain/migrations/010_retention_on.sql`:

```sql
-- P3c-1 (design 2026-10-19-p3c-multicam-design.md §4.2, D32, which changes D20): retention is on by default, 7 days
-- of raw video and audio. A running brain gets it switched on here; days the owner stored stay as they are. The
-- owner approved D32 on 2026-10-19.
INSERT INTO setting (key, value) VALUES ('retention', '{"video_days": 7, "audio_days": 7, "enabled": true}'::jsonb)
ON CONFLICT (key) DO UPDATE SET value = jsonb_set(setting.value, '{enabled}', 'true'::jsonb), updated_at = now();
```

In `brain/src/loopi_brain/store.py`, the `Retention` model becomes:

```python
class Retention(BaseModel):
    """How many days raw recordings are kept (spec §9.5; D20: audio like video, 7 days by default), and whether the
    nightly deletion runs: **on** by default since D32 (P3c-1, one copy per segment); the owner can change the days
    or turn it off in Cài đặt, which shows what a run would delete."""

    model_config = ConfigDict(extra="forbid")

    video_days: int = Field(default=7, ge=1, le=MAX_DAYS)
    audio_days: int = Field(default=7, ge=1, le=MAX_DAYS)
    enabled: bool = True
```

In `brain/src/loopi_brain/pipeline/retention.py`, the module docstring's first paragraph becomes:

```
Every night at 03:00 local time — **while retention is on in Cài đặt** (on by default since D32; the page shows what
a run would delete) — raw recordings older than the kept days (7 and 7 by default) lose their raw file and the copy
the web plays (video MP4, normalised audio): only recordings whose analysis is finished, never markers. ...
```

(keep the rest of that paragraph and every other paragraph unchanged), and `apply_retention`'s docstring line
"Nothing while retention is off (the default)." becomes "Nothing while retention is off in Cài đặt.".

In `deploy/README.md`, the bullet that starts "Retention is **off** after installing" becomes:

```
- Retention is **on** after installing (D32): at 03:00 local time, raw files (and their MP4/playback copies) of
  finished recordings older than the kept days (7 and 7) are deleted and the pages say "đã hết hạn". Cài đặt shows
  how many recordings (and GB) a run would delete, and changes the days or turns it off. A video segment keeps one
  copy, its MP4: the raw H.264 is deleted once the MP4 and the keyframes exist. Keyframes, transcripts, chapters,
  recaps and vlogs stay. The same 03:00 run retries files a "Quên khoảng thời gian" could not delete at once.
```

Search the templates (`grep -rn "tắt" brain/src/loopi_brain/web/templates/settings.html
brain/src/loopi_brain/web/templates/pipeline.html`) for text that says retention ships off or is off "by default"
and reword it to the new default; the "đang tắt" status line itself stays (it is still shown when the owner turns it
off).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: PASS, every test.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain/migrations/010_retention_on.sql brain/src/loopi_brain/store.py \
  brain/src/loopi_brain/pipeline/retention.py deploy/README.md brain/tests brain/src/loopi_brain/web/templates
git commit -m "feat(brain): retention is on by default, 7/7 days, and a migration turns it on in a running brain (P3c-1, D32)"
```

---

### Task 3: An editable location name per device

Every device gets `device.location` (D34), "Phòng khách" say; it starts as the device's name. The Thiết bị page
shows it as the card title (the technical name stays next to it, muted) and lets the owner change it inline.

**Files:**
- Create: `brain/src/loopi_brain/migrations/011_device_location.sql`
- Create: `brain/src/loopi_brain/places.py`
- Create: `brain/src/loopi_brain/web/templates/_location.html`
- Modify: `brain/src/loopi_brain/devices.py` (`register_device` inserts `location`; `list_devices` selects it)
- Modify: `brain/src/loopi_brain/web/routes.py` (route `POST /devices/{device_id}/location`)
- Modify: `brain/src/loopi_brain/web/templates/devices.html` (title, include the form)
- Modify: `brain/src/loopi_brain/web/templates/base.html` (CSS for `.place` and `.location`)
- Test: `brain/tests/test_places.py` (create); `brain/tests/test_web_ota.py` (its `update_form` helper takes the
  first `<form hx-post` of the page, which is now the location form: make it find
  `<form hx-post="/devices/{id}/update"`); `brain/tests/test_db.py` (add `011_device_location.sql`)

**Interfaces:**
- Consumes: nothing from Tasks 1–2.
- Produces (Tasks 4–6 use these):
  - column `device.location TEXT NOT NULL`;
  - `places.LOCATION_MAX = 60`, `places.LocationError(ValueError)` (message in Vietnamese),
    `places.clean_location(text: str) -> str`,
    `async places.set_location(pool, device_id: str, location: str) -> bool` (False: unknown or revoked device),
    `places.several(device_ids: Iterable[str | None]) -> bool` (two or more distinct non-None ids),
    `places.at(location: str | None, named: bool, sep: str = "") -> str` (`" at Phòng khách"` + `sep`, or `""`).
  - CSS classes `.place` (a small location tag) and `.location` (the inline form).

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_places.py`:

```python
from importlib import resources

import psycopg
import pytest

from helpers import fetch_one, fresh_database, paired_device
from loopi_brain import devices, places
from loopi_brain.db import migrate

HX = {"HX-Request": "true"}


def test_a_location_is_trimmed_and_its_inner_spaces_collapsed():
    assert places.clean_location("  Phòng   khách \n") == "Phòng khách"


@pytest.mark.parametrize(("text", "message"), [
    ("   ", "Tên vị trí không được để trống."),
    ("x" * 61, "Tên vị trí dài quá 60 ký tự."),
    ("Bếp\x00", "Tên vị trí có ký tự không hợp lệ."),
])
def test_a_bad_location_is_refused_with_a_vietnamese_reason(text, message):
    with pytest.raises(places.LocationError, match=message):
        places.clean_location(text)


def test_places_and_the_phrase_of_model_text():
    assert places.several(["A", "A", None]) is False
    assert places.several(["A", "B"]) is True
    assert places.at("Bếp", True) == " at Bếp" and places.at("Bếp", True, ",") == " at Bếp,"
    assert places.at("Bếp", False) == "" and places.at(None, True) == ""


def test_the_migration_starts_every_location_as_the_name(database_url):
    before = ["001_init.sql", "002_pipeline.sql", "003_understanding.sql", "004_retention_files.sql",
              "005_evaluation.sql", "006_mcp.sql", "007_vlog.sql", "008_ota.sql", "009_one_copy.sql",
              "010_retention_on.sql"]
    with fresh_database(database_url, "loopi_location") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("CREATE TABLE schema_migration (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            for name in before:
                conn.execute(resources.files("loopi_brain.migrations").joinpath(name).read_text())
                conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (name,))
            conn.execute("INSERT INTO device (id, name, cert_fingerprint) VALUES ('D1', 'loopi-ab12', 'fp')")
        assert "011_device_location.sql" in migrate(url)
        with psycopg.connect(url) as conn:
            assert conn.execute("SELECT location FROM device WHERE id = 'D1'").fetchone() == ("loopi-ab12",)
            with pytest.raises(psycopg.errors.NotNullViolation):
                conn.execute("INSERT INTO device (id, name, cert_fingerprint, location) VALUES ('D2', 'n', 'fp2', NULL)")


async def test_a_new_device_starts_with_its_name_as_its_location(pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    row = await fetch_one(pool, "SELECT name, location FROM device WHERE id = %s", (dev["device_id"],))
    assert row["location"] == row["name"]
    assert {d["id"]: d["location"] for d in await devices.list_devices(pool)}[dev["device_id"]] == row["name"]


async def test_the_owner_renames_a_location_from_the_devices_page(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    url = f"/devices/{dev['device_id']}/location"
    assert (await client.post(url, data={"location": "Bếp"})).status_code == 403  # no HX-Request
    r = await client.post(url, data={"location": "  Phòng   khách "}, headers=HX)
    assert r.status_code == 200
    assert 'value="Phòng khách"' in r.text and "Đã lưu." in r.text
    assert (await fetch_one(pool, "SELECT location FROM device WHERE id = %s", (dev["device_id"],)))[
        "location"] == "Phòng khách"
    page = (await client.get("/devices")).text
    assert "<strong>Phòng khách</strong>" in page and f'hx-post="/devices/{dev["device_id"]}/location"' in page


async def test_a_refused_location_keeps_the_old_one_and_says_why(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    r = await client.post(f"/devices/{dev['device_id']}/location", data={"location": " "}, headers=HX)
    assert r.status_code == 200 and "Tên vị trí không được để trống." in r.text
    row = await fetch_one(pool, "SELECT name, location FROM device WHERE id = %s", (dev["device_id"],))
    assert row["location"] == row["name"]


async def test_a_location_is_escaped_everywhere(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    await client.post(f"/devices/{dev['device_id']}/location", data={"location": "<b>x</b>"}, headers=HX)
    page = (await client.get("/devices")).text
    assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page


async def test_unknown_and_revoked_devices_cannot_be_renamed(client, pool, pki_settings):
    assert (await client.post("/devices/NOPE/location", data={"location": "Bếp"}, headers=HX)).status_code == 404
    dev = await paired_device(pool, pki_settings)
    await devices.revoke_device(pool, dev["device_id"])
    r = await client.post(f"/devices/{dev['device_id']}/location", data={"location": "Bếp"}, headers=HX)
    assert r.status_code == 404
    page = (await client.get("/devices")).text
    assert f'hx-post="/devices/{dev["device_id"]}/location"' not in page  # no form on a revoked card
```

In `brain/tests/test_web_ota.py`, change the `update_form(html)` helper (and the code near line 238 that slices the
same way) so it starts at the update form, for example
`start = html.index('/update"'); html[html.rindex("<form", 0, start):html.index("</form>", start)]`; every OTA
test must keep passing unchanged otherwise. In `brain/tests/test_db.py`, add `"011_device_location.sql"` after
`"010_retention_on.sql"` in the three lists.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_places.py tests/test_db.py -q`
Expected: FAIL — `ImportError: cannot import name 'places'`; no `location` column.

- [ ] **Step 3: Write the migration and `places.py`**

Create `brain/src/loopi_brain/migrations/011_device_location.sql`:

```sql
-- P3c-1 (design 2026-10-19-p3c-multicam-design.md §4.1, D34): every device has an editable location name
-- ("Phòng khách"), used on the timeline, in search, chat, MCP and in the text the chapter and recap models read.
-- It starts as the device's name.
ALTER TABLE device ADD COLUMN location TEXT;
UPDATE device SET location = name;
ALTER TABLE device ALTER COLUMN location SET NOT NULL;
```

Create `brain/src/loopi_brain/places.py`:

```python
"""Where each device stands (P3c design §4.1, §4.4, D34): an editable location name per device ("Phòng khách"),
shown on the timeline, in search, chat and MCP, and named in the text the chapter and recap models read. It starts
as the device's name; the owner changes it on the Thiết bị page. Pages and prompts name the place only when the rows
come from two or more devices, so a brain with a single camera looks and prompts as before."""
from __future__ import annotations

from collections.abc import Iterable

from psycopg_pool import AsyncConnectionPool

LOCATION_MAX = 60


class LocationError(ValueError):
    """A location name the page refuses; the message is shown to the owner as is."""


def clean_location(text: str) -> str:
    """The name to store: surrounding blanks dropped, inner runs of blanks made one space; refused when empty,
    longer than LOCATION_MAX characters, or holding a character that does not print."""
    cleaned = " ".join(text.split())
    if not cleaned:
        raise LocationError("Tên vị trí không được để trống.")
    if len(cleaned) > LOCATION_MAX:
        raise LocationError(f"Tên vị trí dài quá {LOCATION_MAX} ký tự.")
    if not cleaned.isprintable():
        raise LocationError("Tên vị trí có ký tự không hợp lệ.")
    return cleaned


async def set_location(pool: AsyncConnectionPool, device_id: str, location: str) -> bool:
    """Store an already cleaned name for a device that is not revoked; False when there is no such device."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE device SET location = %s WHERE id = %s AND NOT revoked RETURNING id", (location, device_id)
        )
        return await cur.fetchone() is not None


def several(device_ids: Iterable[str | None]) -> bool:
    """Whether the rows come from two or more devices: only then is each one labelled with its place."""
    return len({d for d in device_ids if d is not None}) >= 2


def at(location: str | None, named: bool, sep: str = "") -> str:
    """The place in a line of model text (" at Phòng khách" + ``sep``), or nothing when places are not named."""
    return f" at {location}{sep}" if named and location else ""
```

- [ ] **Step 4: Save new devices with their location, list it, and the route**

In `brain/src/loopi_brain/devices.py`, `register_device`'s INSERT becomes:

```python
            name = f"loopi-{device_id[-4:].lower()}"
            await conn.execute(
                "INSERT INTO device (id, name, location, cert_fingerprint, hw_info) VALUES (%s, %s, %s, %s, %s)",
                (device_id, name, name, fp, Jsonb(hw_info)),
            )
```

and `list_devices` selects `id, name, location, revoked, hw_info, last_heartbeat, last_heartbeat_at, dropped_total,
created_at`.

In `brain/src/loopi_brain/web/routes.py` (import `places` from `..` next to the existing module imports, and `Form`
from `fastapi` if it is not imported yet), add after the `revoke` route:

```python
@router.post("/devices/{device_id}/location", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def save_location(request: Request, device_id: str, location: str = Form("")):
    """Rename where a device stands (D34). A refused name is answered with the form and the reason (200, so htmx
    swaps it in); an unknown or revoked device is a 404."""
    try:
        cleaned = places.clean_location(location)
    except places.LocationError as exc:
        return TEMPLATES.TemplateResponse(
            request, "_location.html", {"d": {"id": device_id, "location": location}, "error": str(exc)}
        )
    if not await places.set_location(request.app.state.pool, device_id, cleaned):
        raise HTTPException(status_code=404, detail="unknown device")
    return TEMPLATES.TemplateResponse(request, "_location.html", {"d": {"id": device_id, "location": cleaned},
                                                                   "saved": True})
```

Create `brain/src/loopi_brain/web/templates/_location.html`:

```html
<form class="inline location" hx-post="/devices/{{ d.id }}/location" hx-target="this" hx-swap="outerHTML">
  <label>Vị trí: <input name="location" value="{{ d.location }}" maxlength="60" required></label>
  <button>Lưu</button>
  {% if saved %}<span class="muted">Đã lưu.</span>{% endif %}
  {% if error %}<span class="err">{{ error }}</span>{% endif %}
</form>
```

In `brain/src/loopi_brain/web/templates/devices.html`, the title line `<strong>{{ d.name }}</strong>` becomes
`<strong>{{ d.location }}</strong> <span class="muted">{{ d.name }}</span>`, and right after the status `{% endif %}`
that follows it add:

```html
  {% if not d.revoked %}{% include "_location.html" %}{% endif %}
```

In `brain/src/loopi_brain/web/templates/base.html`, add to the inline CSS (next to the other small classes such as
`.muted`):

```css
.place { font-size: .8em; border: 1px solid #bbb; border-radius: 3px; padding: 0 .3em; margin-right: .3em; }
form.location { margin: .3em 0; }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: PASS, every test (including `tests/test_web.py::test_device_name_is_escaped` and the OTA page tests).

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/migrations/011_device_location.sql brain/src/loopi_brain/places.py \
  brain/src/loopi_brain/devices.py brain/src/loopi_brain/web/routes.py brain/src/loopi_brain/web/templates \
  brain/tests/test_places.py brain/tests/test_web_ota.py brain/tests/test_db.py
git commit -m "feat(brain): an editable location name per device on the Thiết bị page (P3c-1, D34)"
```

---

### Task 4: The merged timeline: location labels and a camera filter

"Hôm nay"/"Ngày" (`GET /`) and "Chi tiết theo giờ" (`GET /hours`) already merge every device by time. They gain a
`device` query parameter (the filter, kept in every link of the page), a filter bar ("Camera: Tất cả · Phòng khách ·
Bếp") and a location tag on each entry. Both appear once the day has recordings from two or more devices (or a
filter is set); a day with one camera renders exactly as today. Chapters, the recap, commitments and the vlog stay
day-wide; with a filter, a chapter shows only that camera's moments and is left out when it has none.

**Files:**
- Modify: `brain/src/loopi_brain/places.py` (add `day_places`)
- Modify: `brain/src/loopi_brain/web/day_routes.py` (`day_view`, `day_page`)
- Modify: `brain/src/loopi_brain/web/routes.py` (`_day_by_hour`, `timeline`)
- Create: `brain/src/loopi_brain/web/templates/_places.html`
- Modify: `brain/src/loopi_brain/web/templates/day.html`, `brain/src/loopi_brain/web/templates/timeline.html`
- Test: `brain/tests/test_day_places.py` (create)

**Interfaces:**
- Consumes: `device.location`, `places.several`, CSS `.place` (Task 3).
- Produces: `async places.day_places(pool, start: dt.datetime, end: dt.datetime) -> list[dict]` — every device with
  an item or a moment in `[start, end)`, as `{"id", "location"}`, ordered by location then id;
  `async places.known_device(pool, device_id: str) -> bool` (any device row, revoked ones too);
  `day_view(pool, day, zone, today=None, device: str | None = None)` returns, besides today's keys, `"places"` (that
  list), `"device"` (the filter or None); each moment view carries `"place"` (its location when labels are shown,
  else None).

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_day_places.py`:

```python
import datetime as dt

import pytest

from helpers import insert_chapter, insert_item, insert_moment, paired_device
from loopi_brain import devices, places

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
DAY = dt.date(2026, 10, 14)


@pytest.fixture
async def two(pool, pki_settings):
    """Two cameras: the living room (Pi) and the kitchen."""
    living = await paired_device(pool, pki_settings)
    kitchen = await paired_device(pool, pki_settings)
    await places.set_location(pool, living["device_id"], "Phòng khách")
    await places.set_location(pool, kitchen["device_id"], "Bếp")
    return living["device_id"], kitchen["device_id"]


async def test_a_day_with_one_camera_has_no_labels_and_no_filter(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, text="Chào", lang="vi")
    text = (await client.get("/", params={"day": "2026-10-14"})).text
    assert 'class="place"' not in text and 'class="places"' not in text


async def test_every_entry_of_a_merged_day_names_its_place(client, pool, two):
    living, kitchen = two
    a = await insert_moment(pool, living, kind="speech", ts=NINE, text="Chào", lang="vi")
    b = await insert_moment(pool, kitchen, kind="marker", ts=NINE + dt.timedelta(minutes=5), text="x")
    text = (await client.get("/", params={"day": "2026-10-14"})).text
    assert f'<p class="speech" id="m-{a}"><time>09:00:00</time> <span class="place">Phòng khách</span> ' in text
    assert f'<p class="marker" id="m-{b}"><time>09:05:00</time> <span class="place">Bếp</span> ★' in text
    assert text.index("Tất cả") < text.index(f'href="/?day=2026-10-14&amp;device={kitchen}">Bếp</a>') < text.index(
        f'href="/?day=2026-10-14&amp;device={living}">Phòng khách</a>')  # ordered by location: Bếp before Phòng khách


async def test_the_filter_keeps_one_camera_and_every_link_keeps_the_filter(client, pool, two):
    living, kitchen = two
    keep = await insert_moment(pool, living, kind="speech", ts=NINE, text="Ở phòng khách", lang="vi")
    drop = await insert_moment(pool, kitchen, kind="speech", ts=NINE + dt.timedelta(hours=2), text="Ở bếp", lang="vi")
    only_kitchen = await insert_chapter(pool, DAY, NINE + dt.timedelta(hours=2), NINE + dt.timedelta(hours=2),
                                        title="Nấu ăn")
    text = (await client.get("/", params={"day": "2026-10-14", "device": living})).text
    assert f'id="m-{keep}"' in text and f'id="m-{drop}"' not in text
    assert f'id="ch-{only_kitchen}"' not in text  # no moment of this camera in that chapter
    assert 'class="place"' not in text  # one camera shown: no tags, the bar says which
    assert "<strong>Phòng khách</strong>" in text and 'href="/?day=2026-10-14">Tất cả</a>' in text
    assert f'href="/?day=2026-10-13&amp;device={living}"' in text and f'href="/?day=2026-10-15&amp;device={living}"' in text
    assert f'href="/hours?day=2026-10-14&amp;device={living}"' in text
    assert f'<input type="hidden" name="device" value="{living}">' in text


async def test_an_unknown_device_filter_is_refused(client, pool, two):
    assert (await client.get("/", params={"day": "2026-10-14", "device": "NOPE"})).status_code == 422
    assert (await client.get("/hours", params={"day": "2026-10-14", "device": "NOPE"})).status_code == 422


async def test_a_revoked_devices_moments_keep_their_label(client, pool, two):
    living, kitchen = two
    await insert_moment(pool, living, kind="speech", ts=NINE, text="Một", lang="vi")
    await insert_moment(pool, kitchen, kind="speech", ts=NINE + dt.timedelta(minutes=1), text="Hai", lang="vi")
    await devices.revoke_device(pool, kitchen)
    text = (await client.get("/", params={"day": "2026-10-14"})).text
    assert '<span class="place">Bếp</span>' in text


async def test_the_hours_page_filters_and_labels_too(client, pool, two):
    living, kitchen = two
    await insert_item(pool, living, kind="audio", started_at=NINE)
    other = await insert_item(pool, kitchen, kind="audio", started_at=NINE + dt.timedelta(minutes=1))
    await insert_moment(pool, kitchen, kind="speech", ts=NINE + dt.timedelta(minutes=1), text="Ở bếp", lang="vi")
    merged = (await client.get("/hours", params={"day": "2026-10-14"})).text
    assert '<span class="place">Bếp</span>' in merged and '<span class="place">Phòng khách</span>' in merged
    filtered = (await client.get("/hours", params={"day": "2026-10-14", "device": living})).text
    assert f'id="item-{other}"' not in filtered and "Ở bếp" not in filtered
    assert f'href="/hours?day=2026-10-13&amp;device={living}"' in filtered
    assert f'href="/?day=2026-10-14&amp;device={living}"' in filtered


async def test_a_location_is_escaped_in_the_filter_bar(client, pool, two):
    living, kitchen = two
    await places.set_location(pool, kitchen, "<i>Bếp</i>")
    await insert_moment(pool, living, kind="speech", ts=NINE, text="Một", lang="vi")
    await insert_moment(pool, kitchen, kind="speech", ts=NINE, text="Hai", lang="vi")
    text = (await client.get("/", params={"day": "2026-10-14"})).text
    assert "<i>Bếp</i>" not in text and "&lt;i&gt;Bếp&lt;/i&gt;" in text
```

Check how the hours page renders an audio segment and a speech line (`templates/timeline.html`) before running: the
asserts above use the `id="item-…"` of segments and the speech text.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_day_places.py -q`
Expected: FAIL — no labels, no filter, `device` is ignored (no 422).

- [ ] **Step 3: `places.day_places`**

Add to `brain/src/loopi_brain/places.py` (with `import datetime as dt` at the top):

```python
async def day_places(pool: AsyncConnectionPool, start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Every device (revoked ones too) with a recording or a moment in [start, end), as {"id", "location"},
    ordered by location then id: the cameras a merged day shows."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT d.id, d.location FROM device d"
            " WHERE EXISTS (SELECT 1 FROM item i WHERE i.device_id = d.id AND i.started_at >= %(start)s"
            "               AND i.started_at < %(end)s)"
            " OR EXISTS (SELECT 1 FROM moment m WHERE m.device_id = d.id AND m.ts_start >= %(start)s"
            "            AND m.ts_start < %(end)s)"
            " ORDER BY d.location, d.id",
            {"start": start, "end": end},
        )
        return await cur.fetchall()


async def known_device(pool: AsyncConnectionPool, device_id: str) -> bool:
    """Whether a device (revoked ones too) has this id: a camera filter naming anything else is refused."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM device WHERE id = %s", (device_id,))
        return await cur.fetchone() is not None
```

- [ ] **Step 4: The Day page**

In `brain/src/loopi_brain/web/day_routes.py`:

- `day_view` takes `device: str | None = None`. Its moment query selects `m.device_id` too, joins
  `LEFT JOIN device d ON d.id = m.device_id` selecting `d.location`, and adds
  `AND (%s::text IS NULL OR m.device_id = %s)` with `device, device` appended to the parameters.
- Before the query it computes `day_cams = await places.day_places(pool, start, end)` and
  `named = device is None and len(day_cams) >= 2`, and each moment view gets `view["place"] = row["location"] if
  named else None` (do it in `_moment_view` by passing `named`).
- In the chapter loop, when `device` is set and `members` is empty, the chapter is not appended to `sections` (the
  `loose` handling stays as it is).
- The returned dict adds `"places": day_cams` and `"device": device`.
- `day_page` takes `device: str | None = Query(default=None, max_length=64)`. When `device` is set and
  `await places.known_device(pool, device)` is False, it raises `HTTPException(status_code=422,
  detail="unknown device")`. It passes `device` to `day_view` and adds `"base": "/"` to the template context.

Create `brain/src/loopi_brain/web/templates/_places.html`:

```html
{% if places|length >= 2 or device %}
<nav class="places">Camera:
  {% if device %}<a href="{{ base }}?day={{ day }}">Tất cả</a>{% else %}<strong>Tất cả</strong>{% endif %}
  {% for p in places %} · {% if p.id == device %}<strong>{{ p.location }}</strong>{% else %}<a href="{{ base }}?day={{ day }}&amp;device={{ p.id }}">{{ p.location }}</a>{% endif %}{% endfor %}
</nav>
{% endif %}
```

In `brain/src/loopi_brain/web/templates/day.html`:

- after `{% block content %}` add `{% set dq = ("&device=" ~ device) if device else "" %}`;
- the four day links become `href="/?day={{ prev_day }}{{ dq }}"`, `href="/?day={{ next_day }}{{ dq }}"` and
  `href="/hours?day={{ day }}{{ dq }}"`; the date form gets
  `{% if device %}<input type="hidden" name="device" value="{{ device }}">{% endif %}` before its button
  (Jinja escapes `&` in `dq` to `&amp;` inside the attribute; the tests expect `&amp;device=`);
- right after the `<h1>` line add `{% include "_places.html" %}`;
- in the `moments(g)` macro, right after each `<time>{{ m.time }}</time> ` (figure captions, markers, speech) add
  `{% if m.place %}<span class="place">{{ m.place }}</span> {% endif %}`, so that with `m.place` None the output is
  byte-for-byte what it is today (the existing `tests/test_day_page.py` fragments must still match).

- [ ] **Step 5: The hours page**

In `brain/src/loopi_brain/web/routes.py`:

- `_day_by_hour(pool, day, zone, device: str | None = None, labels: dict[str, str] | None = None)` keeps its
  return type: each of its three queries selects the device id (`i.device_id`, `k.device_id`, `m.device_id`) and
  adds `AND (%s::text IS NULL OR <alias>.device_id = %s)`, and every entry (segment, keyframe, speech, marker) gets
  `"place": (labels or {}).get(row["device_id"])`.
- `timeline(request, day=..., device: str | None = Query(default=None, max_length=64))` refuses an unknown device
  with 422 exactly like `day_page` (`places.known_device`), computes
  `day_cams = await places.day_places(pool, *day_bounds_utc(day, zone))`, and passes
  `labels = {p["id"]: p["location"] for p in day_cams} if device is None and len(day_cams) >= 2 else None` to
  `_day_by_hour`; the template gets `hours`, `places` (`day_cams`), `device` and `"base": "/hours"`.

In `brain/src/loopi_brain/web/templates/timeline.html`: the same `dq` variable; the prev/next links
`href="/hours?day={{ prev_day }}{{ dq }}"` / `{{ next_day }}{{ dq }}` and the "Theo chương" link
`href="/?day={{ day }}{{ dq }}"`; `{% include "_places.html" %}` right after the `</nav>`; and after the `<time>` of
each keyframe caption, speech line, marker and segment add `{% if X.place %}<span class="place">{{ X.place }}</span>
{% endif %}` (with `X` the loop variable: `kf`, `m`, `it`), again so that the output is unchanged when `place` is
None (`tests/test_web_pipeline.py` must still pass).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: PASS, every test.

- [ ] **Step 7: Commit**

```bash
git add brain/src/loopi_brain/places.py brain/src/loopi_brain/web brain/tests/test_day_places.py
git commit -m "feat(brain): the merged timeline names each entry's place and filters by camera (P3c-1, D34)"
```

---

### Task 5: Places in search, chat and MCP

Search results and chat sources carry the moment's device and location. The Search page tags each result with its
place, and the chat prompt names it (" at Bếp"), once the results come from two or more devices. MCP results always
carry `location` (a structured API: the client may use it or not).

**Files:**
- Modify: `brain/src/loopi_brain/search.py` (`_COLUMNS`, the two queries, `Hit`)
- Modify: `brain/src/loopi_brain/web/search_routes.py` (`hit_view`, the Search page and the chat `sources` event)
- Modify: `brain/src/loopi_brain/web/templates/_hit.html`
- Modify: `brain/src/loopi_brain/chat.py` (`source_lines`)
- Modify: `brain/src/loopi_brain/mcp/memory.py` (`MomentRef`, `MarkerOut`, `MomentOut`, `search_memories`,
  `get_timeline`, `get_moment`)
- Test: `brain/tests/test_search_places.py` (create)

**Interfaces:**
- Consumes: `device.location`, `places.several`, `places.at` (Task 3).
- Produces: `search.Hit` gains two last fields with defaults, `device_id: str | None = None` and
  `location: str | None = None` (positional constructions with 11 arguments and `Hit(**row, ...)` in
  `evaluation/questions.py` keep working); `hit_view(hit, zone, *, named: bool = False)` adds `"place"`;
  `MomentRef.location`, `MarkerOut.location`, `MomentOut.location`: `str | None = None`.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_search_places.py`:

```python
import datetime as dt

import httpx
import pytest

from helpers import FakeOllama, insert_moment, paired_device
from loopi_brain import chat, places, search
from loopi_brain.app import create_app
from loopi_brain.mcp import memory

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)


@pytest.fixture
def ollama():
    return FakeOllama()


@pytest.fixture
async def web(pool, pki_settings, jobs_open, ollama):
    """The app with a fake model server, as in tests/test_search.py (moments without embeddings are found by words)."""
    app = create_app(pki_settings, pool=pool, http_client=ollama.client(), llm_client=ollama.client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


@pytest.fixture
async def two(pool, pki_settings):
    living = await paired_device(pool, pki_settings)
    kitchen = await paired_device(pool, pki_settings)
    await places.set_location(pool, living["device_id"], "Phòng khách")
    await places.set_location(pool, kitchen["device_id"], "Bếp")
    return living["device_id"], kitchen["device_id"]


async def test_a_hit_carries_its_device_and_location(pool, two):
    living, _ = two
    moment = await insert_moment(pool, living, kind="speech", ts=NINE, text="đi chợ mua rau", lang="vi")
    [hit] = (await search.search_moments(pool, None, "đi chợ mua rau")).hits
    assert (hit.id, hit.device_id, hit.location) == (moment, living, "Phòng khách")


async def test_the_search_page_tags_results_from_several_cameras(web, pool, two):
    living, kitchen = two
    await insert_moment(pool, living, kind="speech", ts=NINE, text="đi chợ mua rau", lang="vi")
    await insert_moment(pool, kitchen, kind="speech", ts=NINE + dt.timedelta(minutes=1), text="đi chợ mua cá",
                        lang="vi")
    text = (await web.get("/search", params={"q": "đi chợ"})).text
    assert '<span class="place">Phòng khách</span>' in text and '<span class="place">Bếp</span>' in text


async def test_results_from_one_camera_are_not_tagged(web, pool, two):
    living, _ = two
    await insert_moment(pool, living, kind="speech", ts=NINE, text="đi chợ mua rau", lang="vi")
    assert 'class="place"' not in (await web.get("/search", params={"q": "đi chợ"})).text


def test_chat_sources_name_the_place_when_there_are_several(pki_settings):
    def hit(n, device, location):
        return search.Hit(f"M{n}", "speech", NINE, "đi chợ", "vi", None, None, None, None, None, 0.0,
                          device_id=device, location=location)

    several = chat.source_lines([hit(1, "A", "Phòng khách"), hit(2, "B", "Bếp")], pki_settings.zone)
    assert several == ["[1] 2026-10-14 09:00 at Phòng khách, speech (vi): đi chợ",
                       "[2] 2026-10-14 09:00 at Bếp, speech (vi): đi chợ"]
    single = chat.source_lines([hit(1, "A", "Phòng khách")], pki_settings.zone)
    assert single == ["[1] 2026-10-14 09:00, speech (vi): đi chợ"]


async def test_mcp_results_carry_the_location(pool, pki_settings, two):
    living, kitchen = two
    speech = await insert_moment(pool, living, kind="speech", ts=NINE, text="đi chợ mua rau", lang="vi")
    marker = await insert_moment(pool, kitchen, kind="marker", ts=NINE + dt.timedelta(minutes=1), text="x")
    found = await memory.search_memories(pool, None, pki_settings, "đi chợ mua rau", None, None, 10)
    assert [(m.moment_id, m.location) for m in found.moments] == [(speech, "Phòng khách")]
    timeline = await memory.get_timeline(pool, "2026-10-14", pki_settings.zone)
    assert [(m.moment_id, m.location) for m in timeline.markers] == [(marker, "Bếp")]
    one, _ = await memory.get_moment(pool, pki_settings, speech, images=False)
    assert one.location == "Phòng khách"
```

`search_memories(pool, router, settings, query, start, end, limit)` and `search.search_moments(pool, router, query)`
are called with `router=None`: words only, so no model server is needed. Check the imports against
`tests/test_search.py` (`create_app`, `FakeOllama`) before running.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_search_places.py -q`
Expected: FAIL — `Hit` has no `device_id`/`location`, no place tags, no MCP `location`.

- [ ] **Step 3: Search**

In `brain/src/loopi_brain/search.py`:

```python
_COLUMNS = (
    "m.id, m.kind, m.ts_start, m.text, m.lang, m.keyframe_id, m.item_id, i.meta AS item_meta, m.device_id,"
    " d.location"
)
```

both queries add `LEFT JOIN device d ON d.id = m.device_id` after `LEFT JOIN item i ON i.id = m.item_id`; `Hit`
gains, after `score: float`:

```python
    device_id: str | None = None  # the device that recorded the moment
    location: str | None = None  # where that device stands (D34)
```

and the `Hit(...)` built in `search_moments` passes `device_id=row["device_id"], location=row["location"]`.

- [ ] **Step 4: The Search page and the chat sources**

In `brain/src/loopi_brain/web/search_routes.py`, `hit_view(hit: search.Hit, zone, *, named: bool = False)` adds
`"place": hit.location if named else None` to its dict. The Search page builds its hits with
`named = places.several(h.device_id for h in result.hits)` and passes it to every `hit_view` call; the chat
`sources` event (where `_sources.html` is rendered, ~line 118) does the same over its hits.

`brain/src/loopi_brain/web/templates/_hit.html`: after the `{% if h.lang and h.kind == "speech" %}…{% endif %}` of
the second line add `{% if h.place %} · <span class="place">{{ h.place }}</span>{% endif %}` (so the existing
fragments asserted in `tests/test_search.py` stay as they are).

In `brain/src/loopi_brain/chat.py`, `source_lines` becomes:

```python
def source_lines(hits: list[Hit], zone) -> list[str]:
    named = places.several(hit.device_id for hit in hits)
    lines = []
    for n, hit in enumerate(hits, start=1):
        kind = KIND_WORDS.get(hit.kind, hit.kind)
        if hit.kind == "speech":
            kind = f"speech ({hit.lang or '?'})"
        text = hit.text
        if hit.kind == "marker":
            note = moment_rules.marker_note(hit.item_meta or {})
            text = f"the user marked this moment: {note}" if note else "the user marked this moment"
        when = f"{hit.ts_start.astimezone(zone):%Y-%m-%d %H:%M}{places.at(hit.location, named)}"
        lines.append(f"[{n}] {when}, {kind}: {clip(text, SOURCE_TEXT_MAX)}")
    return lines
```

(import `places` from `.`).

- [ ] **Step 5: MCP**

In `brain/src/loopi_brain/mcp/memory.py`:
- `MomentRef`, `MarkerOut` and `MomentOut` each gain `location: str | None = None  # where the recording device
  stands (D34)` as their last field;
- `search_memories` passes `location=h.location` to each `MomentRef`;
- `get_timeline`'s marker query becomes `SELECT m.id, m.ts_start, m.text, d.location FROM moment m LEFT JOIN device d
  ON d.id = m.device_id WHERE m.kind = 'marker' AND m.ts_start >= %s AND m.ts_start < %s ORDER BY m.ts_start, m.id`,
  and each `MarkerOut` gets `location=m["location"]`;
- `get_moment`'s query selects `d.location` with `LEFT JOIN device d ON d.id = m.device_id`, and `MomentOut` gets
  `location=row["location"]`.

Do not add `from __future__ import annotations` to this module (the MCP SDK reads its annotations).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: PASS, every test (including `tests/test_mcp_server.py`, whose models are built by keyword, and
`tests/test_eval_questions.py`, which builds `Hit` positionally).

- [ ] **Step 7: Commit**

```bash
git add brain/src/loopi_brain/search.py brain/src/loopi_brain/web brain/src/loopi_brain/chat.py \
  brain/src/loopi_brain/mcp/memory.py brain/tests/test_search_places.py
git commit -m "feat(brain): search, chat and MCP name the place of each moment (P3c-1, D34)"
```

---

### Task 6: The chapter and recap models read where things happened

When a chapter's moments (or a day's speech and marked moments) come from two or more devices, each line the model
reads names the place right after the time, in English like the rest of the prompt: `09:15 at Phòng khách, picture:
…`, `09:20 at Bếp, speech (vi): …`, `09:40 at Bếp, the user pressed the mark button: chìa khoá`, and in the recap
`[S1] 09:10 at Phòng khách (en) Thank you.`, `[M1] 09:40 at Bếp the user pressed the mark button: chìa khoá`. With one
device every line is exactly what it is today (the existing tests pin that). A repeated caption is dropped per
camera, not across cameras (two rooms can look alike at the same minute).

**Files:**
- Modify: `brain/src/loopi_brain/pipeline/chapters.py` (`chapter_lines`)
- Modify: `brain/src/loopi_brain/pipeline/recap.py` (`recap_material`)
- Test: `brain/tests/test_prompt_places.py` (create)

**Interfaces:**
- Consumes: `device.location`, `places.several`, `places.at` (Task 3).
- Produces: no new names; the line formats above.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_prompt_places.py`:

```python
import datetime as dt

import pytest

from helpers import insert_chapter, insert_moment, paired_device
from loopi_brain import places
from loopi_brain.pipeline import chapters, recap

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)
DAY = dt.date(2026, 10, 14)


def at(minutes: int) -> dt.datetime:
    return NINE + dt.timedelta(minutes=minutes)


@pytest.fixture
async def two(pool, pki_settings):
    living = await paired_device(pool, pki_settings)
    kitchen = await paired_device(pool, pki_settings)
    await places.set_location(pool, living["device_id"], "Phòng khách")
    await places.set_location(pool, kitchen["device_id"], "Bếp")
    return living["device_id"], kitchen["device_id"]


async def test_chapter_lines_name_the_place_when_two_cameras_recorded(pool, pki_settings, two):
    living, kitchen = two
    await insert_moment(pool, living, kind="visual", ts=at(15), text="Two people cook")
    await insert_moment(pool, kitchen, kind="visual", ts=at(16), text="Two people cook")  # same words, other room
    await insert_moment(pool, kitchen, kind="speech", ts=at(20), text="Mai mình đi chợ nhé", lang="vi")
    await insert_moment(pool, kitchen, kind="marker", ts=at(40), text="Marked moment")
    lines = await chapters.chapter_lines(pool, NINE, at(59), pki_settings.zone)
    assert lines[:3] == ["09:15 at Phòng khách, picture: Two people cook", "09:16 at Bếp, picture: Two people cook",
                         "09:20 at Bếp, speech (vi): Mai mình đi chợ nhé"]
    assert lines[3].startswith("09:40 at Bếp, the user pressed the mark button")


async def test_chapter_lines_of_one_camera_are_unchanged(pool, pki_settings, two):
    living, _ = two
    await insert_moment(pool, living, kind="visual", ts=at(15), text="Two people cook")
    await insert_moment(pool, living, kind="visual", ts=at(16), text="Two people cook")  # a repeat: dropped
    await insert_moment(pool, living, kind="speech", ts=at(20), text="Mai mình đi chợ nhé", lang="vi")
    assert await chapters.chapter_lines(pool, NINE, at(59), pki_settings.zone) == [
        "09:15 picture: Two people cook", "09:20 speech (vi): Mai mình đi chợ nhé"]


async def test_recap_lines_name_the_place_when_two_cameras_recorded(pool, pki_settings, two):
    living, kitchen = two
    await insert_chapter(pool, DAY, NINE, at(30), title="Sáng", summary="Một buổi sáng.")
    await insert_moment(pool, living, kind="speech", ts=at(10), text="Thank you.", lang="en")
    await insert_moment(pool, kitchen, kind="marker", ts=at(40), text="Marked moment")
    material = await recap.recap_material(pool, DAY, pki_settings.zone)
    assert material.speech == ["[S1] 09:10 at Phòng khách (en) Thank you."]
    assert material.markers[0].startswith("[M1] 09:40 at Bếp the user pressed the mark button")


async def test_recap_lines_of_one_camera_are_unchanged(pool, pki_settings, two):
    living, _ = two
    await insert_moment(pool, living, kind="speech", ts=at(10), text="Thank you.", lang="en")
    material = await recap.recap_material(pool, DAY, pki_settings.zone)
    assert material.speech == ["[S1] 09:10 (en) Thank you."]
```

Read `insert_moment` in `tests/helpers.py` before running: a `marker` moment there may need an `item_id` for
`marker_note`; with none, `marker_line` writes "the user pressed the mark button" with no note, which the
`startswith` asserts allow.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_prompt_places.py -q`
Expected: FAIL — the two-camera lines carry no place, and the second caption is dropped as a repeat.

- [ ] **Step 3: Chapter lines**

In `brain/src/loopi_brain/pipeline/chapters.py`, `chapter_lines` becomes (import `places` from `..`):

```python
async def chapter_lines(pool: AsyncConnectionPool, first: dt.datetime, last: dt.datetime, zone) -> list[str]:
    """What happened in a chapter, one line per moment, bounded: at most 40 picture lines (repeats dropped, per
    camera), 60 speech lines and 20 markers, spread over the chapter, within PROMPT_BUDGET characters. When two or
    more cameras recorded the chapter, each line names its camera's place after the time (D34)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT m.kind, m.ts_start, m.text, m.lang, m.device_id, d.location, i.meta AS item_meta FROM moment m"
            " LEFT JOIN item i ON i.id = m.item_id LEFT JOIN device d ON d.id = m.device_id"
            " WHERE m.ts_start >= %s AND m.ts_start <= %s ORDER BY m.ts_start, m.id",
            (first, last),
        )
        rows = await cur.fetchall()
    named = places.several(r["device_id"] for r in rows)
    pictures, speech, markers = [], [], []
    previous_picture: dict[str | None, str] = {}  # the last caption of each camera
    for r in rows:
        at = hm(r["ts_start"], zone) + places.at(r["location"], named, ",")
        if r["kind"] == "visual":
            if r["text"] != previous_picture.get(r["device_id"]):
                pictures.append((r["ts_start"], f"{at} picture: {clip(r['text'], 200)}"))
            previous_picture[r["device_id"]] = r["text"]
        elif r["kind"] == "speech":
            speech.append((r["ts_start"], f"{at} speech ({r['lang'] or '?'}): {clip(r['text'], 300)}"))
        else:
            markers.append((r["ts_start"], marker_line(at, marker_note(r["item_meta"] or {}))))
    chosen = (spread(pictures, MAX_PICTURE_LINES) + spread(speech, MAX_SPEECH_LINES)
              + spread(markers, MAX_MARKER_LINES))
    return within([line for _, line in sorted(chosen, key=lambda pair: pair[0])], PROMPT_BUDGET)
```

- [ ] **Step 4: Recap lines**

In `brain/src/loopi_brain/pipeline/recap.py`, `recap_material` (import `places` from `..`):
- the marker query becomes `SELECT m.id, m.ts_start, m.device_id, d.location, i.meta AS item_meta FROM moment m
  LEFT JOIN item i ON i.id = m.item_id LEFT JOIN device d ON d.id = m.device_id WHERE …` (same WHERE and ORDER BY);
- the speech query becomes `SELECT m.id, m.ts_start, m.lang, m.text, m.device_id, d.location FROM moment m LEFT JOIN
  device d ON d.id = m.device_id WHERE m.kind = 'speech' AND m.ts_start >= %s AND m.ts_start < %s ORDER BY
  m.ts_start, m.id`;
- after the queries: `named = places.several(r["device_id"] for r in [*marker_rows, *speech_rows])`;
- the marker bodies become `marker_line(hm(r["ts_start"], zone) + places.at(r["location"], named), marker_note(...))`;
- the speech body becomes `f"{hm(row['ts_start'], zone)}{places.at(row['location'], named)} ({row['lang'] or '?'})
  {clip(row['text'], 250)}"`.

Everything else (budgets, references, order) stays as it is.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: PASS, every test (`tests/test_chapters.py` and `tests/test_recap.py` pin the one-camera formats).

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/pipeline/chapters.py brain/src/loopi_brain/pipeline/recap.py \
  brain/tests/test_prompt_places.py
git commit -m "feat(brain): chapter and recap prompts name where each line was recorded when several cameras record (P3c-1, D34)"
```

---

### Task 7: Amend the binding spec and the P3c design

The P3c design (§10) lists what the binding spec must gain; P3c-1 makes all those edits now (one place for the
decisions, before P3c-2 and P3c-3 build on them), plus the P4c note that D37 voids.

**Files:**
- Modify: `docs/superpowers/specs/2026-10-14-loopi-design.md`
- Modify: `docs/superpowers/specs/2026-10-19-p3c-multicam-design.md`

**Interfaces:** none (documentation).

- [ ] **Step 1: The binding spec**

In `docs/superpowers/specs/2026-10-14-loopi-design.md`:

1. The "Sửa đổi" line (line 5) gains at its end, before the final newline:
   `; 2026-10-19: P3c — nhiều camera trong LAN qua dịch vụ cameras, một agent cho mỗi camera mạng (D30), báo đang ghi cho camera mạng (D31), một bản mỗi đoạn video và retention bật mặc định 7 ngày (D32), chép lời từ mic nghe rõ nhất (D33), timeline gộp có tên vị trí (D34), vlog nhiều camera (D35), thêm camera bất kỳ theo loại (D36)`
2. The decisions table gains, right after the D29 row, the rows D30–D36 copied from the table in
   `docs/superpowers/specs/2026-10-19-p3c-multicam-design.md` §1 (same wording, same order: D30, D31, D32, D33, D34,
   D36, D35 → put them in numeric order D30…D36), each ending with
   ` (chi tiết: 2026-10-19-p3c-multicam-design.md)`. Keep D37 after them.
3. The D20 row's sentence "Job lưu trữ lúc 03:00 chỉ chạy sau khi người dùng bật trong Cài đặt (mặc định tắt)" becomes
   "Job lưu trữ lúc 03:00 chạy khi retention bật trong Cài đặt (mặc định bật từ D32; trước đó mặc định tắt)".
4. §4 "Nguyên tắc" item 3 becomes:
   `3. **Bất biến riêng tư:** không có chuyện đang ghi mà LED lại tắt. Không có tuỳ chọn nào để tắt LED. Ngoại lệ duy nhất là camera mạng (D31): không có LED do Loopi điều khiển, nên dấu hiệu là tín hiệu sẵn có của thiết bị cộng với web.`
5. §7.4 gains a last bullet:
   `- Camera mạng (P3c, D31) chạy agent với \`led.kind = "none"\`, chỉ hợp lệ khi nguồn ghi là \`stream\`; trang Thiết bị và dòng "Đang ghi: N camera" là dấu hiệu đang ghi.`
6. §9.5: the first bullet becomes
   `- Video giữ **một bản mỗi đoạn** (D32): MP4 (remux, không mã hoá lại) là bản giữ; H.264 thô bị xoá khi đã có MP4 và phân tích (keyframe) xong. MP4 giữ 7 ngày. Keyframe, transcript, caption, chương, recap và vlog giữ vĩnh viễn.`
   and in the second bullet "việc xoá hằng đêm chỉ chạy sau khi người dùng bật trong Cài đặt (mặc định tắt)" becomes
   "việc xoá hằng đêm chạy khi retention bật trong Cài đặt (mặc định bật, D32)".
7. §10.6: the "Hôm nay / Ngày" row becomes
   `| Hôm nay / Ngày | Timeline theo chương, keyframe, recap, việc cần làm; gộp mọi camera theo thời gian, mỗi mục có tên vị trí và có bộ lọc theo camera (D34) |`
   and the "Thiết bị" row gains, before its final ` |`, `, tên vị trí sửa được (D34); thêm camera mạng theo loại, "Thử kết nối", ước tính dung lượng 7 ngày (P3c-2, D36)`.
8. §14: after the `| **P3** |` row add
   `| **P3c** | Nhiều camera: camera IP, điện thoại, ESP32 (D30–D36; plan P3c-1 … P3c-4) | Như mục 8 của 2026-10-19-p3c-multicam-design.md |`
9. §16 Backlog gains at its end ` · Thiết bị Loopi đầy đủ chạy trên ESP32 (firmware mTLS, hàng đợi trên thẻ, gửi ảnh JPEG lên brain)`.

- [ ] **Step 2: The P3c design**

In `docs/superpowers/specs/2026-10-19-p3c-multicam-design.md`:
- the sentence in the paragraph after the decisions table that ends "khi có BLE presence (P4c), nó giới hạn ghi âm
  trên **mọi** camera." gets ` BLE presence đã chuyển vào backlog (D37, 2026-10-19), nên hiện không có giới hạn này.`
  appended;
- §6's last bullet "Ghi âm trên mọi camera theo quyết định của chủ dự án; P4c (BLE presence) sẽ giới hạn ghi âm trên
  mọi camera." becomes "Ghi âm trên mọi camera theo quyết định của chủ dự án. BLE presence (P4c) đã chuyển vào backlog
  (D37), nên ghi âm chỉ bị giới hạn bằng lịch, nút Pause và công tắc audio.";
- the "Trạng thái" line (line 4) gains `; P3c-1 có plan ngày 2026-10-19 (\`plans/2026-10-19-p3c1-storage-display.md\`)`.

- [ ] **Step 3: Check and commit**

Run: `grep -n "D3[0-7]" docs/superpowers/specs/2026-10-14-loopi-design.md | cut -c1-80` — D30–D37 each appear in the
table once.

```bash
git add docs/superpowers/specs/2026-10-14-loopi-design.md docs/superpowers/specs/2026-10-19-p3c-multicam-design.md
git commit -m "docs(spec): P3c decisions D30–D36 in the binding spec; one copy per segment and retention on (P3c-1)"
```

---

### Task 8: Deploy and accept (controller)

Done by the controller, not by an implementer subagent. It deletes data on the live brain, so it starts only after the
owner says yes.

- [ ] **Step 1: Ask the owner**

Ask, in Vietnamese, with numbers measured that day (`df -h /home/soul/loopi-data`; the live query below): deploying
P3c-1 (a) deletes the raw H.264 of every analysed segment that has its MP4 (≈ 39 GB on 2026-10-19; the MP4s stay and
play), within the first hour, and (b) turns retention on, so at the next 03:00 the MP4 and audio of every recording
older than 7 days are deleted for good (keyframes, transcripts, captions, chapters, recaps and vlogs stay). Wait for
an explicit yes. With a no, stop here: the branch stays undeployed.

```bash
cd /home/soul/loopi-p2/deploy && docker compose -p deploy exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "select kind, count(*), pg_size_pretty(sum(size_bytes)) from item where kind in ('"'"'video'"'"','"'"'audio'"'"') and ended_at < now() - interval '"'"'7 days'"'"' group by kind"'
```

- [ ] **Step 2: Back up, fast-forward, deploy**

```bash
cd /home/soul/loopi-p2/deploy && docker compose -p deploy exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > ~/loopi-backups/p3c1-$(date +%Y-%m-%d-%H%M).dump
git -C /home/soul/loopi-p2 merge --ff-only feat/p3c1
cd /home/soul/loopi-p2/deploy && docker compose -p deploy up -d --build && COMPOSE_PROJECT_NAME=deploy ./smoke.sh
```

(`feat/p3c1` contains `feat/phase2`; if the fast-forward is refused, stop and look at why.) Never `down -v`, never
prune.

- [ ] **Step 3: Check the deploy**

- `schema_migration` lists `009_one_copy.sql`, `010_retention_on.sql`, `011_device_location.sql`; the `retention`
  setting is `{"video_days": 7, "audio_days": 7, "enabled": true}`.
- Within 20 minutes `drop_raw_video` has run: `select count(*) from item where h264_deleted_at is not null` grows to
  the number of analysed segments; `du -sh /home/soul/loopi-data/media/raw` shrinks and `df` gains about as much; a
  video on "Chi tiết theo giờ" still plays (request its `/media/view/...mp4` through Caddy and expect 200).
- Name the Pi "Phòng khách" on Thiết bị (POST `/devices/<id>/location` with `HX-Request: true` from inside the api
  container, like the update requests of P4a, or in the browser); the card shows it; the Day page has no tags and
  no filter (one camera).
- The next morning: the 03:00 retention run deleted the recordings older than 7 days (`raw_files_gone_at` set on
  them), the Day page of those days still shows keyframes, transcripts, chapters and recaps, and the vlog of those
  days still plays.

- [ ] **Step 4: Measure (P3c design §8, item 6)**

On two mornings after the deploy, record in the ledger: `df -h /home/soul/loopi-data`, `du -sh` of `media/raw`,
`media/view`, `media/keyframes`, the GB added in the last 24 h, and `select count(*) from item where kind = 'video'
and h264_deleted_at is null and raw_deleted_at is null and analysis_state in ('done','skipped') and status =
'ready'` (expected ≈ 0: no raw H.264 kept for an analysed segment), and that no raw or MP4 file is older than 7 days
(`find /home/soul/loopi-data/media/raw /home/soul/loopi-data/media/view -type f -mtime +8 | wc -l` ≈ 0). Acceptance
6 passes when the SSD's free space is stable from one morning to the next and no video or audio file is older than
7 days.

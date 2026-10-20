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

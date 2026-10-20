"""One copy per video segment (P3c design §4.2, D32).

A video segment arrives as raw H.264; the API remuxes it to MP4 without re-encoding (``media.remux_item``) and the
worker extracts its keyframes from the raw file (``pipeline.video``). Once both are done (``status='ready'`` with a
``view_path``, ``analysis_state`` 'done' or 'skipped') the raw H.264 is only a second copy of the same picture: this
job deletes it and records ``h264_deleted_at``; the MP4 stays until retention. Nothing else reads the raw H.264 after
the analysis: the hours page, the vlog, forget and retention use the MP4 (by ``view_path`` and by its fixed name) and
count a missing raw file as gone.

A raw file is deleted only when its MP4 is on disk, not empty and flushed: the MP4 and its directory are fsynced
first (the directory once per batch), because after this the MP4 is the only copy; if an fsync fails the raw file
stays and the segment counts as kept. Rows are taken ``FOR UPDATE SKIP LOCKED`` (a row someone else holds, a forget
deleting it say, waits for the next run) and marked in the same transaction, after their raw files are gone; a run
that dies in between leaves the row unmarked with its raw file already gone, and the next run marks it (a missing raw
file counts as gone). Logs carry counts and ids only; the segments without a usable MP4 are one line per run.
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
UNUSABLE_IDS_LOGGED = 5

_DUE = (
    "kind = 'video' AND status = 'ready' AND view_path IS NOT NULL AND analysis_state IN ('done', 'skipped')"
    " AND raw_deleted_at IS NULL AND h264_deleted_at IS NULL"
)


@dataclass(frozen=True)
class OneCopyResult:
    dropped: int = 0  # segments whose raw H.264 is gone (their MP4 is the kept copy)
    kept: int = 0  # segments left with their raw file this run (no usable MP4 on disk, or the raw file stayed)


def _mp4_is_durable(media_root: Path, view_path: str, flushed_dirs: set[Path]) -> bool:
    """True when the MP4 is on disk, not empty and flushed, so the raw file may go (blocking). The MP4's directory is
    flushed once per batch (``flushed_dirs``): every MP4 in it was renamed before this run, so one flush covers them."""
    mp4 = media_root / view_path
    try:
        if os.path.getsize(mp4) <= 0:
            return False
        media.fsync_path(mp4)
        if mp4.parent not in flushed_dirs:
            media.fsync_path(mp4.parent)
            flushed_dirs.add(mp4.parent)
    except OSError:
        return False
    return True


def _drop(media_root: Path, rows: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """Delete the raw H.264 of each row whose MP4 is durable (blocking): (ids whose raw file is gone, ids kept, ids
    kept because their MP4 is missing, empty or could not be flushed). The last list is part of the second one."""
    gone, kept, unusable = [], [], []
    flushed_dirs: set[Path] = set()
    for row in rows:
        if not _mp4_is_durable(media_root, row["view_path"], flushed_dirs):
            kept.append(row["id"])
            unusable.append(row["id"])
            continue
        _, failed = media.remove_files(media_root, [row["raw_path"]])
        (kept if failed else gone).append(row["id"])
    return gone, kept, unusable


async def drop_raw_video(pool: AsyncConnectionPool, settings: Settings) -> OneCopyResult:
    """Delete the raw H.264 of every analysed segment that has its MP4, in batches; return the counts."""
    dropped = kept = 0
    skip: list[str] = []  # kept in this run: not taken again until the next one
    unusable: list[str] = []  # of those, the ones without a usable MP4
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
            gone, left, no_mp4 = await anyio.to_thread.run_sync(_drop, settings.media_root, rows)
            if gone:
                await conn.execute("UPDATE item SET h264_deleted_at = now() WHERE id = ANY(%s)", (gone,))
        skip.extend(left)
        unusable.extend(no_mp4)
        dropped += len(gone)
        kept += len(left)
    if unusable:  # one line per run, however many there are
        log.warning(
            "%d segments have no usable MP4 (missing, empty or not flushed); their raw H.264 stays; first ids: %s",
            len(unusable), ", ".join(unusable[:UNUSABLE_IDS_LOGGED]),
        )
    if dropped or kept:
        log.info("one copy per segment: %d raw H.264 files deleted, %d kept", dropped, kept)
    return OneCopyResult(dropped, kept)


@job_app.periodic(cron="*/10 * * * *")
@job_app.task(name=jobs.DROP_RAW_VIDEO, queue="light", queueing_lock="drop_raw_video")
async def drop_raw_video_job(timestamp: int | None = None) -> None:
    """Every 10 minutes: delete the raw H.264 of the segments that now have their MP4 and their keyframes."""
    ctx = get_context()
    await drop_raw_video(ctx.pool, ctx.settings)

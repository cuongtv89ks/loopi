"""Retention (spec §9.5, D20; §9.2 "03:00: áp dụng chính sách lưu trữ").

Every night at 03:00 local time — **while retention is on in Cài đặt** (on by default since D32; the page shows what
a run would delete) — raw recordings older than the kept days (7 and 7 by default) lose their raw file and the copy
the web plays (video MP4, normalised audio): only recordings whose analysis is finished, never markers. The item
row stays, marked ``raw_deleted_at`` so the pages say "đã hết hạn"; keyframes, thumbnails, transcripts, captions,
moments, chapters and recaps are never deleted here, nor the golden-set copies under LOOPI_EVAL_DIR (spec D21). The
same nightly run (on or off) retries the media files a "Quên khoảng thời gian" could not delete at once and sweeps the
golden copies whose sample is gone (``evaluation.golden.sweep``).

Order of work per batch: the rows are marked ``raw_deleted_at`` under their row locks (``FOR UPDATE SKIP LOCKED``:
a row someone else holds, say a reprocess resetting it, is left for the next night) and that is committed
**before** any file is deleted, so a reprocess never reads "raw is kept" for a file that is going. Once the files
are gone the row says so (``raw_files_gone_at``, ``view_path`` cleared). A recording whose files could not all be
deleted gets its mark removed again and is tried the next night, but only while its raw file is verifiably still
there (audio; a video's raw H.264 went right after its analysis, D32, so a video MP4 that cannot be deleted lands
in the next case instead); its raw file is only deleted once every copy of it is gone. A worker that dies between the commit and the
deletion leaves rows marked without ``raw_files_gone_at``, possibly with their raw file already unlinked: each run
(retention on) first finishes those, a batch at a time, whatever the kept days say now (the pages already show them
as expired). **Fail closed:** when nothing is known about a recording's files (an unexpected error on it, or a file
that could not be deleted while its raw file is already gone) its mark stays, so a row is never made to look kept
after its raw file is gone (a reprocess of its day would delete its transcript for nothing); it is not tried again in
the same run and the next run's sweep retries it. An error that escapes the whole batch leaves every mark as it is,
for that sweep too. Logs carry counts and ids only, never content.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import anyio
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .. import forget, media, store
from ..config import Settings
from ..days import local_now
from ..evaluation import golden
from . import jobs
from .context import get_context
from .jobs import job_app

log = logging.getLogger(__name__)
RETENTION_HOUR = 3
BATCH = 500
# Per run, for each kind and each phase (finishing what a dead run left, then the newly due): at most MAX_BATCHES
# batches of BATCH recordings; a bigger backlog takes more nights.
MAX_BATCHES = 40

# Finished recordings of one kind whose raw file is still kept and that ended before a cutoff. A video still
# 'stored' is not remuxed yet: the startup remux would write an MP4 for a row that is already expired.
_DUE = (
    "kind = %s AND raw_deleted_at IS NULL AND analysis_state IN ('done', 'skipped') AND ended_at < %s"
    " AND (kind <> 'video' OR status <> 'stored')"
)
# Recordings of one kind marked as expired whose files are not yet known to be gone (a run died in between).
_UNFINISHED = "kind = %s AND raw_deleted_at IS NOT NULL AND raw_files_gone_at IS NULL"


@dataclass(frozen=True)
class RetentionResult:
    video: int = 0  # video recordings whose raw file and MP4 are gone
    audio: int = 0  # audio recordings whose raw file and playback copy are gone
    files: int = 0  # files deleted
    files_failed: int = 0  # files that could not be deleted (their recordings are tried again next night)


class _Removed(NamedTuple):
    done: list[str]  # every file is gone
    # a file could not be deleted and the raw file is verifiably still there (audio, or a video one copy has not
    # reached yet): loses its mark again
    left: list[str]
    # state unknown (unexpected error, or a failure with the raw file already gone, as with every video after one
    # copy): keeps its mark and is retried by the _UNFINISHED phase of the next run
    stuck: list[str]
    deleted: int  # files deleted
    failed: int  # files that could not be deleted (an unexpected error on a recording counts as one)


def _remove(media_root: Path, rows: list[dict]) -> _Removed:
    """Delete the files of each row (blocking). The raw file goes last and only when every copy went."""
    out = _Removed([], [], [], 0, 0)
    for row in rows:
        try:
            files = media.item_files(row["kind"], row["raw_path"], row["view_path"])
            count, bad = media.remove_files(media_root, [rel for rel in files if rel != row["raw_path"]])
            if not bad:
                more, bad = media.remove_files(media_root, [row["raw_path"]])
                count += more
        except Exception:  # a bug, not a file problem: nothing is known about this row's files
            log.exception("could not delete the files of item %s; its mark stays, the next run tries again", row["id"])
            out.stuck.append(row["id"])
            out = out._replace(failed=out.failed + 1)
            continue
        out = out._replace(deleted=out.deleted + count, failed=out.failed + len(bad))
        if not bad:
            out.done.append(row["id"])
        elif os.path.lexists(media_root / row["raw_path"]):  # still there: it can be reprocessed, tried tomorrow
            out.left.append(row["id"])
        else:  # the raw file is already gone (a video's raw H.264 goes after its analysis, D32; or a dead run took
            # it): the row must keep saying so, and the _UNFINISHED phase retries the MP4 or the playback copy
            out.stuck.append(row["id"])
    return out


async def _claim(pool: AsyncConnectionPool, kind: str, cutoff: dt.datetime, skip: list[str]) -> list[dict]:
    """Mark up to BATCH due recordings of ``kind`` (none of ``skip``) as raw-deleted and commit; return them."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            f"SELECT id, kind, raw_path, view_path FROM item WHERE {_DUE} AND NOT (id = ANY(%s))"
            " ORDER BY ended_at, id LIMIT %s FOR UPDATE SKIP LOCKED",
            (kind, cutoff, skip, BATCH),
        )
        rows = await cur.fetchall()
        if rows:
            await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE id = ANY(%s)", ([r["id"] for r in rows],))
    return rows


async def _unfinished(pool: AsyncConnectionPool, kind: str, skip: list[str]) -> list[dict]:
    """Up to BATCH recordings of ``kind`` marked by a run that did not finish (none of ``skip``), oldest first."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT id, kind, raw_path, view_path FROM item WHERE {_UNFINISHED} AND NOT (id = ANY(%s))"
            " ORDER BY raw_deleted_at, id LIMIT %s",
            (kind, skip, BATCH),
        )
        return await cur.fetchall()


async def _delete(pool: AsyncConnectionPool, settings: Settings, rows: list[dict]) -> tuple[int, list[str], int, int]:
    """Delete the files of marked ``rows``: (recordings finished, ids not to take again in this run, files deleted,
    files left). Finished ones are recorded as gone and no longer point at a playable copy; the ``left`` ones lose
    their mark again; the ``stuck`` ones keep it. An error that escapes ``_remove`` (the worker dying, say) leaves
    every mark: the next run finishes those."""
    removed = await anyio.to_thread.run_sync(_remove, settings.media_root, rows)
    await _record(pool, removed.done, removed.left)
    return len(removed.done), [*removed.left, *removed.stuck], removed.deleted, removed.failed


async def _record(pool: AsyncConnectionPool, done: list[str], left: list[str]) -> None:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE item SET view_path = NULL, raw_files_gone_at = now() WHERE id = ANY(%s)", (done,)
        )
        await conn.execute("UPDATE item SET raw_deleted_at = NULL WHERE id = ANY(%s)", (left,))


async def preview_retention(pool: AsyncConnectionPool, now: dt.datetime | None = None) -> dict:
    """What a run would delete now with the kept days in Cài đặt (read only): per kind, recordings and bytes."""
    now = now or dt.datetime.now(dt.UTC)
    kept = await store.get_retention(pool)
    out = {}
    async with pool.connection() as conn:
        for kind, days in (("video", kept.video_days), ("audio", kept.audio_days)):
            cur = await conn.execute(
                f"SELECT count(*) AS recordings, coalesce(sum(size_bytes), 0)::bigint AS bytes FROM item WHERE {_DUE}",
                (kind, now - dt.timedelta(days=days)),
            )
            out[kind] = await cur.fetchone()
    return out


async def apply_retention(
    pool: AsyncConnectionPool, settings: Settings, now: dt.datetime | None = None
) -> RetentionResult:
    """Delete the raw files of finished recordings older than the kept days, in batches; return the counts.
    Nothing while retention is off in Cài đặt."""
    now = now or dt.datetime.now(dt.UTC)
    kept = await store.get_retention(pool)
    if not kept.enabled:
        log.info("retention is off (Cài đặt): no recording deleted")
        return RetentionResult()
    counts = {"video": 0, "audio": 0}
    files = files_failed = 0
    for kind, days in (("video", kept.video_days), ("audio", kept.audio_days)):
        cutoff = now - dt.timedelta(days=days)
        skip: list[str] = []  # recordings with a file left or in doubt: tried next night, not again in this run
        # What a dead run left comes first, then what is newly due; each in at most MAX_BATCHES batches of BATCH.
        for take in (lambda: _unfinished(pool, kind, skip), lambda: _claim(pool, kind, cutoff, skip)):
            for _ in range(MAX_BATCHES):
                rows = await take()
                if not rows:
                    break
                finished, not_again, deleted, failed = await _delete(pool, settings, rows)
                skip.extend(not_again)
                counts[kind] += finished
                files += deleted
                files_failed += failed
    result = RetentionResult(counts["video"], counts["audio"], files, files_failed)
    log.info("retention (video %d days, audio %d days): %s", kept.video_days, kept.audio_days, result)
    return result


async def retry_forgotten_files(pool: AsyncConnectionPool, settings: Settings) -> int:
    """Delete the media files "Quên khoảng thời gian" could not delete at once; return how many went. A path
    outside the media root is dropped from the audit row (never deleted, never retried). A row that cannot be
    handled (malformed paths, say) is logged and left as it is; it never stops the others or the retention run."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, pending_files FROM forget_log WHERE pending_files <> '[]'::jsonb ORDER BY id"
        )
        rows = await cur.fetchall()
    total = 0
    for row in rows:
        try:
            deleted, failed = await anyio.to_thread.run_sync(
                media.remove_files, settings.media_root, row["pending_files"]
            )
            keep = forget.retryable(settings.media_root, failed)
            async with pool.connection() as conn:
                await conn.execute("UPDATE forget_log SET pending_files = %s WHERE id = %s", (Jsonb(keep), row["id"]))
        except Exception:
            log.exception("could not retry the files of forget row %s", row["id"])
            continue
        total += deleted
    if rows:
        log.info("forgotten files: %d deleted on retry", total)
    return total


def due(timestamp: int | None, zone) -> bool:
    """Is the hourly tick scheduled at ``timestamp`` (default: now) the 03:00 one, in local time?"""
    at = dt.datetime.fromtimestamp(timestamp, dt.UTC) if timestamp is not None else None
    return local_now(zone, at).hour == RETENTION_HOUR


@job_app.periodic(cron="0 * * * *")
@job_app.task(name=jobs.APPLY_RETENTION, queue="light", queueing_lock="apply_retention")
async def apply_retention_job(timestamp: int | None = None) -> None:
    """Hourly tick (Procrastinate's cron runs in UTC, so the local 03:00 is found here): retry the files of a
    forgotten range and sweep the golden copies, on or off; then apply retention unless the owner turned it off in
    Cài đặt."""
    ctx = get_context()
    if due(timestamp, ctx.settings.zone):
        await retry_forgotten_files(ctx.pool, ctx.settings)
        try:
            _, left = await golden.sweep(ctx.pool, ctx.settings.eval_dir)
            if left:
                log.warning("%d golden copies could not be deleted; the next night's sweep tries again", len(left))
        except Exception:  # never stops retention
            log.exception("could not sweep the golden copies")
        await apply_retention(ctx.pool, ctx.settings)

"""Turn rows that still need work into jobs, exactly once (API fast path + periodic dispatcher)."""
from __future__ import annotations

import logging

from procrastinate import builtin_tasks
from procrastinate.exceptions import AlreadyEnqueued, UniqueViolation
from procrastinate.jobs import Job, Status
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
        return jobs.DETECT_SPEECH, "audio", None  # VAD + SNR; Whisper runs later under "asr" (grouping.group_speech)
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
    """Ask for an embedding run; one waiting run is enough, and runs never overlap (errors are logged)."""
    try:
        async with pool.connection() as conn, conn.transaction():
            await jobs.enqueue(conn, jobs.EMBED_MOMENTS, queue="light", lock="embed", queueing_lock="embed")
    except AlreadyEnqueued:
        pass
    except Exception:
        log.exception("could not defer the embedding job")


async def prune_failed_auto_runs(pool: AsyncConnectionPool) -> int:
    """Delete every failed run of each jobs.AUTO_RERUN_TASKS task except its newest; return how many went.

    These runs start again by themselves, so a persistent error (or a transient one, every few
    days) would add failed runs nobody can clear and push real failures off the dead-letter list.
    Events cascade, and Procrastinate's delete trigger unlinks a periodic defer that names the run.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM procrastinate_jobs WHERE id IN ("
            " SELECT id FROM (SELECT id, row_number() OVER (PARTITION BY task_name ORDER BY id DESC) AS newer"
            " FROM procrastinate_jobs WHERE status = 'failed' AND task_name = ANY(%s)) runs WHERE newer > 1)",
            (list(jobs.AUTO_RERUN_TASKS),),
        )
        return cur.rowcount


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
    uncapped fast path — never queues behind a whole backlog. Caption jobs that video analysis
    defers for its new keyframes bypass that cap: a fresh upload can still wait behind about
    ``max_queued`` analyses × keyframes per segment caption jobs (≈ 20 × 2 = 40 for 60 s segments,
    more for longer ones). That bound holds only because this tick waits its turn in the same FIFO.

    Caption jobs and the embedding run are deferred only if the model server can serve them right
    now (one health check per tick); otherwise that work is skipped with one log line, and no job
    is created just to fail. Each tick first prunes the failed runs of the automatic tasks
    (``prune_failed_auto_runs``).
    """
    await prune_failed_auto_runs(pool)
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


async def _drop_superseded(pool: AsyncConnectionPool, job: Job) -> int | None:
    """Delete stalled ``job``: a job with its queueing lock was deferred while it ran and waits to do
    the same work (the next embedding run or periodic tick, or the same row's job). A row that
    recorded ``job`` points at that waiting job instead — or is cleared if it already started, and
    the dispatcher adopts the row again. Returns the waiting job's id.
    """
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "SELECT id FROM procrastinate_jobs WHERE queueing_lock = %s AND status = 'todo'", (job.queueing_lock,)
        )
        row = await cur.fetchone()
        waiting = row["id"] if row else None
        target = job.task_kwargs
        if "item_id" in target:
            await conn.execute(
                "UPDATE item SET analysis_job_id = %s WHERE id = %s AND analysis_job_id = %s",
                (waiting, target["item_id"], job.id),
            )
        elif "keyframe_id" in target:
            await conn.execute(
                "UPDATE keyframe SET caption_job_id = %s WHERE id = %s AND caption_job_id = %s",
                (waiting, target["keyframe_id"], job.id),
            )
        # Events cascade; a periodic defer naming the job is unlinked by Procrastinate's trigger.
        await conn.execute("DELETE FROM procrastinate_jobs WHERE id = %s AND status = 'doing'", (job.id,))
    return waiting


async def recover_stalled(pool: AsyncConnectionPool) -> tuple[int, int]:
    """Jobs left 'doing' by a dead worker go back to 'todo' (Procrastinate's recipe) — unless they
    already used their attempts: a job that keeps killing the worker (native crash, out of memory)
    ends 'failed', on the dead-letter list, with the reason stored on its row.

    A stalled job cannot go back to 'todo' while another job with its queueing lock waits there;
    that job does the same work, so the stalled one is deleted (neither retried nor dead-lettered).

    Returns (retried, failed); a deleted job counts as retried, since its work runs again.
    """
    retried = failed = 0
    for job in await job_app.job_manager.get_stalled_jobs():
        if job.attempts < jobs.RETRY.max_attempts:
            try:
                await job_app.job_manager.retry_job(job)
            except UniqueViolation:
                waiting = await _drop_superseded(pool, job)
                log.warning("stalled job %s (%s) dropped: job %s waits to do the same work", job.id, job.task_name,
                            waiting)
            else:
                log.warning("retrying stalled job %s (%s)", job.id, job.task_name)
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

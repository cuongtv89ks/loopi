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
    "loopi_brain.pipeline.video",
    "loopi_brain.pipeline.moments",
    "loopi_brain.pipeline.caption",
    "loopi_brain.pipeline.asr",
    "loopi_brain.pipeline.chapters",
    "loopi_brain.pipeline.recap",
    "loopi_brain.pipeline.vlogs",
    "loopi_brain.pipeline.retention",
    "loopi_brain.pipeline.one_copy",
    "loopi_brain.evaluation.runner",
]

ANALYZE_VIDEO = "analyze_video"
CAPTION_KEYFRAME = "caption_keyframe"
TRANSCRIBE_AUDIO = "transcribe_audio"
INDEX_MARKER = "index_marker"
EMBED_MOMENTS = "embed_moments"
DISPATCH_PENDING = "dispatch_pending"
RETRY_STALLED = "retry_stalled_jobs"
REMOVE_OLD_JOBS = "remove_old_jobs"
PLAN_CHAPTERS = "plan_chapters"
BUILD_CHAPTERS = "build_chapters"
PLAN_RECAPS = "plan_recaps"
WRITE_RECAP = "write_recap"
APPLY_RETENTION = "apply_retention"
DROP_RAW_VIDEO = "drop_raw_video"
PLAN_VLOGS = "plan_vlogs"
WRITE_VLOG_SCRIPT = "write_vlog_script"
RENDER_VLOG = "render_vlog"
RUN_EVAL = "run_eval"
PIPELINE_TASKS = (ANALYZE_VIDEO, CAPTION_KEYFRAME, TRANSCRIBE_AUDIO, INDEX_MARKER, EMBED_MOMENTS)
# Runs that start again by themselves (the dispatcher asks for embedding runs; the scheduler starts
# each periodic tick; the hourly planner asks for each day's chapters again), so a failed one is never
# retried and only the newest failed one is kept.
AUTO_RERUN_TASKS = (
    EMBED_MOMENTS, DISPATCH_PENDING, RETRY_STALLED, REMOVE_OLD_JOBS, PLAN_CHAPTERS, BUILD_CHAPTERS, PLAN_RECAPS,
    PLAN_VLOGS, APPLY_RETENTION, DROP_RAW_VIDEO,
)
# The text models run one job at a time (chapters, recaps): each one takes most of the CPU. They also wait behind
# every live job (video, captions, ASR, markers, embeddings: priority 0) for a free worker slot, so recordings keep
# their 15-minute latency target while chapters and recaps are being written (spec §14 P2).
LLM_LOCK = "llm"
LLM_PRIORITY = -10

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
    priority: int | None = None,
    **task_kwargs,
) -> int:
    """Defer ``task`` inside the caller's transaction and return the job id (``priority``: higher runs first,
    default 0).

    If a 'todo' job already holds ``queueing_lock``, return that job's id instead.
    """
    try:
        async with conn.transaction():  # savepoint: a unique violation must not abort the caller
            deferrer = job_app.configure_task(
                task, queue=queue, lock=lock, queueing_lock=queueing_lock, priority=priority, connection=conn
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

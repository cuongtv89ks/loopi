"""Numbers for the /pipeline page: backlog, latency, model server, dead-letter jobs."""
from __future__ import annotations

import datetime as dt
import logging

import httpx
from procrastinate.exceptions import ConnectorException
from psycopg_pool import AsyncConnectionPool

from .. import store
from ..config import Settings
from ..health import HEARTBEAT_MAX_AGE_S
from ..providers.config import ProvidersConfigError, load_providers
from ..providers.router import build_router
from . import jobs
from .jobs import job_app

log = logging.getLogger(__name__)
FAILED_LIMIT = 100
TASK_LABELS = {
    jobs.ANALYZE_VIDEO: "Phân tích video",
    jobs.CAPTION_KEYFRAME: "Mô tả khung hình",
    jobs.DETECT_SPEECH: "Tìm tiếng nói",
    jobs.GROUP_SPEECH: "Ghép tiếng nói giữa các mic",
    jobs.TRANSCRIBE_AUDIO: "Nhận dạng giọng nói",
    jobs.INDEX_MARKER: "Ghi nhận đánh dấu",
    jobs.EMBED_MOMENTS: "Tạo embedding",
    jobs.DISPATCH_PENDING: "Điều phối xử lý",
    jobs.RETRY_STALLED: "Khôi phục job bị treo",
    jobs.REMOVE_OLD_JOBS: "Dọn job cũ",
    jobs.PLAN_CHAPTERS: "Lên lịch chia chương",
    jobs.BUILD_CHAPTERS: "Chia chương",
    jobs.PLAN_RECAPS: "Lên lịch recap",
    jobs.WRITE_RECAP: "Viết recap",
    jobs.PLAN_VLOGS: "Lên lịch vlog",
    jobs.WRITE_VLOG_SCRIPT: "Viết kịch bản vlog",
    jobs.RENDER_VLOG: "Dựng vlog",
    jobs.APPLY_RETENTION: "Dọn dữ liệu cũ",
    jobs.DROP_RAW_VIDEO: "Bỏ bản H.264 thô",
    jobs.RUN_EVAL: "Chạy đánh giá",
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
       COALESCE(j.args->>'item_id', j.args->>'keyframe_id', j.args->>'day') AS target,
       COALESCE(i.analysis_error, k.caption_error, e.error) AS error
  FROM procrastinate_jobs j
  LEFT JOIN item i ON i.id = j.args->>'item_id'
  LEFT JOIN keyframe k ON k.id = j.args->>'keyframe_id'
  LEFT JOIN day_job_error e ON e.task = j.task_name AND e.day::text = j.args->>'day'
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
# Likewise for keyframes whose caption job is dead-lettered.
WAITING_KEYFRAMES = """
SELECT count(*) AS n FROM keyframe k
  LEFT JOIN procrastinate_jobs j ON j.id = k.caption_job_id AND j.status = 'failed'
 WHERE k.caption IS NULL AND j.id IS NULL
"""
# jobs.AUTO_RERUN_TASKS have no retry button: the dispatcher requests a new embedding run every
# minute, the scheduler starts the next tick of each periodic task by itself (retry_stalled_jobs
# every 10 minutes, remove_old_jobs daily), and the hourly chapter planner asks for a day's build again
# (a chapter whose summary failed: after 6 hours). The value says when that happens.
EVERY_MINUTE = "tự chạy lại mỗi phút"
CHAPTER_RERUN = "tự chạy lại ở lần lên lịch chia chương (mỗi giờ; chương tóm tắt lỗi: sau 6 giờ)"
ON_SCHEDULE = "tự chạy lại theo lịch"
AUTO_RERUN = {
    task: EVERY_MINUTE if task in (jobs.EMBED_MOMENTS, jobs.DISPATCH_PENDING)
    else CHAPTER_RERUN if task == jobs.BUILD_CHAPTERS else ON_SCHEDULE
    for task in jobs.AUTO_RERUN_TASKS
}
NOT_RETRIED = jobs.AUTO_RERUN_TASKS


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
        cur = await conn.execute(WAITING_KEYFRAMES)
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
            {
                **r,
                "label": TASK_LABELS.get(r["task_name"], r["task_name"]),
                "retryable": r["task_name"] not in NOT_RETRIED,
                "rerun": AUTO_RERUN.get(r["task_name"]),
            }
            for r in await cur.fetchall()
        ]
    worker_alive = worker_seen is not None and dt.datetime.now(dt.UTC) - worker_seen < dt.timedelta(
        seconds=HEARTBEAT_MAX_AGE_S
    )
    return {
        "pending": pending,
        "keyframes_waiting": keyframes_waiting,
        "moments_waiting": moments_waiting,
        "latency": latency,
        "worker_alive": worker_alive,
        "failed": failed,
        "failed_total": failed_total,
    }


async def understanding_status(pool: AsyncConnectionPool, zone) -> dict:
    """The understanding jobs at a glance: chapters waiting for a summary, the newest recap, failed recaps,
    retention (days, on or off)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM chapter WHERE summary_signature IS DISTINCT FROM signature"
        )
        waiting = (await cur.fetchone())["n"]
        cur = await conn.execute("SELECT day, generated_at FROM day_recap ORDER BY generated_at DESC LIMIT 1")
        recap = await cur.fetchone()
        cur = await conn.execute(
            "SELECT count(*) AS n FROM procrastinate_jobs WHERE task_name = %s AND status = 'failed'",
            (jobs.WRITE_RECAP,),
        )
        recaps_failed = (await cur.fetchone())["n"]
        cur = await conn.execute(
            "SELECT day, generated_at FROM vlog WHERE state = 'rendered' ORDER BY generated_at DESC LIMIT 1"
        )
        vlog = await cur.fetchone()
        cur = await conn.execute(
            "SELECT count(*) AS n FROM procrastinate_jobs WHERE task_name = ANY(%s) AND status = 'failed'",
            ([jobs.WRITE_VLOG_SCRIPT, jobs.RENDER_VLOG],),
        )
        vlogs_failed = (await cur.fetchone())["n"]
    return {
        "chapters_waiting": waiting,
        "recaps_failed": recaps_failed,
        "last_recap": (
            {"day": recap["day"], "at": recap["generated_at"].astimezone(zone).strftime("%d/%m %H:%M")} if recap else None
        ),
        "vlogs_failed": vlogs_failed,
        "last_vlog": (
            {"day": vlog["day"], "at": vlog["generated_at"].astimezone(zone).strftime("%d/%m %H:%M")} if vlog else None
        ),
        "retention": await store.get_retention(pool),
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
    """Put a dead-lettered job back in its queue (it runs once more); False if it is not 'failed', or
    if it is a run that starts again by itself (NOT_RETRIED)."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT status, task_name FROM procrastinate_jobs WHERE id = %s", (job_id,))
        row = await cur.fetchone()
    if row is None or row["status"] != "failed" or row["task_name"] in NOT_RETRIED:
        return False
    try:
        await job_app.job_manager.retry_job_by_id_async(job_id, retry_at=dt.datetime.now(dt.UTC))
    except ConnectorException:  # retried by someone else in between
        log.info("job %s was no longer failed", job_id)
        return False
    return True


async def retry_all_failed(pool: AsyncConnectionPool) -> int:
    """Retry every dead-lettered job that has a retry button; return how many went back to the queue."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM procrastinate_jobs WHERE status = 'failed' AND NOT (task_name = ANY(%s)) ORDER BY id",
            (list(NOT_RETRIED),),
        )
        job_ids = [r["id"] for r in await cur.fetchall()]
    return sum([await retry_failed_job(pool, job_id) for job_id in job_ids])

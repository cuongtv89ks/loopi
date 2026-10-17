"""Making the daily vlog (spec §9.2 "23:30: … dựng vlog", §10.3, D25; P3 design §4.1, §4.5; rulings R4–R7, R9, R11):
a planner every 15 minutes; per day one script job (the text model's lock) and one render job (its own lock); the
vlog row and its two files; "Dựng lại vlog". A vlog is stored only if no part of its day was forgotten while it was
made (the forget count, read before and checked again under the day's lock)."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import shutil
import uuid
from pathlib import Path, PurePosixPath

import anyio
import procrastinate
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .. import media
from ..days import day_bounds_utc, local_now
from ..vlog import render
from ..vlog.material import vlog_material
from ..vlog.script import to_json, write_script
from . import jobs
from .context import PipelineContext, get_context
from .jobs import job_app
from .state import clear_day_error, forgotten, lock_day, raw_kept, record_day_error

log = logging.getLogger(__name__)

VLOG_LOCK = "vlog"
RENDER_RETRY = procrastinate.RetryStrategy(max_attempts=2, exponential_wait=60)  # at most 3 runs
RENDER_TIMEOUT_S = 1200.0
SECONDS_PER_SEGMENT = 0.25  # of timelapse per active one-minute recording
TIMELAPSE_MIN, TIMELAPSE_MAX = 10.0, 60.0
STILL_SECONDS, STILL_MAX = 3.0, 5.0
VLOG_MIN = 20.0
CATCH_UP = 1  # earlier days asked for per planner run (newest first); today never waits
TMP_DIR = "vlog/.tmp"
SPEECH_MAX = 120  # characters of a conversation's sentence under its picture

_STORE_SCRIPT = """
INSERT INTO vlog (day, state, script, model_version) VALUES (%s, 'scripted', %s, %s)
ON CONFLICT (day) DO UPDATE SET state = 'scripted', script = EXCLUDED.script, model_version = EXCLUDED.model_version,
    scripted_at = now(), stale = false, error = NULL, error_at = NULL"""
_STORE_EMPTY = """
INSERT INTO vlog (day, state) VALUES (%s, 'empty')
ON CONFLICT (day) DO UPDATE SET state = 'empty', script = NULL, model_version = NULL, source = NULL, path = NULL,
    duration_s = NULL, generated_at = NULL, scripted_at = now(), stale = false, error = NULL, error_at = NULL"""


class VlogFailed(Exception):
    """A render that failed; the message is fixed text (it is stored and shown)."""


def vlog_lock(day: dt.date) -> str:
    return f"vlog:{day.isoformat()}"


def vlog_rel(day: dt.date) -> str:
    return f"vlog/{day:%Y/%m/%d}/{day.isoformat()}.mp4"


def vtt_rel(path: str) -> str:
    return str(PurePosixPath(path).with_suffix(".vtt"))


def plan_seconds(n_segments: int, n_stills: int) -> tuple[float, float]:
    """(timelapse seconds, seconds per picture), D25: 60–90 s on a full day, not under VLOG_MIN when it can be."""
    timelapse = min(TIMELAPSE_MAX, max(TIMELAPSE_MIN, SECONDS_PER_SEGMENT * n_segments)) if n_segments else 0.0
    still = STILL_SECONDS
    if n_stills and timelapse + n_stills * still < VLOG_MIN:
        still = min(STILL_MAX, (VLOG_MIN - timelapse) / n_stills)
    if not n_stills and n_segments and timelapse < VLOG_MIN:
        timelapse = VLOG_MIN
    return timelapse, round(still, 3)


async def _enqueue(conn: AsyncConnection, task: str, day: dt.date, lock: str) -> int:
    return await jobs.enqueue(conn, task, queue="understand", lock=lock, priority=jobs.LLM_PRIORITY,
                              queueing_lock=vlog_lock(day), day=day.isoformat())


async def defer_script(pool: AsyncConnectionPool, day: dt.date) -> int:
    async with pool.connection() as conn, conn.transaction():
        return await _enqueue(conn, jobs.WRITE_VLOG_SCRIPT, day, jobs.LLM_LOCK)


async def defer_render(pool: AsyncConnectionPool, day: dt.date) -> int:
    async with pool.connection() as conn, conn.transaction():
        return await _enqueue(conn, jobs.RENDER_VLOG, day, VLOG_LOCK)


async def drop_failed_vlogs(conn: AsyncConnection, days: list[dt.date]) -> None:
    """In the caller's transaction: delete the failed vlog runs of ``days`` and their stored errors."""
    await conn.execute("DELETE FROM procrastinate_jobs WHERE status = 'failed' AND queueing_lock = ANY(%s)",
                       ([vlog_lock(day) for day in days],))
    await conn.execute("DELETE FROM day_job_error WHERE task = ANY(%s) AND day = ANY(%s)",
                       ([jobs.WRITE_VLOG_SCRIPT, jobs.RENDER_VLOG], days))


def _inside(root: Path, rel: str | PurePosixPath | None) -> Path | None:
    if not rel:
        return None
    try:
        return media.inside(root, rel)
    except ValueError:
        log.warning("a vlog input path leaves the media root; skipped")
        return None


async def write_vlog_script(ctx: PipelineContext, day: dt.date) -> str:
    """Script the day's vlog and ask for its render ('scripted'), or store it 'empty' (nothing active); 'skipped'
    when part of the day was forgotten meanwhile (nothing stored: the planner asks again)."""
    pool, zone = ctx.pool, ctx.settings.zone
    start, end = day_bounds_utc(day, zone)
    old_path = None
    try:
        async with pool.connection() as conn:
            generation = await forgotten(conn, start, end)
        material = await vlog_material(pool, day, zone)
        written = None if material.empty else await write_script(ctx.router, material, zone)
        async with pool.connection() as conn, conn.transaction():
            await lock_day(conn, day)
            if await forgotten(conn, start, end) != generation:
                log.info("vlog of %s: part of the day was forgotten while it was scripted; not stored", day)
                return "skipped"
            if written is None:
                cur = await conn.execute("SELECT path FROM vlog WHERE day = %s FOR UPDATE", (day,))
                old = await cur.fetchone()
                old_path = old["path"] if old else None
                await conn.execute(_STORE_EMPTY, (day,))
                state = "empty"
            else:
                await conn.execute(_STORE_SCRIPT, (day, Jsonb(to_json(written)), written.model_version))
                await _enqueue(conn, jobs.RENDER_VLOG, day, VLOG_LOCK)
                state = "scripted"
    except Exception as exc:
        await record_day_error(pool, jobs.WRITE_VLOG_SCRIPT, day, exc)
        raise
    if old_path:  # the day has nothing left to show: its earlier vlog goes
        media.remove_files(ctx.settings.media_root, [old_path, vtt_rel(old_path)])
    await clear_day_error(pool, jobs.WRITE_VLOG_SCRIPT, day)
    log.info("vlog of %s: %s (%d highlights)", day, state, len(written.highlights) if written else 0)
    return state


async def _sound(pool: AsyncConnectionPool, root: Path, item_id: str) -> Path | None:
    """The normalised playback copy of an audio item (D19), made now when missing (as the /audio route does), or
    None: raw audio expired or forgotten, file missing, or the copy could not be made."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT raw_path, raw_deleted_at FROM item WHERE id = %s AND kind = 'audio'",
                                 (item_id,))
        row = await cur.fetchone()
    if row is None or row["raw_deleted_at"] is not None:
        return None
    raw_rel = PurePosixPath(row["raw_path"])
    view_rel = media.audio_view_path(raw_rel)
    raw, view = _inside(root, raw_rel), _inside(root, view_rel)
    if raw is None or view is None or not raw.is_file():
        return None
    if not view.is_file():
        try:
            await media.normalise_audio(raw, view)
        except (media.MediaError, OSError) as exc:
            log.warning("vlog sound of %s skipped: %s", item_id, type(exc).__name__)
            return None
        if not await raw_kept(pool, item_id):  # forgotten or expired while the copy was made
            media.remove_files(root, [str(view_rel)])
            return None
    return view


async def _stills(pool: AsyncConnectionPool, root: Path, script: dict) -> list[render.Still]:
    stills = []
    for h in script.get("highlights", []):
        jpeg = _inside(root, h.get("jpeg"))
        if jpeg is None or not jpeg.is_file():
            continue  # its picture is gone (forgotten): the highlight is left out
        clip = await _sound(pool, root, h["audio_item"]) if h.get("audio_item") else None
        # The sentence of a conversation goes under the line only when it differs from it (the rule script's talk
        # line is the sentence itself: shown once).
        speech = h.get("speech")
        caption = h["line"] if not speech or speech.strip() == h["line"].strip() else f"{h['line']}\n“{speech[:SPEECH_MAX]}”"
        stills.append(render.Still(jpeg, dt.datetime.fromisoformat(h["at"]), caption, clip, h.get("audio_offset", 0.0)))
    return stills


def _failure(exc: BaseException) -> str:
    if isinstance(exc, render.RenderError):
        return f"render failed at {exc.step} (ffmpeg exit {exc.returncode})"
    if isinstance(exc, render.InvalidMedia):
        return "a picture or recording could not be read"
    return f"render took longer than {RENDER_TIMEOUT_S / 60:.0f} minutes"


async def _store_render(ctx: PipelineContext, day: dt.date, scripted_at: dt.datetime, generation: int,
                        result: render.RenderResult, source: str) -> bool:
    root, zone = ctx.settings.media_root, ctx.settings.zone
    start, end = day_bounds_utc(day, zone)
    rel = vlog_rel(day)
    final = media.inside(root, rel)
    async with ctx.pool.connection() as conn, conn.transaction():
        await lock_day(conn, day)
        if await forgotten(conn, start, end) != generation:
            log.info("vlog of %s: part of the day was forgotten while it rendered; not stored", day)
            return False
        cur = await conn.execute(
            "SELECT 1 FROM vlog WHERE day = %s AND state = 'scripted' AND scripted_at = %s FOR UPDATE", (day, scripted_at)
        )
        if await cur.fetchone() is None:
            log.info("vlog of %s: its script changed while it rendered; not stored", day)
            return False
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(result.out, final)
        os.replace(result.vtt, final.with_suffix(".vtt"))
        await conn.execute(
            "UPDATE vlog SET state = 'rendered', path = %s, source = %s, duration_s = %s, generated_at = now(),"
            " error = NULL, error_at = NULL WHERE day = %s",
            (rel, source, result.seconds, day),
        )
    return True


async def render_vlog(ctx: PipelineContext, day: dt.date) -> bool:
    """Render the day's scripted vlog and store it; False when there is nothing to render or it was not stored
    (forgotten or rescripted meanwhile). VlogFailed (fixed message, stored on the row) when the render fails."""
    pool, settings = ctx.pool, ctx.settings
    root, zone = settings.media_root, settings.zone
    start, end = day_bounds_utc(day, zone)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT state, script, scripted_at FROM vlog WHERE day = %s", (day,))
        row = await cur.fetchone()
        generation = await forgotten(conn, start, end)
    if row is None or row["state"] != "scripted":
        return False
    work = root / TMP_DIR / f"{day.isoformat()}-{uuid.uuid4().hex}"
    try:
        material = await vlog_material(pool, day, zone)
        stills = await _stills(pool, root, row["script"] or {})
        segments = [render.Segment(p, s.start) for s in material.segments if (p := _inside(root, s.path))]
        # Always passed: the renderer uses them only when no segment is usable (missing or broken MP4 files).
        frames = [render.Keyframe(p, f.at) for f in material.frames if (p := _inside(root, f.path))]
        timelapse, still_seconds = plan_seconds(len(segments), len(stills))
        work.mkdir(parents=True)
        try:
            async with asyncio.timeout(RENDER_TIMEOUT_S):
                result = await render.render(
                    segments=segments, stills=stills, keyframes=frames, target_seconds=timelapse,
                    workdir=work / "parts", out=work / "vlog.mp4", still_seconds=still_seconds,
                    title=(row["script"] or {}).get("title"),
                )
        except (render.RenderError, render.InvalidMedia, TimeoutError) as exc:
            log.warning("vlog of %s: %s", day, _failure(exc))
            raise VlogFailed(_failure(exc)) from None
        stored = await _store_render(ctx, day, row["scripted_at"], generation, result,
                                     "keyframes" if result.mode == "slideshow" else "video")
    except Exception as exc:
        message = str(exc) if isinstance(exc, VlogFailed) else None
        try:
            async with pool.connection() as conn:
                await conn.execute("UPDATE vlog SET error = %s, error_at = now() WHERE day = %s",
                                   (message or "render failed", day))
        except Exception:
            log.exception("could not record the vlog error of %s", day)
        await record_day_error(pool, jobs.RENDER_VLOG, day, exc)
        raise
    finally:
        await anyio.to_thread.run_sync(lambda: shutil.rmtree(work, ignore_errors=True))
    await clear_day_error(pool, jobs.RENDER_VLOG, day)
    if stored:
        log.info("vlog of %s rendered: %s, %.1f s, %d skipped inputs", day, result.mode, result.seconds,
                 len(result.skipped))
    return stored


async def request_rebuild(pool: AsyncConnectionPool, day: dt.date) -> int | None:
    """"Dựng lại vlog": drop the day's failed vlog runs and their errors, mark its vlog out of date (it stays until
    the new one is stored, R5), and ask for a new script now. Its job id, or None when it could not be queued."""
    async with pool.connection() as conn, conn.transaction():
        await drop_failed_vlogs(conn, [day])
        await conn.execute("UPDATE vlog SET stale = true WHERE day = %s", (day,))
    try:
        return await defer_script(pool, day)
    except Exception:
        log.exception("could not ask for the vlog of %s", day)
        return None


async def plan_vlogs(pool: AsyncConnectionPool, zone, now: dt.datetime | None = None) -> list[dt.date]:
    """Ask for the vlog of every day that has a recap and needs one (R6), and return those days:
    - no vlog yet, a stale one, or one scripted before the day's (non-stale) recap was last written;
    - a 'scripted' vlog with no job (a render lost in a crash): its render.
    Never a day whose vlog job waits, runs or failed; earlier days CATCH_UP per run, newest first."""
    today = local_now(zone, now).date()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT r.day, r.generated_at AS recap_at, r.stale AS recap_stale, v.state, v.scripted_at, v.stale
                 FROM day_recap r LEFT JOIN vlog v ON v.day = r.day
                WHERE r.day <= %(today)s
                  AND NOT EXISTS (SELECT 1 FROM procrastinate_jobs j
                                   WHERE j.queueing_lock = 'vlog:' || r.day::text
                                     AND j.status IN ('todo', 'doing', 'failed'))
                ORDER BY r.day DESC""",
            {"today": today},
        )
        rows = await cur.fetchall()
    asked: list[dt.date] = []
    earlier = 0
    for r in rows:
        if r["state"] == "scripted" and not r["stale"]:
            await defer_render(pool, r["day"])
            asked.append(r["day"])
            continue
        due = r["state"] is None or r["stale"] or (not r["recap_stale"] and r["scripted_at"] < r["recap_at"])
        if not due:
            continue
        if r["day"] < today:
            if earlier >= CATCH_UP:
                continue
            earlier += 1
        await defer_script(pool, r["day"])
        asked.append(r["day"])
    return asked


@job_app.periodic(cron="*/15 * * * *")
@job_app.task(name=jobs.PLAN_VLOGS, queue="light", queueing_lock="plan_vlogs")
async def plan_vlogs_job(timestamp: int | None = None) -> None:
    ctx = get_context()
    await plan_vlogs(ctx.pool, ctx.settings.zone)


# Each task's own lock and priority are the ones its defer helper passes, so a direct .defer behaves the same.
@job_app.task(name=jobs.WRITE_VLOG_SCRIPT, queue="understand", lock=jobs.LLM_LOCK, priority=jobs.LLM_PRIORITY,
              retry=jobs.RETRY)
async def write_vlog_script_job(day: str) -> None:
    await write_vlog_script(get_context(), dt.date.fromisoformat(day))


@job_app.task(name=jobs.RENDER_VLOG, queue="understand", lock=VLOG_LOCK, priority=jobs.LLM_PRIORITY,
              retry=RENDER_RETRY)
async def render_vlog_job(day: str) -> None:
    await render_vlog(get_context(), dt.date.fromisoformat(day))

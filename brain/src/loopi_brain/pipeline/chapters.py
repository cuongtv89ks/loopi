"""Chapters of a local day (spec §10.1; §9.2 "Mỗi giờ: chia chương cho phần dữ liệu mới").

Every hour the planner asks for a build of each day that may have changed. A build cuts the day's moments
into chapters (``segment.py``), keeps the settled ("closed") chapters as they are, recomputes only the open
tail, and has the chapter_summary model write a Vietnamese title and summary for each chapter whose
moments changed. A day is reopened (all of its chapters recomputed) when moments appear inside its closed
part, or when "Quên khoảng thời gian" or reprocessing changes it.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from procrastinate import JobContext
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from .. import devices, places
from ..days import day_bounds_utc, hm, local_now
from ..llm import ASR_CAVEAT, PICTURE_NOTE, AnswerInvalid, clip, clipped, marker_line, parse_answer, spread, within
from ..providers.base import ProviderError, ProviderUnavailable
from ..providers.router import Router
from . import jobs
from .context import PipelineContext, get_context
from .jobs import job_app
from .moments import marker_note
from .segment import ChapterParams, Point, closed_count, segment, signature, unit
from .state import clear_day_error, error_text, forgotten, lock_day, record_day_error

log = logging.getLogger(__name__)
OPEN_MIN_MOMENTS = 3  # the open chapter at the end of today gets a summary once it has this many moments
RECENT = dt.timedelta(hours=26)  # the planner rebuilds every day that got new moments this recently
CATCHUP_PER_RUN = 1  # days with moments but no chapter yet (analysed before P2b) built per hourly run, newest first
ERROR_COOLDOWN = dt.timedelta(hours=6)  # a chapter whose summary failed is not the reason for a build before this
TITLE_MAX = 80
SUMMARY_MAX = 600
SUMMARY_MAX_TOKENS = 400
RETRY_TEMPERATURE = 0.3
PROMPT_BUDGET = 7000  # characters of moment lines in one prompt (fits LOOPI_LLM_NUM_CTX 8192 with room)
MAX_PICTURE_LINES = 40
MAX_SPEECH_LINES = 60
MAX_MARKER_LINES = 20
SUMMARY_SYSTEM = (
    "You keep a personal diary, in Vietnamese, of what a still camera and a microphone in a home recorded. "
    f"{PICTURE_NOTE} {ASR_CAVEAT} "
    "Use only what the lines support; never invent people, names, places or events. "
    "Answer with JSON: title = a short Vietnamese title for this part of the day (at most 8 words); "
    "summary = 2 to 3 Vietnamese sentences about what happened in it."
)
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": TITLE_MAX},
        "summary": {"type": "string", "maxLength": SUMMARY_MAX},
    },
    "required": ["title", "summary"],
}


class ChapterSummary(BaseModel):
    """Over-long answers are cut, not rejected."""

    title: clipped(TITLE_MAX)
    summary: clipped(SUMMARY_MAX)


class ChapterSummaryFailed(Exception):
    """At least one chapter got no summary because the model answered badly (the job is retried)."""


@dataclass(frozen=True)
class BuildResult:
    chapters: int
    closed: int
    summarised: int


async def _pending_since(conn: AsyncConnection, start: dt.datetime, end: dt.datetime) -> dt.datetime | None:
    """Earliest moment of [start, end) whose analysis is still to come: a pending item, an uncaptioned
    keyframe or an unembedded moment (work on the dead-letter list does not count: it may never come)."""
    cur = await conn.execute(
        """SELECT least(
             (SELECT min(i.started_at) FROM item i
                LEFT JOIN procrastinate_jobs j ON j.id = i.analysis_job_id AND j.status = 'failed'
               WHERE i.analysis_state = 'pending' AND j.id IS NULL AND i.ended_at >= %(start)s AND i.started_at < %(end)s),
             (SELECT min(k.ts) FROM keyframe k
                LEFT JOIN procrastinate_jobs j ON j.id = k.caption_job_id AND j.status = 'failed'
               WHERE k.caption IS NULL AND j.id IS NULL AND k.ts >= %(start)s AND k.ts < %(end)s),
             (SELECT min(m.ts_start) FROM moment m
               WHERE m.embedding IS NULL AND m.ts_start >= %(start)s AND m.ts_start < %(end)s)
           ) AS t""",
        {"start": start, "end": end},
    )
    return (await cur.fetchone())["t"]


async def _points(conn: AsyncConnection, start: dt.datetime, end: dt.datetime, after: dt.datetime | None) -> list[Point]:
    cur = await conn.execute(
        "SELECT id, kind, ts_start, ts_end, embedding FROM moment"
        " WHERE ts_start >= %s AND ts_start < %s AND (%s::timestamptz IS NULL OR ts_start > %s)"
        " ORDER BY ts_start, id",
        (start, end, after, after),
    )
    return [
        Point(r["id"], r["kind"], r["ts_start"], max(r["ts_end"], r["ts_start"]),
              None if r["embedding"] is None else unit(r["embedding"].to_numpy()))
        for r in await cur.fetchall()
    ]


async def rebuild_chapters(
    pool: AsyncConnectionPool,
    day: dt.date,
    zone,
    params: ChapterParams = ChapterParams(),
    now: dt.datetime | None = None,
) -> tuple[int, int]:
    """Recompute the open chapters of ``day``; return (chapters, closed chapters).

    Closed chapters stay unless moments appeared or vanished inside them (then the whole day is
    recomputed). An open chapter keeps its row (and its summary) when its moments are unchanged, and keeps
    its row (with the old summary, now out of date) when it still starts at the same moment.
    """
    now = now or dt.datetime.now(dt.UTC)
    start, end = day_bounds_utc(day, zone)
    async with pool.connection() as conn, conn.transaction():
        # One writer of a day's chapters at a time (the "llm" job lock serialises jobs; this also covers a forget
        # and callers outside the worker).
        await lock_day(conn, day)
        cur = await conn.execute(
            "SELECT id, ts_start, ts_end, last_moment_at, moment_count, signature, closed FROM chapter"
            " WHERE day = %s ORDER BY ts_start",
            (day,),
        )
        chapters = await cur.fetchall()
        closed = [c for c in chapters if c["closed"]]
        frontier = closed[-1]["last_moment_at"] if closed else None
        if closed:
            cur = await conn.execute(
                "SELECT count(*) AS n FROM moment WHERE ts_start >= %s AND ts_start <= %s", (start, frontier)
            )
            if (await cur.fetchone())["n"] != sum(c["moment_count"] for c in closed):
                log.info("chapters of %s: moments changed inside closed chapters; recomputing the day", day)
                await conn.execute("UPDATE chapter SET closed = false WHERE day = %s", (day,))
                for c in chapters:
                    c["closed"] = False
                closed, frontier = [], None
        segments = segment(await _points(conn, start, end, frontier), params)
        pending = await _pending_since(conn, start, end)
        horizon = now - dt.timedelta(seconds=params.settle_s)
        if pending is not None:
            horizon = min(horizon, pending)
        settled = closed_count(segments, horizon, end)
        open_rows = [c for c in chapters if not c["closed"]]
        by_signature = {c["signature"]: c for c in open_rows}
        by_start = {c["ts_start"]: c for c in open_rows}
        kept: set[str] = set()
        for k, seg in enumerate(segments):
            sig = signature(seg)
            row = by_signature.get(sig)
            if row is None or row["id"] in kept:
                row = by_start.get(seg[0].start)
                if row is not None and row["id"] in kept:
                    row = None
            values = (seg[0].start, max(p.end for p in seg), seg[-1].start, len(seg), sig, k < settled)
            if row is None:
                chapter_id = devices.new_id()
                await conn.execute(
                    "INSERT INTO chapter (id, day, ts_start, ts_end, last_moment_at, moment_count, signature, closed)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (chapter_id, day, *values),
                )
            else:
                chapter_id = row["id"]
                current = (row["ts_start"], row["ts_end"], row["last_moment_at"], row["moment_count"],
                           row["signature"], row["closed"])
                if values != current:  # updated_at moves only when the chapter changes (the recap planner reads it)
                    await conn.execute(
                        "UPDATE chapter SET ts_start = %s, ts_end = %s, last_moment_at = %s, moment_count = %s,"
                        " signature = %s, closed = %s, updated_at = now() WHERE id = %s",
                        (*values, chapter_id),
                    )
            kept.add(chapter_id)
        await conn.execute(
            "DELETE FROM chapter WHERE day = %s AND NOT closed AND NOT (id = ANY(%s))", (day, list(kept))
        )
    return len(closed) + len(segments), len(closed) + settled


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


def summary_messages(lines: list[str], first: dt.datetime, end: dt.datetime, zone) -> list[dict]:
    return [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": f"Part of the day from {hm(first, zone)} to {hm(end, zone)}:\n" + "\n".join(lines)},
    ]


async def summarise_chapters(ctx: PipelineContext, day: dt.date, attempt: int = 0) -> tuple[int, list[str]]:
    """Write the title and summary of every chapter of ``day`` whose moments changed since its summary
    (closed chapters, and the open one once it has OPEN_MIN_MOMENTS moments). Returns (written, errors).

    A model server outage stops here without an error: the next hourly build continues. A bad answer is
    stored on its chapter and the other chapters go on; the next first attempt leaves that chapter alone for
    ERROR_COOLDOWN (as the planner does for older days), so a chapter whose answer is always invalid does not cost
    four model calls every hour, while the retries of the failing job (``attempt`` > 0) still try it at once.
    If part of the day is forgotten while the model writes, nothing more is stored: the forget asked for a new build.
    """
    zone = ctx.settings.zone
    start, end = day_bounds_utc(day, zone)
    async with ctx.pool.connection() as conn:
        generation = await forgotten(conn, start, end)
        cur = await conn.execute(
            "SELECT id, ts_start, ts_end, last_moment_at, signature FROM chapter"
            " WHERE day = %s AND summary_signature IS DISTINCT FROM signature"
            " AND (closed OR moment_count >= %s)"
            " AND (summary_error_at IS NULL OR summary_error_at < now() - %s) ORDER BY ts_start",
            (day, OPEN_MIN_MOMENTS, ERROR_COOLDOWN if attempt == 0 else dt.timedelta(0)),
        )
        rows = await cur.fetchall()
    written, errors = 0, []
    for row in rows:
        lines = await chapter_lines(ctx.pool, row["ts_start"], row["last_moment_at"], zone)
        try:
            result = await ctx.router.chat(
                "chapter_summary",
                summary_messages(lines, row["ts_start"], row["ts_end"], zone),
                json_schema=SUMMARY_SCHEMA,
                temperature=0.0 if attempt == 0 else RETRY_TEMPERATURE,
                max_tokens=SUMMARY_MAX_TOKENS,
            )
            answer = parse_answer(result.text, ChapterSummary)
        except ProviderUnavailable as exc:
            log.warning("chapters of %s: model server unavailable, summaries wait for the next build: %s", day, exc)
            break
        except (ProviderError, AnswerInvalid) as exc:
            errors.append(error_text(exc))
            async with ctx.pool.connection() as conn:
                await conn.execute(
                    "UPDATE chapter SET summary_error = %s, summary_error_at = now() WHERE id = %s",
                    (errors[-1], row["id"]),
                )
            continue
        async with ctx.pool.connection() as conn, conn.transaction():
            await lock_day(conn, day)
            if await forgotten(conn, start, end) != generation:
                log.info("chapters of %s: part of the day was forgotten during the summary; not stored", day)
                break
            await conn.execute(
                "UPDATE chapter SET title = %s, summary = %s, model_version = %s, summary_signature = %s,"
                " summary_error = NULL, summary_error_at = NULL, updated_at = now() WHERE id = %s AND signature = %s",
                (answer.title, answer.summary, result.model_version, row["signature"], row["id"], row["signature"]),
            )
        written += 1
    return written, errors


async def build_chapters(
    ctx: PipelineContext,
    day: dt.date,
    attempt: int = 0,
    params: ChapterParams = ChapterParams(),
    now: dt.datetime | None = None,
) -> BuildResult:
    """Rebuild the chapters of ``day`` and summarise the ones that changed."""
    try:
        chapters, closed = await rebuild_chapters(ctx.pool, day, ctx.settings.zone, params, now)
        summarised, errors = await summarise_chapters(ctx, day, attempt)
        if errors:
            raise ChapterSummaryFailed(f"{len(errors)} chapter summaries failed; last: {errors[-1]}")
    except Exception as exc:
        await record_day_error(ctx.pool, jobs.BUILD_CHAPTERS, day, exc)
        raise
    await clear_day_error(ctx.pool, jobs.BUILD_CHAPTERS, day)
    log.info("chapters of %s: %d (%d closed), %d summaries written", day, chapters, closed, summarised)
    return BuildResult(chapters, closed, summarised)


async def defer_build(pool: AsyncConnectionPool, day: dt.date, zone) -> int | None:
    """Ask for a build of ``day`` if it has moments or chapters; one waiting build per day (errors are logged)."""
    start, end = day_bounds_utc(day, zone)
    try:
        async with pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT EXISTS (SELECT 1 FROM moment WHERE ts_start >= %s AND ts_start < %s)"
                " OR EXISTS (SELECT 1 FROM chapter WHERE day = %s) AS needed",
                (start, end, day),
            )
            if not (await cur.fetchone())["needed"]:
                return None
            return await jobs.enqueue(
                conn, jobs.BUILD_CHAPTERS, queue="understand", lock=jobs.LLM_LOCK, priority=jobs.LLM_PRIORITY,
                queueing_lock=f"chapters:{day.isoformat()}", day=day.isoformat(),
            )
    except Exception:
        log.exception("could not ask for the chapters of %s", day)
        return None


async def plan_chapters(
    pool: AsyncConnectionPool, router: Router, zone, now: dt.datetime | None = None
) -> list[dt.date]:
    """The hourly planner; returns the days it asked a build for:
    - today, yesterday, every day that got moments in the last 26 hours (a backlog being analysed) and every day
      with open chapters;
    - while the chapter_summary model can run: every day with a chapter still waiting for its summary (an outage
      or a failure on an older, settled day), except chapters whose summary failed less than 6 hours ago;
    - one day with moments but no chapter at all (days analysed before P2b was installed), the newest first.
    """
    now = now or dt.datetime.now(dt.UTC)
    today = local_now(zone, now).date()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT (ts_start AT TIME ZONE %s)::date AS day FROM moment WHERE created_at > %s"
            " UNION SELECT day FROM chapter WHERE NOT closed",
            (zone.key, now - RECENT),
        )
        days = {r["day"] for r in await cur.fetchall()} | {today, today - dt.timedelta(days=1)}
        if router.task_ready("chapter_summary", await router.health()):
            cur = await conn.execute(
                "SELECT DISTINCT day FROM chapter WHERE summary_signature IS DISTINCT FROM signature"
                " AND (closed OR moment_count >= %s) AND (summary_error_at IS NULL OR summary_error_at < %s)",
                (OPEN_MIN_MOMENTS, now - ERROR_COOLDOWN),
            )
            days |= {r["day"] for r in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT d.day FROM (SELECT DISTINCT (ts_start AT TIME ZONE %s)::date AS day FROM moment) d"
            " WHERE NOT EXISTS (SELECT 1 FROM chapter c WHERE c.day = d.day) ORDER BY d.day DESC",
            (zone.key,),
        )
        unbuilt = [r["day"] for r in await cur.fetchall() if r["day"] not in days]
        days |= set(unbuilt[:CATCHUP_PER_RUN])
    return [day for day in sorted(days) if await defer_build(pool, day, zone) is not None]


@job_app.periodic(cron="5 * * * *")
@job_app.task(name=jobs.PLAN_CHAPTERS, queue="light", queueing_lock="plan_chapters")
async def plan_chapters_job(timestamp: int | None = None) -> None:
    ctx = get_context()
    await plan_chapters(ctx.pool, ctx.router, ctx.settings.zone)


# The task's own priority is the same as the one ``defer_build`` passes, so a direct ``.defer`` never runs a chapter
# build ahead of live work (or, in Procrastinate 3.10, ahead of every queued recap behind the same lock) either.
@job_app.task(
    name=jobs.BUILD_CHAPTERS, queue="understand", lock=jobs.LLM_LOCK, priority=jobs.LLM_PRIORITY, retry=jobs.RETRY,
    pass_context=True,
)
async def build_chapters_job(context: JobContext, day: str) -> None:
    await build_chapters(get_context(), dt.date.fromisoformat(day), attempt=context.job.attempts)

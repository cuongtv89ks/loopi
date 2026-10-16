"""Reprocess one local day (spec §12 "Đổi model: ... có lệnh xử lý lại theo ngày").

Each choice resets what the pipeline derived, so the usual machinery does the work again with the models now
configured: the dispatcher re-captions keyframes and re-transcribes audio items whose raw file still exists,
the chapter planner rebuilds the reopened chapters, and the recap planner rewrites the recap once the day has
settled (or at once, when the recap itself is chosen).
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from .days import day_bounds_utc
from .pipeline import chapters as chapter_rules
from .pipeline import recap as recap_rules
from .pipeline.state import lock_day

log = logging.getLogger(__name__)

CHOICES = ("captions", "transcripts", "chapters", "recap")


@dataclass(frozen=True)
class ReprocessResult:
    keyframes: int = 0  # keyframes to caption again
    audio_items: int = 0  # recordings to transcribe again
    expired_audio: int = 0  # recordings whose raw file retention already removed (not transcribed again)
    chapters: int = 0  # chapters reopened
    recap: bool = False  # a recap exists and was marked out of date


async def reprocess_day(pool: AsyncConnectionPool, day: dt.date, zone, what: set[str]) -> ReprocessResult:
    unknown = what - set(CHOICES)
    if not what or unknown:
        raise ValueError(f"choose among {', '.join(CHOICES)} (got {sorted(what)})")
    try:
        start, end = day_bounds_utc(day, zone)
    except OverflowError as exc:  # the first and last days of the calendar have no UTC bounds
        raise ValueError(f"day {day} is out of range") from exc
    keyframes = audio_items = expired = 0
    async with pool.connection() as conn, conn.transaction():
        # One writer of a day's derived text at a time: a chapter rebuild or a recap write that read the day before
        # the reset must not write over it, and "Quên khoảng thời gian" relies on the same lock.
        await lock_day(conn, day)
        if "captions" in what:
            await conn.execute(
                "DELETE FROM moment WHERE kind = 'visual' AND keyframe_id IN"
                " (SELECT id FROM keyframe WHERE ts >= %s AND ts < %s)",
                (start, end),
            )
            cur = await conn.execute(
                "UPDATE keyframe SET caption = NULL, caption_model = NULL, captioned_at = NULL,"
                " caption_job_id = NULL, caption_error = NULL WHERE ts >= %s AND ts < %s",
                (start, end),
            )
            keyframes = cur.rowcount
        if "transcripts" in what:
            cur = await conn.execute(
                "SELECT id, raw_deleted_at FROM item WHERE kind = 'audio' AND analysis_state = 'done'"
                " AND started_at >= %s AND started_at < %s FOR UPDATE",
                (start, end),
            )
            rows = await cur.fetchall()
            redo = [r["id"] for r in rows if r["raw_deleted_at"] is None]
            expired = len(rows) - len(redo)
            # Moments go with their transcript segments (ON DELETE CASCADE).
            await conn.execute("DELETE FROM transcript_segment WHERE item_id = ANY(%s)", (redo,))
            await conn.execute(
                "UPDATE item SET analysis_state = 'pending', analysis_job_id = NULL, analysis_error = NULL,"
                " analyzed_at = NULL WHERE id = ANY(%s)",
                (redo,),
            )
            audio_items = len(redo)
        reopened = 0
        if what & {"captions", "transcripts", "chapters"}:  # the day's moments (or their summaries) change
            cur = await conn.execute(
                "UPDATE chapter SET closed = false,"
                " summary_signature = CASE WHEN %s THEN NULL ELSE summary_signature END WHERE day = %s",
                ("chapters" in what, day),
            )
            reopened = cur.rowcount
        cur = await conn.execute("UPDATE day_recap SET stale = true WHERE day = %s", (day,))
        stale = cur.rowcount > 0
    if "chapters" in what:
        await chapter_rules.defer_build(pool, day, zone)
    if "recap" in what:
        try:
            await recap_rules.defer_recap(pool, day)
        except Exception:  # the reset is committed; the planner writes a stale recap once the day has settled
            log.exception("could not ask for the recap of %s", day)
    return ReprocessResult(keyframes, audio_items, expired, reopened, stale)

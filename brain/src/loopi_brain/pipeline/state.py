"""Row state shared by every stage: finish an item, store a job's error on its row, check a row still exists.

Procrastinate keeps no error text, so each task writes the last 500 characters of its
error onto the row it works on before re-raising, and clears it on success. A row deleted
before or during its job (e.g. by the smoke test's cleanup) makes the job a no-op instead.
"""
from __future__ import annotations

import datetime as dt
import logging

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)
ERROR_TAIL = 500


def error_text(exc: BaseException) -> str:
    """``Type: message``, at most 500 characters; long messages keep their end (ffmpeg puts the cause last)."""
    name = type(exc).__name__
    return f"{name}: {str(exc)[-(ERROR_TAIL - len(name) - 2):]}"


async def _store(pool: AsyncConnectionPool, sql: str, row_id: str, message: str) -> None:
    try:
        async with pool.connection() as conn:
            await conn.execute(sql, (message, row_id))
    except Exception:
        log.exception("could not record pipeline error for %s", row_id)


async def record_item_error(pool: AsyncConnectionPool, item_id: str, exc: BaseException) -> None:
    await _store(pool, "UPDATE item SET analysis_error = %s WHERE id = %s", item_id, error_text(exc))


async def record_keyframe_error(pool: AsyncConnectionPool, keyframe_id: str, exc: BaseException) -> None:
    await _store(pool, "UPDATE keyframe SET caption_error = %s WHERE id = %s", keyframe_id, error_text(exc))


async def finish_item(pool: AsyncConnectionPool, item_id: str, state: str) -> None:
    """Mark a pending item 'done' or 'skipped' and clear any error left by earlier attempts."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE item SET analysis_state = %s, analyzed_at = now(), analysis_error = NULL"
            " WHERE id = %s AND analysis_state = 'pending'",
            (state, item_id),
        )


async def lock_pending_item(conn: AsyncConnection, item_id: str) -> bool:
    """Lock the item row inside the caller's transaction; true if it still exists and is pending."""
    cur = await conn.execute(
        "SELECT 1 FROM item WHERE id = %s AND analysis_state = 'pending' FOR UPDATE", (item_id,)
    )
    return await cur.fetchone() is not None


async def mark_item_done(conn: AsyncConnection, item_id: str) -> None:
    """Mark the item 'done' and clear its error, in the caller's transaction (which holds the row lock)."""
    await conn.execute(
        "UPDATE item SET analysis_state = 'done', analyzed_at = now(), analysis_error = NULL WHERE id = %s",
        (item_id,),
    )


async def item_exists(pool: AsyncConnectionPool, item_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM item WHERE id = %s", (item_id,))
        return await cur.fetchone() is not None


async def raw_kept(pool: AsyncConnectionPool, item_id: str) -> bool:
    """Does the item exist with its raw file not marked as removed by retention?"""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM item WHERE id = %s AND raw_deleted_at IS NULL", (item_id,))
        return await cur.fetchone() is not None


async def keyframe_exists(pool: AsyncConnectionPool, keyframe_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM keyframe WHERE id = %s", (keyframe_id,))
        return await cur.fetchone() is not None


async def record_day_error(pool: AsyncConnectionPool, task: str, day: dt.date, exc: BaseException) -> None:
    """Store the last error of a per-day job (chapters, recap) for the dead-letter list."""
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO day_job_error (task, day, error) VALUES (%s, %s, %s)"
                " ON CONFLICT (task, day) DO UPDATE SET error = EXCLUDED.error, at = now()",
                (task, day, error_text(exc)),
            )
    except Exception:
        log.exception("could not record the %s error of %s", task, day)


async def lock_day(conn: AsyncConnection, day: dt.date) -> None:
    """Serialise, for one local day and inside the caller's transaction, everything that writes text derived from
    that day (chapters, summaries, the recap) with "Quên khoảng thời gian"."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"day:{day.isoformat()}",))


async def forgotten(conn: AsyncConnection, start: dt.datetime, end: dt.datetime) -> int:
    """How many "Quên khoảng thời gian" requests overlap [start, end): read before a model call and again (under
    ``lock_day``) before writing its answer; a different number means the material may hold forgotten text."""
    cur = await conn.execute(
        "SELECT count(*) AS n FROM forget_log WHERE range_start < %s AND range_end > %s", (end, start)
    )
    return (await cur.fetchone())["n"]


async def clear_day_error(pool: AsyncConnectionPool, task: str, day: dt.date) -> None:
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM day_job_error WHERE task = %s AND day = %s", (task, day))

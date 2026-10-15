"""Row state shared by every stage: finish an item, store a job's error on its row, check a row still exists.

Procrastinate keeps no error text, so each task writes the last 500 characters of its
error onto the row it works on before re-raising, and clears it on success. A row deleted
before or during its job (e.g. by the smoke test's cleanup) makes the job a no-op instead.
"""
from __future__ import annotations

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


async def keyframe_exists(pool: AsyncConnectionPool, keyframe_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM keyframe WHERE id = %s", (keyframe_id,))
        return await cur.fetchone() is not None

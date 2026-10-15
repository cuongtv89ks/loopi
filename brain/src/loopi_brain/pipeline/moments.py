"""Marker moments and text embeddings of moments (spec §9.2 "Hợp nhất")."""
from __future__ import annotations

import logging

from pgvector import Vector

from .. import devices
from ..providers.base import ProviderUnavailable
from . import dispatch, jobs
from .context import PipelineContext, get_context
from .jobs import job_app
from .state import mark_item_done, record_item_error

log = logging.getLogger(__name__)
MARKER_TEXT = "Marked moment"
MARKER_VERSION = "loopi:marker-v1"
EMBED_BATCH = 32


def marker_note(meta: dict) -> str | None:
    """The note a marker carries (stripped), or None: the device's button sends ``{}``."""
    note = meta.get("note")
    return note.strip() if isinstance(note, str) and note.strip() else None


def marker_text(meta: dict) -> str:
    return marker_note(meta) or MARKER_TEXT


async def _index(ctx: PipelineContext, item_id: str) -> bool:
    pool = ctx.pool
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "SELECT device_id, kind, started_at, ended_at, meta, analysis_state FROM item WHERE id = %s FOR UPDATE",
            (item_id,),
        )
        item = await cur.fetchone()
        if item is None or item["kind"] != "marker" or item["analysis_state"] != "pending":
            return False
        await conn.execute("DELETE FROM moment WHERE item_id = %s AND kind = 'marker'", (item_id,))
        await conn.execute(
            "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, item_id, model_version)"
            " VALUES (%s, %s, %s, %s, 'marker', %s, %s, %s)",
            (devices.new_id(), item["device_id"], item["started_at"], item["ended_at"], marker_text(item["meta"]),
             item_id, MARKER_VERSION),
        )
        await mark_item_done(conn, item_id)
    await dispatch.defer_embed(pool)
    return True


async def index_marker(ctx: PipelineContext, item_id: str) -> bool:
    """Turn a pending marker item into one 'marker' moment; False if there was nothing to do."""
    try:
        return await _index(ctx, item_id)
    except Exception as exc:
        await record_item_error(ctx.pool, item_id, exc)
        raise


async def embed_pending(ctx: PipelineContext, batch: int = EMBED_BATCH) -> int:
    """Embed every moment that has no embedding yet, ``batch`` at a time; return how many were embedded.

    Documents carry no instruction prefix (queries will, in P2b). A model server outage is logged
    and ends the run: the dispatcher asks for another run once the server is back.
    """
    done = 0
    while True:
        async with ctx.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, text FROM moment WHERE embedding IS NULL ORDER BY created_at, id LIMIT %s", (batch,)
            )
            rows = await cur.fetchall()
        if not rows:
            return done
        try:
            result = await ctx.router.embed("embed", [r["text"] for r in rows])
        except ProviderUnavailable as exc:
            log.warning("embedding paused, model server unavailable: %s", exc)
            return done
        async with ctx.pool.connection() as conn, conn.transaction():
            for row, vector in zip(rows, result.vectors, strict=True):
                await conn.execute(
                    "UPDATE moment SET embedding = %s, embed_model = %s WHERE id = %s AND embedding IS NULL",
                    (Vector(vector), result.model_version, row["id"]),
                )
        done += len(rows)


@job_app.task(name=jobs.INDEX_MARKER, queue="light", retry=jobs.RETRY)
async def index_marker_job(item_id: str) -> None:
    await index_marker(get_context(), item_id)


# Deferred when moments are written, and by the dispatcher every minute while moments wait and the
# embedding model is available (so an outage creates no job at all).
@job_app.task(name=jobs.EMBED_MOMENTS, queue="light", queueing_lock="embed", retry=jobs.RETRY)
async def embed_moments_job() -> None:
    await embed_pending(get_context())

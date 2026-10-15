"""analyze_video: pick the new keyframes of one video segment and store them (spec §9.2 steps 3-4)."""
from __future__ import annotations

import logging
from pathlib import Path

from .. import devices
from . import dispatch, jobs
from .context import PipelineContext, get_context
from .frames import (
    KeyframeParams,
    extract_jpeg,
    keyframe_rel_path,
    read_small_frames,
    select_keyframes,
    to_signed64,
    to_unsigned64,
)
from .jobs import job_app
from .state import error_text, finish_item, item_exists, lock_pending_item, mark_item_done, record_item_error

log = logging.getLogger(__name__)


async def _analyze(ctx: PipelineContext, item_id: str) -> list[str]:
    pool, settings = ctx.pool, ctx.settings
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT device_id, kind, started_at, raw_path, status, meta, analysis_state FROM item WHERE id = %s",
            (item_id,),
        )
        item = await cur.fetchone()
        if item is None or item["kind"] != "video" or item["analysis_state"] != "pending":
            return []
        cur = await conn.execute("SELECT count(*) AS n FROM keyframe WHERE item_id = %s", (item_id,))
        already = (await cur.fetchone())["n"]
        cur = await conn.execute(
            "SELECT ts, dhash FROM keyframe WHERE device_id = %s AND ts < %s ORDER BY ts DESC LIMIT 1",
            (item["device_id"], item["started_at"]),
        )
        previous = await cur.fetchone()
    if already:  # a previous run committed its keyframes; only the state is missing
        await finish_item(pool, item_id, "done")
        return []
    raw = settings.media_root / item["raw_path"]
    if item["status"] == "error" or not raw.is_file():
        await finish_item(pool, item_id, "skipped")
        return []

    fps = int(item["meta"].get("video_fps", settings.default_video_fps))
    frames = await read_small_frames(raw, fps)
    picks = select_keyframes(
        frames, fps, item["started_at"],
        to_unsigned64(previous["dhash"]) if previous else None,
        previous["ts"] if previous else None,
        KeyframeParams.from_settings(settings),
    )
    written: list[Path] = []
    rows = []
    stored = False
    try:
        for pick in picks:
            keyframe_id = devices.new_id()
            rel = keyframe_rel_path(item["device_id"], pick.ts, keyframe_id)
            await extract_jpeg(raw, fps, pick.index, settings.media_root / rel)
            written.append(settings.media_root / rel)
            rows.append((keyframe_id, item_id, item["device_id"], pick.ts, pick.index, str(rel),
                         to_signed64(pick.dhash), pick.luma))
        async with pool.connection() as conn, conn.transaction():
            if await lock_pending_item(conn, item_id):  # still there and still pending
                for row in rows:
                    await conn.execute(
                        "INSERT INTO keyframe (id, item_id, device_id, ts, frame_index, path, dhash, luma)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        row,
                    )
                await mark_item_done(conn, item_id)
                stored = True
    finally:
        if not stored:  # failed, or the item was deleted meanwhile: leave no JPEG behind
            for path in written:
                path.unlink(missing_ok=True)
    if not stored:
        log.info("item %s was deleted during its analysis; nothing stored", item_id)
        return []
    keyframe_ids = [row[0] for row in rows]
    log.info("item %s: %d of %d frames are keyframes", item_id, len(keyframe_ids), len(frames))
    await dispatch.defer_keyframes(pool, keyframe_ids)
    return keyframe_ids


async def analyze_video(ctx: PipelineContext, item_id: str) -> list[str]:
    """Select and store the keyframes of a pending video item; return the new keyframe ids."""
    try:
        return await _analyze(ctx, item_id)
    except Exception as exc:
        if not await item_exists(ctx.pool, item_id):  # deleted while we worked (e.g. its raw file went first)
            log.info("item %s was deleted during its analysis: %s", item_id, error_text(exc))
            return []
        await record_item_error(ctx.pool, item_id, exc)
        raise


@job_app.task(name=jobs.ANALYZE_VIDEO, queue="video", retry=jobs.RETRY)
async def analyze_video_job(item_id: str) -> None:
    await analyze_video(get_context(), item_id)

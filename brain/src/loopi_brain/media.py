"""Browser-playable views of raw media (raw H.264 -> MP4 without re-encoding)."""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from pathlib import Path, PurePosixPath

from psycopg_pool import AsyncConnectionPool

from .config import Settings

log = logging.getLogger(__name__)
STDERR_TAIL = 500
FFMPEG_TIMEOUT_S = 300.0


class MediaError(Exception):
    """ffmpeg failed to process a file."""


# Assumption: raw input has no VUI timing (Pi encoder output), so -framerate sets the pace;
# streams that carry VUI timing (e.g. libx264) make ffmpeg ignore -framerate.
async def remux_to_mp4(src: Path, dst: Path, fps: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.stem}.{uuid.uuid4().hex}.part.mp4")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        "-framerate", str(fps), "-f", "h264", "-i", str(src),
        "-c", "copy", "-movflags", "+faststart", str(tmp),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), FFMPEG_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise MediaError(f"ffmpeg timed out after {FFMPEG_TIMEOUT_S}s") from exc
        if proc.returncode != 0:
            raise MediaError(stderr.decode(errors="replace")[-STDERR_TAIL:] or f"ffmpeg exit {proc.returncode}")
        tmp.replace(dst)
    except BaseException:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
            await asyncio.shield(proc.wait())
        tmp.unlink(missing_ok=True)
        raise


async def _mark_error(pool: AsyncConnectionPool, item_id: str, message: str) -> None:
    await _update(
        pool, item_id,
        "UPDATE item SET status = 'error', error = %s WHERE id = %s AND status = 'stored'",
        (message[-STDERR_TAIL:], item_id),
    )


async def _update(pool: AsyncConnectionPool, item_id: str, sql: str, params: tuple) -> None:
    try:
        async with pool.connection() as conn:
            await conn.execute(sql, params)
    except Exception:
        log.exception("could not record remux result for %s", item_id)


async def remux_item(pool: AsyncConnectionPool, settings: Settings, item_id: str) -> None:
    """Remux one stored video item; never fails silently (errors are logged and stored on the row)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT raw_path, meta FROM item WHERE id = %s AND kind = 'video' AND status = 'stored'",
            (item_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return
    raw_rel = PurePosixPath(row["raw_path"])
    view_rel = PurePosixPath("view", *raw_rel.parts[1:]).with_suffix(".mp4")
    fps = int(row["meta"].get("video_fps", settings.default_video_fps))
    try:
        await remux_to_mp4(settings.media_root / raw_rel, settings.media_root / view_rel, fps)
    except (MediaError, OSError) as exc:
        log.warning("remux failed for %s: %s", item_id, exc)
        await _mark_error(pool, item_id, str(exc) or type(exc).__name__)
        return
    except Exception as exc:
        log.exception("unexpected remux failure for %s", item_id)
        await _mark_error(pool, item_id, f"unexpected: {type(exc).__name__}: {exc}")
        return
    await _update(
        pool, item_id,
        "UPDATE item SET status = 'ready', view_path = %s, error = NULL WHERE id = %s AND status = 'stored'",
        (str(view_rel), item_id),
    )


async def requeue_stored(pool: AsyncConnectionPool, settings: Settings) -> None:
    """Remux video items left in 'stored' by a crash or restart (errors are logged, never raised)."""
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM item WHERE kind = 'video' AND status = 'stored' ORDER BY started_at"
            )
            ids = [r["id"] for r in await cur.fetchall()]
    except Exception:
        log.exception("could not list stored videos for requeue")
        return
    for item_id in ids:
        try:
            await remux_item(pool, settings, item_id)
        except Exception:
            log.exception("requeue of %s failed", item_id)

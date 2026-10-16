"""Browser-playable views of raw media: raw H.264 -> MP4 without re-encoding, and audio at a level you
can hear (spec D19). Raw files are only ever read."""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from pathlib import Path, PurePosixPath

import anyio
import numpy as np
from psycopg_pool import AsyncConnectionPool

from . import audio_level
from .config import Settings

log = logging.getLogger(__name__)
STDERR_TAIL = 500
FFMPEG_TIMEOUT_S = 300.0
AUDIO_TIMEOUT_S = 120.0  # per ffmpeg pass; a 10-minute segment (the agent's longest) takes a few seconds
# -1 dBFS ceiling; level=false: no automatic make-up gain on top of ours; latency=true: no 5 ms shift.
AUDIO_LIMITER = "alimiter=limit=0.891:attack=5:release=50:level=false:latency=true"


class MediaError(Exception):
    """ffmpeg failed to process a file."""


async def run_ffmpeg(args: list[str], timeout: float) -> bytes:
    """Run ffmpeg with ``args``; its stdout. MediaError on a non-zero exit or after ``timeout`` seconds,
    and the process never outlives the call."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError as exc:
            raise MediaError(f"ffmpeg timed out after {timeout}s") from exc
        if proc.returncode != 0:
            raise MediaError(stderr.decode(errors="replace")[-STDERR_TAIL:] or f"ffmpeg exit {proc.returncode}")
        return stdout
    except BaseException:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
            await asyncio.shield(proc.wait())
        raise


def inside(media_root: Path, rel: str | PurePosixPath) -> Path:
    """``media_root / rel`` resolved; ValueError for an absolute path or one that leaves the media root (a path
    read from the database is still checked before a file is read, written or deleted)."""
    root = media_root.resolve()
    path = (root / rel).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"path outside the media root: {rel}")
    return path


def _view_rel(raw_rel: PurePosixPath, suffix: str) -> PurePosixPath:
    """``raw/<device>/YYYY/MM/DD/<id>.<ext>`` -> ``view/<device>/YYYY/MM/DD/<id><suffix>``."""
    return PurePosixPath("view", *raw_rel.parts[1:]).with_suffix(suffix)


# Assumption: raw input has no VUI timing (Pi encoder output), so -framerate sets the pace;
# streams that carry VUI timing (e.g. libx264) make ffmpeg ignore -framerate.
async def remux_to_mp4(src: Path, dst: Path, fps: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.stem}.{uuid.uuid4().hex}.part.mp4")
    try:
        await run_ffmpeg(
            ["-framerate", str(fps), "-f", "h264", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(tmp)],
            FFMPEG_TIMEOUT_S,
        )
        tmp.replace(dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def audio_view_path(raw_rel: PurePosixPath) -> PurePosixPath:
    """``raw/<device>/YYYY/MM/DD/<id>.opus`` -> ``view/<device>/YYYY/MM/DD/<id>.opus``."""
    return _view_rel(raw_rel, ".opus")


async def normalise_audio(src: Path, dst: Path) -> float:
    """Write ``src`` to ``dst`` as Ogg Opus raised by the shared level gain, through a -1 dBFS limiter so
    nothing clips; return the gain. ``src`` is only read; ``dst`` appears complete or not at all. The encode is
    bit-exact (no random Ogg stream serial), so two first plays racing for one item write identical copies."""
    pcm = await run_ffmpeg(["-i", str(src), "-vn", "-ac", "1", "-ar", str(audio_level.SAMPLE_RATE), "-f", "f32le", "-"],
                           AUDIO_TIMEOUT_S)
    gain = await anyio.to_thread.run_sync(audio_level.level_gain, np.frombuffer(pcm, dtype=np.float32))
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.stem}.{uuid.uuid4().hex}.part.opus")
    try:
        await run_ffmpeg(
            ["-i", str(src), "-vn", "-ac", "1", "-ar", str(audio_level.SAMPLE_RATE),
             "-af", f"volume={gain:.6f},{AUDIO_LIMITER}", "-c:a", "libopus", "-b:a", "32k",
             "-fflags", "+bitexact", "-f", "ogg", str(tmp)],
            AUDIO_TIMEOUT_S,
        )
        tmp.replace(dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return gain


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
    view_rel = _view_rel(raw_rel, ".mp4")
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

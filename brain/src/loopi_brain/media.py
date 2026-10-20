"""Browser-playable views of raw media: raw H.264 -> MP4 without re-encoding, and audio at a level you
can hear (spec D19). Raw files are only ever read."""
from __future__ import annotations

import asyncio
import logging
import os
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
    """``media_root / rel`` resolved; ValueError for an absolute path (even one that points into the root), for the
    media root itself, or for one that leaves the media root, by ``..`` or through a symlink (a path read from the
    database is still checked before a file is read, written or deleted)."""
    if PurePosixPath(rel).is_absolute():
        raise ValueError(f"absolute path: {rel}")
    root = media_root.resolve()
    path = (root / rel).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"path outside the media root: {rel}")
    return path


def _entry(media_root: Path, rel: str) -> Path:
    """The directory entry ``rel`` names, for deleting it. ``inside`` checks where ``rel`` leads; the entry itself
    is what goes (a symlink is removed, never the file it points to), and the folder it lives in must also lie
    inside the media root (a link kept in a folder outside the root is not ours to remove)."""
    inside(media_root, rel)
    root = media_root.resolve()
    entry = (root / rel).parent.resolve() / PurePosixPath(rel).name
    if not entry.parent.is_relative_to(root):
        raise ValueError(f"path outside the media root: {rel}")
    return entry


def fsync_path(path: Path) -> None:
    """fsync a file or a directory (blocking) so what was written to it survives a crash; OSError if it cannot."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def remove_files(media_root: Path, rels: list[str]) -> tuple[int, list[str]]:
    """Delete media files (blocking); return (files deleted, paths that could not be deleted). A missing file
    counts as gone; a path outside the media root is refused and reported, never touched; a symlink inside the
    root is removed without the file it points to."""
    deleted, failed = 0, []
    for rel in rels:
        try:
            entry = _entry(media_root, rel)
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
                deleted += 1
        except (ValueError, OSError) as exc:
            log.warning("could not delete media file %s: %s", rel, exc)
            failed.append(rel)
    return deleted, failed


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
        # tmp -> fsync -> rename -> fsync the directory: the MP4 is the one copy that stays (D32), so it is on disk
        # under its final name before anything else relies on it
        await anyio.to_thread.run_sync(fsync_path, tmp)
        tmp.replace(dst)
        await anyio.to_thread.run_sync(fsync_path, dst.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def audio_view_path(raw_rel: PurePosixPath) -> PurePosixPath:
    """``raw/<device>/YYYY/MM/DD/<id>.opus`` -> ``view/<device>/YYYY/MM/DD/<id>.opus``."""
    return _view_rel(raw_rel, ".opus")


def item_files(kind: str, raw_path: str, view_path: str | None) -> list[str]:
    """Every media file that belongs to one item, relative to the media root: its raw file, the MP4 view of a
    video and the playback copy of an audio recording. The MP4 is listed by its fixed name even when ``view_path``
    is not recorded (a remux may have written it a moment ago and not yet said so); a file that does not exist
    counts as gone. One rule for "Quên khoảng thời gian" and for retention; keyframes and their thumbnails belong
    to the item's keyframes, not to it."""
    files = [raw_path]
    if kind == "video":
        files.append(str(_view_rel(PurePosixPath(raw_path), ".mp4")))
        if view_path and view_path not in files:
            files.append(view_path)
    if kind == "audio":
        files.append(str(audio_view_path(PurePosixPath(raw_path))))
    return files


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
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE item SET status = 'ready', view_path = %s, error = NULL"
                " WHERE id = %s AND status = 'stored' RETURNING id",
                (str(view_rel), item_id),
            )
            recorded = await cur.fetchone() is not None
            gone = False
            if not recorded:  # moved on (kept as it is), or forgotten while ffmpeg worked
                cur = await conn.execute("SELECT 1 FROM item WHERE id = %s", (item_id,))
                gone = await cur.fetchone() is None
    except Exception:
        log.exception("could not record remux result for %s", item_id)
        return
    if gone:
        log.info("item %s was deleted during its remux; its MP4 is removed", item_id)
        remove_files(settings.media_root, [str(view_rel)])


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

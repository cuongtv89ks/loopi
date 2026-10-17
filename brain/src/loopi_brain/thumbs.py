"""Small JPEG thumbnails of keyframes (P2a deferred M8: pages loaded the 1080p keyframes).

``keyframes/<device>/YYYY/MM/DD/<id>.jpg`` gets ``thumbs/<device>/YYYY/MM/DD/<id>.jpg``, 320 px wide, made on the
first request and kept (keyframes never change). "Quên khoảng thời gian" deletes both.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path, PurePosixPath

import anyio
from PIL import Image
from psycopg_pool import AsyncConnectionPool

from . import media
from .pipeline.state import keyframe_exists

log = logging.getLogger(__name__)

THUMB_WIDTH = 320
THUMB_QUALITY = 80


def thumb_rel(keyframe_rel: PurePosixPath) -> PurePosixPath:
    """``keyframes/…/<id>.jpg`` -> ``thumbs/…/<id>.jpg``; ValueError for any other path."""
    if keyframe_rel.is_absolute() or len(keyframe_rel.parts) < 2 or keyframe_rel.parts[0] != "keyframes":
        raise ValueError(f"not a keyframe path: {keyframe_rel}")
    return PurePosixPath("thumbs", *keyframe_rel.parts[1:])


def make_thumb(src: Path, dst: Path) -> None:
    """Write ``src`` scaled to THUMB_WIDTH pixels wide (never enlarged) as JPEG to ``dst`` (temp file, then rename)."""
    with Image.open(src) as img:
        img = img.convert("RGB")
        if img.width > THUMB_WIDTH:
            img = img.resize((THUMB_WIDTH, max(1, round(img.height * THUMB_WIDTH / img.width))), Image.Resampling.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.stem}.{uuid.uuid4().hex}.part.jpg")
        try:
            img.save(tmp, format="JPEG", quality=THUMB_QUALITY)
            tmp.replace(dst)
        finally:
            tmp.unlink(missing_ok=True)


class ThumbUnavailable(Exception):
    """No thumbnail for a keyframe; ``str(exc)`` is a fixed reason: unknown keyframe, keyframe file missing,
    keyframe unreadable."""


async def keyframe_thumb(pool: AsyncConnectionPool, media_root: Path, keyframe_id: str) -> Path:
    """The path of ``keyframe_id``'s thumbnail, made now if it is not there yet (the Day page's ``/thumb`` route and
    the MCP ``get_moment`` share this); ThumbUnavailable when there is nothing to show."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT path FROM keyframe WHERE id = %s", (keyframe_id,))
        row = await cur.fetchone()
    if row is None:
        raise ThumbUnavailable("unknown keyframe")
    try:
        rel = PurePosixPath(row["path"])
        src = media.inside(media_root, rel)
        dst = media.inside(media_root, thumb_rel(rel))
    except ValueError as exc:
        log.warning("keyframe %s has an unusable path: %s", keyframe_id, exc)
        raise ThumbUnavailable("keyframe file missing") from exc
    if not src.is_file():
        raise ThumbUnavailable("keyframe file missing")
    if not dst.is_file():
        try:
            await anyio.to_thread.run_sync(make_thumb, src, dst)
        except OSError as exc:  # not a readable picture (PIL.UnidentifiedImageError is one), or the disk refused it
            log.warning("keyframe %s: no thumbnail: %s", keyframe_id, exc)
            raise ThumbUnavailable("keyframe unreadable") from exc
        if not await keyframe_exists(pool, keyframe_id):  # forgotten while the thumbnail was made
            media.remove_files(media_root, [str(thumb_rel(rel))])
            raise ThumbUnavailable("unknown keyframe")
    return dst

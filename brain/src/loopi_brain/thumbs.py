"""Small JPEG thumbnails of keyframes (P2a deferred M8: pages loaded the 1080p keyframes).

``keyframes/<device>/YYYY/MM/DD/<id>.jpg`` gets ``thumbs/<device>/YYYY/MM/DD/<id>.jpg``, 320 px wide, made on the
first request and kept (keyframes never change). "Quên khoảng thời gian" deletes both.
"""
from __future__ import annotations

import uuid
from pathlib import Path, PurePosixPath

from PIL import Image

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

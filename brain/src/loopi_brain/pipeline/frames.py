"""Keyframe selection (spec §9.2 steps 3-4): small grey frames, dHash, darkness and motion filters."""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import numpy as np
from PIL import Image

from ..config import Settings
from ..media import MediaError, run_ffmpeg

SMALL_W, SMALL_H = 64, 36
FFMPEG_TIMEOUT_S = 300.0
_U64 = 1 << 64
# Neighbouring cells must differ by more than this many grey levels to give a 1 bit, so sensor noise
# on flat or dim areas cannot flip bits (a still, noisy scene keeps one hash).
DHASH_DEADBAND = 2


class FrameError(Exception):
    """ffmpeg could not decode or extract frames."""


@dataclass(frozen=True)
class KeyframeParams:
    dark_luma: float = 24.0  # mean grey level below which a frame is "night"
    motion_max: float = 8.0  # mean absolute difference to the neighbouring frame above which it is blurred motion
    hash_threshold: int = 12  # dHash bits that must differ from the previous keyframe
    min_gap_s: float = 30.0  # minimum time between two keyframes of a device

    @classmethod
    def from_settings(cls, settings: Settings) -> KeyframeParams:
        return cls(
            dark_luma=settings.keyframe_dark_luma,
            motion_max=settings.keyframe_motion_max,
            hash_threshold=settings.keyframe_hash_threshold,
            min_gap_s=settings.keyframe_min_gap_s,
        )


@dataclass(frozen=True)
class Selected:
    index: int
    ts: dt.datetime
    dhash: int  # unsigned 64-bit
    luma: float


def dhash(frame: np.ndarray) -> int:
    """64-bit difference hash: BOX-resize to 9x8; a bit is 1 where the right neighbour is brighter
    by more than DHASH_DEADBAND grey levels."""
    small = np.asarray(Image.fromarray(frame).resize((9, 8), Image.Resampling.BOX), dtype=np.int16)
    value = 0
    for bit in ((small[:, 1:] - small[:, :-1]) > DHASH_DEADBAND).flatten():
        value = (value << 1) | int(bit)
    return value


def hamming(a: int, b: int) -> int:
    return ((a ^ b) % _U64).bit_count()


def to_signed64(value: int) -> int:
    """Unsigned 64-bit hash -> Postgres BIGINT."""
    return value - _U64 if value >= 1 << 63 else value


def to_unsigned64(value: int) -> int:
    """Postgres BIGINT -> unsigned 64-bit hash."""
    return value % _U64


def motion_scores(frames: np.ndarray) -> np.ndarray:
    """Mean absolute difference of each frame to the previous one (frame 0: to the next one)."""
    n = len(frames)
    if n < 2:
        return np.zeros(n)
    diffs = np.abs(np.diff(frames.astype(np.int16), axis=0)).reshape(n - 1, -1).mean(axis=1)
    return np.concatenate(([diffs[0]], diffs))


def select_keyframes(
    frames: np.ndarray,
    fps: int,
    start: dt.datetime,
    last_hash: int | None,
    last_ts: dt.datetime | None,
    params: KeyframeParams,
) -> list[Selected]:
    """Pick the frames that are really new: lit, still, far (in time and dHash) from the previous keyframe."""
    if len(frames) == 0:
        return []
    luma = frames.reshape(len(frames), -1).mean(axis=1)
    motion = motion_scores(frames)
    selected: list[Selected] = []
    for i in range(len(frames)):
        if luma[i] < params.dark_luma or motion[i] > params.motion_max:
            continue
        ts = start + dt.timedelta(seconds=i / fps)
        if last_ts is not None and (ts - last_ts).total_seconds() < params.min_gap_s:
            continue
        h = dhash(frames[i])
        if last_hash is not None and hamming(h, last_hash) < params.hash_threshold:
            continue
        selected.append(Selected(i, ts, h, float(luma[i])))
        last_hash, last_ts = h, ts
    return selected


def keyframe_rel_path(device_id: str, ts: dt.datetime, keyframe_id: str) -> PurePosixPath:
    """keyframes/<device>/YYYY/MM/DD/<id>.jpg, UTC date of the keyframe (same convention as raw/)."""
    return PurePosixPath("keyframes", device_id, ts.astimezone(dt.UTC).strftime("%Y/%m/%d"), f"{keyframe_id}.jpg")


async def _ffmpeg(args: list[str]) -> bytes:
    try:
        return await run_ffmpeg(args, FFMPEG_TIMEOUT_S)
    except MediaError as exc:
        raise FrameError(str(exc)) from exc


async def read_small_frames(src: Path, fps: int) -> np.ndarray:
    """Decode every frame of a raw H.264 file as 64x36 grey -> array of shape (N, 36, 64), uint8."""
    out = await _ffmpeg([
        "-framerate", str(fps), "-f", "h264", "-i", str(src),
        "-vf", f"scale={SMALL_W}:{SMALL_H}:flags=area,format=gray", "-fps_mode", "passthrough",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ])
    size = SMALL_W * SMALL_H
    count = len(out) // size
    return np.frombuffer(out[: count * size], dtype=np.uint8).reshape(count, SMALL_H, SMALL_W)


async def extract_jpeg(src: Path, fps: int, index: int, dst: Path) -> None:
    """Write frame ``index`` of ``src`` at full resolution to ``dst`` (temp file, then rename)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.stem}.{uuid.uuid4().hex}.part.jpg")
    try:
        await _ffmpeg([
            "-framerate", str(fps), "-f", "h264", "-i", str(src),
            "-vf", f"select=eq(n\\,{index})", "-fps_mode", "passthrough", "-frames:v", "1", "-q:v", "3", str(tmp),
        ])
        if not tmp.is_file():
            raise FrameError(f"ffmpeg wrote no frame {index}")
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)

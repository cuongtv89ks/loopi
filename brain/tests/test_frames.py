import datetime as dt

import numpy as np
import pytest
from PIL import Image

from helpers import NIGHT, T0, TWO_SCENES, make_h264
from loopi_brain.config import Settings
from loopi_brain.pipeline import frames
from loopi_brain.pipeline.frames import (
    SMALL_H,
    SMALL_W,
    FrameError,
    KeyframeParams,
    dhash,
    hamming,
    select_keyframes,
    to_signed64,
    to_unsigned64,
)

PARAMS = KeyframeParams()
HOUR_AT_2FPS = 2 * 3600


def _frame(value: int) -> np.ndarray:
    return np.full((SMALL_H, SMALL_W), value, dtype=np.uint8)


def _textures(n: int, seed: int = 7, lo: int = 124, hi: int = 133) -> np.ndarray:
    """n frames, each a different random 6x8 block pattern of grey levels lo..hi-1. With the default
    low contrast every frame looks new (dHash differs) but nothing moves fast (mean absolute
    difference about 2.7 grey levels)."""
    coarse = np.random.default_rng(seed).integers(lo, hi, size=(n, 6, 8)).astype(np.uint8)
    return coarse.repeat(6, axis=1).repeat(8, axis=2)


def test_dhash_is_64_bits_and_round_trips_through_bigint():
    h = dhash(_textures(1)[0])
    assert 0 <= h < 1 << 64
    assert dhash(_frame(90)) == 0  # a flat frame has no gradients
    for value in (0, 1, (1 << 63) - 1, 1 << 63, (1 << 64) - 1):
        signed = to_signed64(value)
        assert -(1 << 63) <= signed < 1 << 63
        assert to_unsigned64(signed) == value
    assert hamming(0, (1 << 64) - 1) == 64 and hamming(h, h) == 0


def test_keyframe_params_come_from_settings():
    s = Settings(database_url="postgresql://x", keyframe_dark_luma=10, keyframe_motion_max=3,
                 keyframe_hash_threshold=20, keyframe_min_gap_s=60)
    assert KeyframeParams.from_settings(s) == KeyframeParams(10, 3, 20, 60)


def test_night_hour_gives_no_keyframes():
    night = np.random.default_rng(1).integers(0, 30, size=(HOUR_AT_2FPS, SMALL_H, SMALL_W), dtype=np.uint8)
    assert select_keyframes(night, 2, T0, None, None, PARAMS) == []


def test_busy_hour_is_bounded_by_the_minimum_gap():
    busy = _textures(HOUR_AT_2FPS)
    picks = select_keyframes(busy, 2, T0, None, None, PARAMS)
    gaps = [(b.ts - a.ts).total_seconds() for a, b in zip(picks, picks[1:], strict=False)]
    assert 100 <= len(picks) <= 121  # at most 2 per minute, never one per frame
    assert min(gaps) >= PARAMS.min_gap_s


def test_still_noisy_scene_keeps_one_keyframe_per_hour():
    rng = np.random.default_rng(3)
    dim_flat = np.clip(40 + rng.normal(0, 3, size=(HOUR_AT_2FPS, SMALL_H, SMALL_W)), 0, 255).astype(np.uint8)
    room = _textures(1, seed=5, lo=60, hi=200)[0].astype(float)  # a fixed, contrasted scene
    still_room = np.clip(room + rng.normal(0, 1.5, size=(HOUR_AT_2FPS, SMALL_H, SMALL_W)), 0, 255).astype(np.uint8)
    for clip in (dim_flat, still_room):  # sensor noise only: nothing new happens all hour
        assert len(select_keyframes(clip, 2, T0, None, None, PARAMS)) == 1


def test_fast_motion_frames_are_skipped():
    still = _frame(120)
    moving = [_frame(200 if i % 2 else 120) for i in range(10)]  # flickers by 80 grey levels; frame 9 is 200
    clip = np.stack([*moving, still, still])
    picks = select_keyframes(clip, 2, T0, None, None, PARAMS)
    assert [p.index for p in picks] == [11]  # frame 10 still differs from 9 by 80; frame 11 is still


def test_same_scene_as_previous_keyframe_is_not_new_and_new_scene_is():
    scene = _textures(1, seed=1, lo=60, hi=200)[0]
    other = _textures(1, seed=2, lo=60, hi=200)[0]
    clip = np.stack([scene] * 100 + [other] * 100)
    last_ts = T0 - dt.timedelta(minutes=5)
    picks = select_keyframes(clip, 2, T0, dhash(scene), last_ts, PARAMS)
    assert [p.index for p in picks] == [101]  # 100 is the cut (motion); 101 is the first still frame
    assert picks[0].ts == T0 + dt.timedelta(seconds=50.5)
    assert picks[0].dhash == dhash(other) and picks[0].luma == pytest.approx(other.mean())


def test_first_keyframe_waits_for_the_gap_after_the_previous_one():
    clip = _textures(120)
    picks = select_keyframes(clip, 2, T0, 0, T0 - dt.timedelta(seconds=10), PARAMS)
    assert picks[0].ts - T0 >= dt.timedelta(seconds=20)


def test_empty_input_selects_nothing():
    assert select_keyframes(np.zeros((0, SMALL_H, SMALL_W), np.uint8), 2, T0, None, None, PARAMS) == []
    one = select_keyframes(_textures(1), 2, T0, None, None, PARAMS)
    assert [p.index for p in one] == [0]


def test_keyframe_path_uses_the_utc_date():
    seoul = dt.timezone(dt.timedelta(hours=9))
    ts = dt.datetime(2026, 10, 15, 8, 30, tzinfo=seoul)  # 2026-10-14 23:30 UTC
    assert str(frames.keyframe_rel_path("DEV", ts, "KF")) == "keyframes/DEV/2026/10/14/KF.jpg"


async def test_read_small_frames_and_extract_full_resolution_jpeg(tmp_path):
    src = make_h264(tmp_path / "scenes.h264", TWO_SCENES, 80)
    small = await frames.read_small_frames(src, 2)
    assert small.shape == (160, SMALL_H, SMALL_W) and small.dtype == np.uint8
    picks = select_keyframes(small, 2, T0, None, None, PARAMS)
    assert [p.index for p in picks] == [0, 81]
    dst = tmp_path / "out" / "kf.jpg"
    await frames.extract_jpeg(src, 2, 81, dst)
    with Image.open(dst) as img:
        assert (img.format, img.size) == ("JPEG", (640, 360))
        seen = np.asarray(img.convert("L").resize((SMALL_W, SMALL_H), Image.Resampling.BOX), dtype=np.int16)
    assert [p.name for p in dst.parent.iterdir()] == ["kf.jpg"]
    # The JPEG is the selected frame itself: close to small frame 81, far from frame 79 (the other scene).
    assert np.abs(seen - small[81]).mean() < 6 < 20 < np.abs(seen - small[79]).mean()


async def test_night_video_decodes_to_dark_frames(tmp_path):
    src = make_h264(tmp_path / "night.h264", NIGHT, 30)
    small = await frames.read_small_frames(src, 2)
    assert len(small) == 60 and small.mean() < PARAMS.dark_luma
    assert select_keyframes(small, 2, T0, None, None, PARAMS) == []


async def test_ffmpeg_errors_raise_frame_error_and_leave_no_temp_files(tmp_path):
    garbage = tmp_path / "bad.h264"
    garbage.write_bytes(b"definitely not h264")
    with pytest.raises(FrameError):
        await frames.read_small_frames(garbage, 2)
    src = make_h264(tmp_path / "short.h264", TWO_SCENES, 2)
    with pytest.raises(FrameError):
        await frames.extract_jpeg(src, 2, 500, tmp_path / "kf" / "x.jpg")
    assert list((tmp_path / "kf").iterdir()) == []


async def test_ffmpeg_timeout_kills_the_process(tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ffmpeg").write_text("#!/bin/sh\nexec sleep 30\n")
    (fake / "ffmpeg").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")
    monkeypatch.setattr(frames, "FFMPEG_TIMEOUT_S", 0.3)
    with pytest.raises(FrameError, match="timed out"):
        await frames.read_small_frames(tmp_path / "x.h264", 2)

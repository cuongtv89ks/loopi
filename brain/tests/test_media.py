import hashlib
import shutil
import subprocess

import pytest

from helpers import item_headers, paired_device
from loopi_brain import media
from loopi_brain.app import create_app

ITEM_ID = "01JA2B3C4D5E6F7G8H9J0KMNPR"
FRAMES = 10


@pytest.fixture(scope="session")
def h264_sample(tmp_path_factory) -> bytes:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe are required for media tests (brew install ffmpeg)")
    out = tmp_path_factory.mktemp("media") / "sample.h264"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=2",
         "-frames:v", str(FRAMES), "-c:v", "libx264", "-g", "2", "-bsf:v", "h264_mp4toannexb", "-f", "h264", str(out)],
        check=True,
    )
    return out.read_bytes()


@pytest.fixture(scope="session")
def h264_without_timing(tmp_path_factory) -> bytes:
    """Hardware-encoder-like stream with no VUI timing, so only -framerate decides the pace.

    libx264 writes timing info into the SPS and ffmpeg then ignores -framerate, so the fps flag
    can only be proven with an encoder that omits it (h264_videotoolbox on macOS).
    """
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg required")
    out = tmp_path_factory.mktemp("media_nt") / "sample.h264"
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=2",
         "-frames:v", str(FRAMES), "-c:v", "h264_videotoolbox", "-f", "h264", str(out)],
        capture_output=True,
    )
    if proc.returncode != 0:
        pytest.skip("no timing-free H.264 encoder available (h264_videotoolbox)")
    return out.read_bytes()


def _duration(path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


async def _upload(client, dev, body: bytes, **extra):
    return await client.put(
        f"/v1/items/{ITEM_ID}", content=body, headers=item_headers(dev["fingerprint"], body, **extra)
    )


async def _status_row(pool):
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT status, view_path, error FROM item WHERE id = %s", (ITEM_ID,))
        return await cur.fetchone()


async def test_remux_respects_fps(tmp_path, h264_without_timing):
    src = tmp_path / "in.h264"
    src.write_bytes(h264_without_timing)
    slow, fast = tmp_path / "out" / "slow.mp4", tmp_path / "out" / "fast.mp4"
    await media.remux_to_mp4(src, slow, fps=2)
    await media.remux_to_mp4(src, fast, fps=25)
    assert 4.0 <= _duration(slow) <= 6.0  # 10 frames at 2 fps = 5 s
    assert _duration(fast) < 1.0  # 10 frames at 25 fps = 0.4 s
    assert not list(tmp_path.rglob("*.part.mp4"))


async def test_remux_of_garbage_raises(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg required")
    src = tmp_path / "bad.h264"
    src.write_bytes(b"not video")
    with pytest.raises(media.MediaError):
        await media.remux_to_mp4(src, tmp_path / "bad.mp4", fps=2)
    assert not (tmp_path / "bad.mp4").exists()
    assert not list(tmp_path.rglob("*.part.mp4"))


async def test_uploaded_video_becomes_ready(client, pool, pki_settings, h264_sample):
    dev = await paired_device(pool, pki_settings)
    r = await _upload(client, dev, h264_sample, **{"X-Loopi-Video-Fps": "2"})
    assert r.status_code == 201
    row = await _status_row(pool)
    assert row["status"] == "ready"
    assert row["view_path"] == f"view/{dev['device_id']}/2026/10/14/{ITEM_ID}.mp4"
    assert (pki_settings.media_root / row["view_path"]).is_file()


async def test_failed_remux_marks_item_error(client, pool, pki_settings):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg required")
    dev = await paired_device(pool, pki_settings)
    assert (await _upload(client, dev, b"definitely not h264")).status_code == 201
    row = await _status_row(pool)
    assert row["status"] == "error" and row["error"]


async def test_missing_ffmpeg_marks_item_error(client, pool, pki_settings, monkeypatch, caplog):
    dev = await paired_device(pool, pki_settings)

    async def boom(*_a, **_k):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(media, "remux_to_mp4", boom)
    assert (await _upload(client, dev, b"video-bytes")).status_code == 201
    row = await _status_row(pool)
    assert row["status"] == "error" and "ffmpeg" in row["error"]
    assert ITEM_ID in caplog.text


async def test_unexpected_remux_exception_is_logged_and_marked(client, pool, pki_settings, monkeypatch, caplog):
    dev = await paired_device(pool, pki_settings)

    async def boom(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(media, "remux_to_mp4", boom)
    assert (await _upload(client, dev, b"video-bytes")).status_code == 201
    row = await _status_row(pool)
    assert row["status"] == "error" and "kaboom" in row["error"]
    assert "kaboom" in caplog.text


async def test_startup_requeues_stored_videos(client, pool, pki_settings, h264_sample, monkeypatch):
    dev = await paired_device(pool, pki_settings)
    real_remux = media.remux_item

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(media, "remux_item", noop)  # simulate a crash before remux ran
    assert (await _upload(client, dev, h264_sample, **{"X-Loopi-Video-Fps": "2"})).status_code == 201
    assert (await _status_row(pool))["status"] == "stored"
    monkeypatch.setattr(media, "remux_item", real_remux)

    app = create_app(pki_settings, pool=pool)
    async with app.router.lifespan_context(app):
        await app.state.requeue_task
    row = await _status_row(pool)
    assert row["status"] == "ready"
    assert (pki_settings.media_root / row["view_path"]).is_file()


async def test_startup_does_not_wait_for_requeue(pool, pki_settings, monkeypatch):
    import asyncio

    gate = asyncio.Event()

    async def slow(*_a, **_k):
        await gate.wait()

    monkeypatch.setattr(media, "requeue_stored", slow)
    app = create_app(pki_settings, pool=pool)
    async with app.router.lifespan_context(app):
        assert not app.state.requeue_task.done()

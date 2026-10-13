import asyncio
import hashlib
import shutil
import subprocess
from pathlib import Path

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


# Generated once with:
#   ffmpeg -f lavfi -i testsrc=size=64x64:rate=2 -frames:v 10 -c:v h264_videotoolbox -f h264 no_vui_timing_10f.h264
# The stream carries no VUI timing (libx264 always writes it, and then ffmpeg ignores -framerate),
# like raw Pi encoder output, so -framerate alone decides the playback pace.
NO_VUI_FIXTURE = Path(__file__).parent / "fixtures" / "no_vui_timing_10f.h264"


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


async def test_remux_respects_fps(tmp_path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe required")
    src = tmp_path / "in.h264"
    src.write_bytes(NO_VUI_FIXTURE.read_bytes())
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


async def _insert_item(pool, dev, kind="video", status="stored"):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO item (id, device_id, kind, started_at, ended_at, sha256, size_bytes, raw_path, status)"
            " VALUES (%s, %s, %s, now(), now(), 'x', 1, %s, %s)",
            (ITEM_ID, dev["device_id"], kind, f"raw/{dev['device_id']}/x.h264", status),
        )


async def test_remux_item_is_noop_for_ready_item(pool, pki_settings, monkeypatch):
    dev = await paired_device(pool, pki_settings)
    await _insert_item(pool, dev, status="ready")
    calls = []

    async def spy(*a, **k):
        calls.append(a)

    monkeypatch.setattr(media, "remux_to_mp4", spy)
    await media.remux_item(pool, pki_settings, ITEM_ID)
    assert calls == [] and (await _status_row(pool))["status"] == "ready"


async def test_remux_item_is_noop_for_non_video(pool, pki_settings, monkeypatch):
    dev = await paired_device(pool, pki_settings)
    await _insert_item(pool, dev, kind="audio")
    calls = []

    async def spy(*a, **k):
        calls.append(a)

    monkeypatch.setattr(media, "remux_to_mp4", spy)
    await media.remux_item(pool, pki_settings, ITEM_ID)
    assert calls == [] and (await _status_row(pool))["status"] == "stored"


async def test_late_result_does_not_overwrite_moved_on_state(pool, pki_settings, monkeypatch):
    dev = await paired_device(pool, pki_settings)
    await _insert_item(pool, dev)

    async def finish_elsewhere(*_a, **_k):
        async with pool.connection() as conn:
            await conn.execute("UPDATE item SET status = 'error', error = 'other' WHERE id = %s", (ITEM_ID,))

    monkeypatch.setattr(media, "remux_to_mp4", finish_elsewhere)
    await media.remux_item(pool, pki_settings, ITEM_ID)
    row = await _status_row(pool)
    assert row["status"] == "error" and row["error"] == "other"


async def test_requeue_db_error_is_logged_not_raised(pki_settings, caplog):
    class BrokenPool:
        def connection(self):
            raise RuntimeError("pool down")

    await media.requeue_stored(BrokenPool(), pki_settings)
    assert "could not list stored videos" in caplog.text


async def test_lifespan_closes_pool_even_if_requeue_task_failed(pki_settings, monkeypatch):
    closed = []

    class FakePool:
        async def open(self):
            pass

        async def close(self):
            closed.append(True)

    import loopi_brain.app as app_mod

    async def failing(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(app_mod, "make_pool", lambda _url: FakePool())
    monkeypatch.setattr(media, "requeue_stored", failing)
    app = create_app(pki_settings)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
    assert closed == [True]


async def test_mark_failure_is_logged_with_item_id(pool, pki_settings, monkeypatch, caplog):
    dev = await paired_device(pool, pki_settings)
    await _insert_item(pool, dev)

    async def fail(*_a, **_k):
        raise MediaError_("bad")

    MediaError_ = media.MediaError
    monkeypatch.setattr(media, "remux_to_mp4", fail)

    real_pool = pool

    class FlakyPool:
        def __init__(self):
            self.n = 0

        def connection(self):
            self.n += 1
            if self.n > 1:  # first call is the SELECT; the UPDATE fails
                raise RuntimeError("db gone")
            return real_pool.connection()

    await media.remux_item(FlakyPool(), pki_settings, ITEM_ID)
    assert f"could not record remux result for {ITEM_ID}" in caplog.text


async def test_ffmpeg_timeout_kills_process_and_cleans_up(tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    script = fake / "ffmpeg"
    script.write_text("#!/bin/sh\nexec sleep 30\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")
    monkeypatch.setattr(media, "FFMPEG_TIMEOUT_S", 0.3)
    src = tmp_path / "in.h264"
    src.write_bytes(b"x")
    with pytest.raises(media.MediaError, match="timed out"):
        await media.remux_to_mp4(src, tmp_path / "out" / "x.mp4", fps=2)
    assert not list(tmp_path.rglob("*.part.mp4"))


async def test_concurrent_remux_uses_distinct_temp_files(tmp_path, monkeypatch):
    seen = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kw):
        seen.append(args[-1])
        return await real(*args, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    src = tmp_path / "in.h264"
    src.write_bytes(NO_VUI_FIXTURE.read_bytes())
    await asyncio.gather(*(media.remux_to_mp4(src, tmp_path / "o.mp4", 2) for _ in range(2)))
    assert len(set(seen)) == 2 and (tmp_path / "o.mp4").is_file()

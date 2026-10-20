import datetime as dt
import logging
import shutil
import subprocess

import pytest
from psycopg.types.json import Jsonb

from helpers import insert_item, insert_keyframe, insert_moment, make_ctx, make_mp4, paired_device, write_jpeg
from loopi_brain import places
from loopi_brain.pipeline import vlogs
from loopi_brain.vlog import render

T = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)
DAY = dt.date(2026, 10, 14)  # T is 09:00 in Seoul


def _without_drawtext() -> str | None:
    """Why the label tests cannot run here (named, so a skip is never silent), or None."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return "needs ffmpeg"
    if not render.LABEL_FONT.is_file():
        return f"needs the label font {render.LABEL_FONT}"
    if b" drawtext " not in subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True).stdout:
        return f"ffmpeg at {ffmpeg} has no drawtext; run with PATH=/usr/bin:$PATH"
    return None


WITHOUT_DRAWTEXT = _without_drawtext()
needs_drawtext = pytest.mark.skipif(WITHOUT_DRAWTEXT is not None, reason=WITHOUT_DRAWTEXT or "")


@pytest.mark.parametrize(("n", "n_out"), [(1000, 1440), (7, 240), (4, 10), (240, 1440), (1, 240)])
def test_segments_share_the_timelapse_equally(n, n_out):
    shares = render.segment_shares(n, n_out)
    assert len(shares) == n and sum(shares) == n_out and max(shares) - min(shares) <= 1
    w = min(10, n)  # a morning run lasts as long as an evening one: the extra frames are spread over the day
    assert all(abs(sum(shares[k:k + w]) - w * n_out / n) <= 1 for k in range(n - w + 1))


def test_label_paths_are_refused_when_they_would_need_quoting(tmp_path):
    with pytest.raises(render.InvalidMedia):
        render.label_filter(tmp_path / "a:b.txt")
    assert "drawtext=fontfile=" in render.label_filter(tmp_path / "label_0.txt")
    assert "expansion=none" in render.label_filter(tmp_path / "label_0.txt")
    assert ":x=28:y=28:" in render.label_filter(tmp_path / "label_0.txt")  # top left, clear of the captions


async def test_one_camera_keeps_one_run_and_no_label(tmp_path):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    make_mp4(a, seconds=4, fps=4, size="320x180")
    make_mp4(b, seconds=4, fps=4, size="320x180")
    steps = []

    async def runner(step, argv, feed):
        steps.append((step, argv))
        await render.run_step(step, argv, feed)

    await render.render(segments=[render.Segment(a, T), render.Segment(b, T + dt.timedelta(seconds=4))],
                        stills=[], keyframes=[], target_seconds=1.0, workdir=tmp_path / "w",
                        out=tmp_path / "out.mp4", title="Hôm nay", runner=runner)
    timelapses = [argv for step, argv in steps if step.startswith("timelapse")]
    assert len(timelapses) == 1 and not any("drawtext" in x for x in timelapses[0])
    assert str(tmp_path / "w" / "segments.ffconcat") in timelapses[0]


@needs_drawtext
async def test_two_cameras_render_their_runs_in_one_vlog(tmp_path):
    pi, cam = tmp_path / "pi.mp4", tmp_path / "cam.mp4"
    make_mp4(pi, seconds=4, fps=24, size="640x360", gop=24)
    make_mp4(cam, seconds=4, fps=6, size="320x180", gop=6)  # a slower, smaller camera: 24 frames
    jpeg = tmp_path / "s.jpg"
    write_jpeg(jpeg)
    still = render.Still(jpeg, T + dt.timedelta(seconds=5), "Bếp", label="Bếp")
    result = await render.render(
        segments=[render.Segment(pi, T, "Bếp"), render.Segment(cam, T + dt.timedelta(seconds=4), "Phòng khách")],
        stills=[still], keyframes=[], target_seconds=2.0, workdir=tmp_path / "w", out=tmp_path / "out.mp4",
        still_seconds=1.0,
    )
    assert result.frames == 2 * render.FPS + render.FPS  # 24 + 24 frames of the two runs, then the 1 s still
    assert render.video_track(result.out).samples == result.frames


# ------------------------------------------------------------------------------------------- the render job (R13)

async def _camera(pool, settings, device_id, minutes: int) -> None:
    """An active 6-s recording of ``device_id`` at 09:00 + ``minutes`` (its MP4 view on disk, a moment) and its
    notable keyframe: one segment and one highlight candidate of that camera."""
    start = T + dt.timedelta(minutes=minutes)
    item = await insert_item(pool, device_id, kind="video", started_at=start, seconds=6)
    view = f"view/{device_id}/2026/10/14/{item}.mp4"
    make_mp4(settings.media_root / view)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done', view_path = %s WHERE id = %s", (view, item))
    frame = await insert_keyframe(pool, settings, item, device_id, start)
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption = %s WHERE id = %s",
                           (Jsonb({"activity": "nấu ăn", "notable": True}), frame))
    await insert_moment(pool, device_id, kind="visual", ts=start, text="phòng")


def _spy(monkeypatch) -> list[dict]:
    real, calls = render.render, []

    async def spy(**kwargs):
        calls.append(kwargs)
        return await real(**kwargs)

    monkeypatch.setattr(vlogs.render, "render", spy)
    return calls


@needs_drawtext
async def test_a_vlog_of_two_cameras_labels_every_scene_with_its_place_read_at_render_time(
        pool, pki_settings, jobs_open, monkeypatch):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await places.set_location(pool, pi["device_id"], "Bếp")
    await places.set_location(pool, cam["device_id"], "Phòng khách")
    await _camera(pool, pki_settings, pi["device_id"], 0)
    await _camera(pool, pki_settings, cam["device_id"], 10)
    ctx = make_ctx(pool, pki_settings)
    assert await vlogs.write_vlog_script(ctx, DAY) == "scripted"
    await places.set_location(pool, cam["device_id"], "Sân")  # renamed after the script was written
    calls = _spy(monkeypatch)
    assert await vlogs.render_vlog(ctx, DAY) is True
    [call] = calls
    assert [s.label for s in call["segments"]] == ["Bếp", "Sân"]
    assert [s.label for s in call["stills"]] == ["Bếp", "Sân"]


async def test_a_vlog_of_one_camera_has_no_label(pool, pki_settings, jobs_open, monkeypatch):
    pi = await paired_device(pool, pki_settings)
    await places.set_location(pool, pi["device_id"], "Bếp")
    await _camera(pool, pki_settings, pi["device_id"], 0)
    await _camera(pool, pki_settings, pi["device_id"], 10)
    ctx = make_ctx(pool, pki_settings)
    assert await vlogs.write_vlog_script(ctx, DAY) == "scripted"
    calls = _spy(monkeypatch)
    assert await vlogs.render_vlog(ctx, DAY) is True
    [call] = calls
    assert [s.label for s in call["segments"]] == [None, None]
    assert [s.label for s in call["stills"]] == [None, None]


@needs_drawtext
async def test_a_still_from_another_camera_turns_the_labels_on(pool, pki_settings, jobs_open, monkeypatch):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await places.set_location(pool, pi["device_id"], "Bếp")
    await places.set_location(pool, cam["device_id"], "Phòng khách")
    await _camera(pool, pki_settings, pi["device_id"], 0)
    cam_item = await insert_item(pool, cam["device_id"], kind="video", started_at=T + dt.timedelta(minutes=10))
    frame = await insert_keyframe(pool, pki_settings, cam_item, cam["device_id"], T + dt.timedelta(minutes=10))
    async with pool.connection() as conn:  # the camera's recording is not in the vlog, its picture is
        await conn.execute("UPDATE keyframe SET caption = %s WHERE id = %s",
                           (Jsonb({"activity": "đọc sách", "notable": True}), frame))
    ctx = make_ctx(pool, pki_settings)
    assert await vlogs.write_vlog_script(ctx, DAY) == "scripted"
    calls = _spy(monkeypatch)
    assert await vlogs.render_vlog(ctx, DAY) is True
    [call] = calls
    assert [s.label for s in call["segments"]] == ["Bếp"]
    assert [s.label for s in call["stills"]] == ["Bếp", "Phòng khách"]


async def test_two_cameras_of_one_place_still_render_apart(tmp_path):
    """A run is one camera even when two cameras share a place name: a resolution change inside one ffmpeg rebuilds
    its filtergraph, which restarts select's frame counter."""
    pi, cam = tmp_path / "pi.mp4", tmp_path / "cam.mp4"
    make_mp4(pi, seconds=4, fps=24, size="640x360", gop=24)
    make_mp4(cam, seconds=4, fps=6, size="320x180", gop=6)
    steps = []

    async def runner(step, argv, feed):
        steps.append(step)
        await render.run_step(step, argv, feed)

    result = await render.render(
        segments=[render.Segment(pi, T, camera="pi"), render.Segment(cam, T + dt.timedelta(seconds=4), camera="cam")],
        stills=[], keyframes=[], target_seconds=2.0, workdir=tmp_path / "w", out=tmp_path / "out.mp4", title="Hôm nay",
        runner=runner,
    )
    assert [s for s in steps if s.startswith("timelapse")] == ["timelapse", "timelapse[1]"]
    assert result.frames == 2 * render.FPS and render.video_track(result.out).samples == result.frames


async def test_stills_go_in_time_order_before_inside_between_and_after_the_runs(tmp_path):
    a1, a2, b = (make_mp4(tmp_path / f"{n}.mp4") for n in ("a1", "a2", "b"))  # 6 s at 2 fps: 12 frames each
    jpeg = write_jpeg(tmp_path / "s.jpg")
    at = [-5, 8, 15, 23, 50]  # before run a, inside it, in the gap, inside run b, after it
    stills = [render.Still(jpeg, T + dt.timedelta(seconds=s), f"s{s}") for s in at]
    result = await render.render(
        segments=[render.Segment(a1, T, camera="a"), render.Segment(a2, T + dt.timedelta(seconds=6), camera="a"),
                  render.Segment(b, T + dt.timedelta(seconds=20), camera="b")],
        stills=stills, keyframes=[], target_seconds=1.5, workdir=tmp_path / "w", out=tmp_path / "out.mp4",
        still_seconds=1.0,
    )
    parts = [line.rsplit("/", 1)[1].rstrip("'") for line in
             (tmp_path / "w" / "parts.ffconcat").read_text(encoding="utf-8").splitlines()[1:]]
    assert parts == ["still_000.mp4", "chunk_000.mp4", "still_001.mp4", "chunk_001.mp4", "still_002.mp4",
                     "run1_chunk_000.mp4", "still_003.mp4", "run1_chunk_001.mp4", "still_004.mp4"]
    # 12 frames per segment (36), cut at 16 of run a's 24 and 6 of run b's 12, and five 1-s stills
    assert [render.video_track(tmp_path / "w" / p).samples for p in parts if "chunk" in p] == [16, 8, 6, 6]
    assert result.frames == 36 + 5 * render.FPS == render.video_track(result.out).samples


async def test_without_the_label_font_a_vlog_of_two_cameras_renders_unlabelled(
        pool, pki_settings, jobs_open, monkeypatch, tmp_path, caplog):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await places.set_location(pool, pi["device_id"], "Bếp")
    await places.set_location(pool, cam["device_id"], "Phòng khách")
    await _camera(pool, pki_settings, pi["device_id"], 0)
    await _camera(pool, pki_settings, cam["device_id"], 10)
    ctx = make_ctx(pool, pki_settings)
    assert await vlogs.write_vlog_script(ctx, DAY) == "scripted"
    monkeypatch.setattr(render, "LABEL_FONT", tmp_path / "missing.ttf")
    calls = _spy(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=vlogs.log.name):
        assert await vlogs.render_vlog(ctx, DAY) is True
    [call] = calls
    assert [s.label for s in call["segments"]] == [None, None]
    assert [s.label for s in call["stills"]] == [None, None]
    assert [s.camera for s in call["segments"]] == [pi["device_id"], cam["device_id"]]  # still two runs
    assert [r.getMessage() for r in caplog.records if "label font" in r.getMessage()] == [
        "vlog of 2026-10-14: the label font is missing; rendered without place labels"]

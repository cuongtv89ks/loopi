import datetime as dt

import pytest

from helpers import make_mp4, make_opus, write_jpeg
from loopi_brain.vlog import render

T0 = dt.datetime(2026, 10, 14, tzinfo=dt.UTC)


@pytest.fixture
def day(tmp_path):
    """Four 6-s segments (2 fps, 12 frames, 3 keyframes each), two pictures and a 5-s sound copy."""
    segments = [render.Segment(make_mp4(tmp_path / f"in/seg{i}.mp4"), T0 + dt.timedelta(minutes=i)) for i in range(4)]
    for i in range(2):
        write_jpeg(tmp_path / f"in/still{i}.jpg", (1280, 720))
    make_opus(tmp_path / "in/clip.ogg", "sine=frequency=440:sample_rate=48000", 5)
    stills = [
        render.Still(tmp_path / "in/still0.jpg", T0 + dt.timedelta(minutes=1, seconds=30), "Bữa sáng",
                     tmp_path / "in/clip.ogg", 1.0),
        render.Still(tmp_path / "in/still1.jpg", T0 + dt.timedelta(minutes=3), "안녕하세요 <b>"),
    ]
    return segments, stills


async def test_a_day_has_one_video_one_sound_and_one_subtitle_track(tmp_path, day):
    segments, stills = day
    out = tmp_path / "out/vlog.mp4"
    out.parent.mkdir()
    result = await render.render(segments=segments, stills=stills, keyframes=[], target_seconds=1.0,
                                 workdir=tmp_path / "work", out=out, still_seconds=1.0, title="Ngày 14/10")
    assert (result.mode, result.frames, result.seconds, result.skipped) == ("timelapse-full", 72, 3.0, [])
    tracks = render.probe_mp4(out)
    assert sorted(t.handler for t in tracks) == ["sbtl", "soun", "vide"]
    assert [t.samples for t in tracks if t.handler == "vide"] == [72]
    assert result.vtt == out.with_suffix(".vtt") and result.vtt.read_text(encoding="utf-8") == (
        "WEBVTT\n\n00:00:00.000 --> 00:00:00.500\nNgày 14/10\n\n00:00:00.500 --> 00:00:01.500\nBữa sáng\n\n"
        "00:00:01.750 --> 00:00:02.750\n안녕하세요 &lt;b&gt;\n\n"
    )


async def test_bad_inputs_are_skipped_before_ffmpeg_runs(tmp_path, day):
    segments, stills = day
    (tmp_path / "in/empty.mp4").write_bytes(b"")
    (tmp_path / "in/broken.jpg").write_bytes(b"\xff\xd8\xff\xe0 not a picture")
    bad = [render.Segment(tmp_path / "in/empty.mp4", T0), render.Segment(tmp_path / "in/missing.mp4", T0)]
    out = tmp_path / "vlog.mp4"
    result = await render.render(
        segments=[*segments, *bad], stills=[*stills, render.Still(tmp_path / "in/broken.jpg", T0, "x")],
        keyframes=[], target_seconds=1.0, workdir=tmp_path / "work", out=out, still_seconds=1.0, title=None,
    )
    assert len(result.skipped) == 3 and result.frames == 72
    assert all("ffmpeg" not in s for s in result.skipped)


async def test_a_segment_vanishing_after_the_check_fails_the_render(tmp_path, day):
    segments, stills = day

    async def runner(step, argv, feed):
        if step == "timelapse":
            segments[2].path.unlink()  # retention or a forget deleted it while the render ran
        await render.run_step(step, argv, feed)

    with pytest.raises(render.RenderError) as failed:
        await render.render(segments=segments, stills=stills, keyframes=[], target_seconds=1.0,
                            workdir=tmp_path / "work", out=tmp_path / "vlog.mp4", still_seconds=1.0, runner=runner)
    assert str(failed.value).startswith("timelapse-full: ffmpeg exited")


async def test_without_recordings_the_keyframes_make_a_slideshow(tmp_path, day):
    _, stills = day
    frames = [render.Keyframe(tmp_path / "in/still0.jpg", T0 + dt.timedelta(minutes=k)) for k in range(5)]
    result = await render.render(segments=[], stills=stills[:1], keyframes=frames, target_seconds=1.0,
                                 workdir=tmp_path / "work", out=tmp_path / "vlog.mp4", still_seconds=1.0)
    assert (result.mode, result.frames, result.seconds) == ("slideshow", 54, 2.25)


async def test_a_sound_clip_past_the_end_of_its_file_is_dropped(tmp_path, day):
    segments, stills = day
    late = render.Still(stills[0].jpeg, stills[0].at, "x", stills[0].clip_ogg, 10.0)
    result = await render.render(segments=segments, stills=[late], keyframes=[], target_seconds=1.0,
                                 workdir=tmp_path / "work", out=tmp_path / "vlog.mp4", still_seconds=1.0)
    assert [s for s in result.skipped if s.startswith("clip ")] and result.frames == 48


def test_the_uniform_select_keeps_exactly_n_frames():
    for n_out, n_in in ((24, 48), (24, 50), (7, 100), (100, 100)):
        expr = render.uniform_select(n_out, n_in)
        assert expr == f"select='lt(mod(n*{n_out},{n_in}),{n_out})'"
        assert sum((k * n_out) % n_in < n_out for k in range(n_in)) == n_out


def test_the_web_subtitles_escape_markup():
    cues = [render.Cue(0.0, 1.0, "a <b> & --> c")]
    assert render.vtt_text(cues) == "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\na &lt;b&gt; &amp; --&gt; c\n\n"
    assert render.srt_text(cues) == "1\n00:00:00,000 --> 00:00:01,000\na <b> & --> c\n\n"


def test_a_render_error_never_carries_ffmpeg_output():
    failed = render.RenderError("assemble", 234, "Error parsing subtitle line: Bữa sáng")
    assert str(failed) == "assemble: ffmpeg exited 234" and "Bữa" in failed.stderr_tail


def test_media_checks_read_headers_only(tmp_path):
    track = render.video_track(make_mp4(tmp_path / "seg.mp4"))
    assert (track.samples, track.sync_samples, track.seconds) == (12, 3, 6.0)
    make_opus(tmp_path / "a.ogg", "sine=frequency=440:sample_rate=48000", 5)
    assert render.ogg_opus_duration(tmp_path / "a.ogg") == pytest.approx(5.0, abs=0.05)
    (tmp_path / "x.mp4").write_bytes(b"\x00\x00\x00\x08free")
    with pytest.raises(render.InvalidMedia):
        render.video_track(tmp_path / "x.mp4")

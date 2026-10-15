import datetime as dt
import hashlib
import logging

import httpx
import numpy as np
import pytest

from helpers import QUIET_SPEECH, ROOM_NOISE, fetch_one, insert_item, make_opus, new_ulid, paired_device
from loopi_brain import audio_level, media
from loopi_brain.app import create_app
from loopi_brain.pipeline.asr import load_audio

DAY = "2026-10-14"
UTC_9AM_SEOUL = dt.datetime(2026, 10, 14, 0, 30, tzinfo=dt.UTC)


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
async def web(pool, pki_settings, jobs_open):
    app = create_app(pki_settings, pool=pool)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


async def _audio(pool, settings, device_id, source=QUIET_SPEECH, seconds=10, body: bytes | None = None):
    """An audio item whose raw file is made from the lavfi ``source`` (or is ``body``); (id, raw path)."""
    item_id = await insert_item(pool, device_id, kind="audio", started_at=UTC_9AM_SEOUL, seconds=seconds)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    raw = settings.media_root / row["raw_path"]
    if body is None:
        make_opus(raw, source, seconds)
    else:
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(body)
    return item_id, raw


def _p999_db(path) -> float:
    return audio_level.dbfs(float(np.percentile(np.abs(load_audio(path)), 99.9)))


def _view(settings, raw):
    return settings.media_root / media.audio_view_path(raw.relative_to(settings.media_root))


def _leftovers(settings) -> list:
    return list(settings.media_root.rglob("*.part.opus"))


async def test_quiet_audio_plays_louder_by_the_level_gain(web, pool, pki_settings, dev):
    item_id, raw = await _audio(pool, pki_settings, dev["device_id"])
    expected = audio_level.dbfs(audio_level.level_gain(load_audio(raw)))
    r = await web.get(f"/audio/{item_id}.opus")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/ogg"
    view = _view(pki_settings, raw)
    assert r.content == view.read_bytes()
    assert expected == pytest.approx(28.0, abs=0.5)  # -34 dBFS bursts -> -6 dBFS
    assert _p999_db(view) - _p999_db(raw) == pytest.approx(expected, abs=1.0)
    assert _leftovers(pki_settings) == []


async def test_the_copy_is_made_once_and_the_raw_file_is_never_changed(web, pool, pki_settings, dev, monkeypatch):
    item_id, raw = await _audio(pool, pki_settings, dev["device_id"])
    raw_sha = hashlib.sha256(raw.read_bytes()).hexdigest()
    first = await web.get(f"/audio/{item_id}.opus")

    async def must_not_run(*_a):
        raise AssertionError("the cached copy is served")

    monkeypatch.setattr(media, "normalise_audio", must_not_run)
    second = await web.get(f"/audio/{item_id}.opus", headers={"Range": "bytes=0-99"})
    assert second.status_code == 206 and second.content == first.content[:100], "browsers seek with Range"
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == raw_sha


async def test_unknown_items_videos_and_missing_files_are_404(web, pool, pki_settings, dev):
    assert (await web.get(f"/audio/{new_ulid()}.opus")).status_code == 404
    video = await insert_item(pool, dev["device_id"], kind="video")
    assert (await web.get(f"/audio/{video}.opus")).status_code == 404
    gone = await insert_item(pool, dev["device_id"], kind="audio")  # no file written
    assert (await web.get(f"/audio/{gone}.opus")).status_code == 404


@pytest.mark.parametrize("body", [b"OggS-not-really", b""], ids=["corrupt", "zero-length"])
async def test_an_undecodable_file_falls_back_to_the_raw_bytes(web, pool, pki_settings, dev, caplog, body):
    item_id, raw = await _audio(pool, pki_settings, dev["device_id"], body=body)
    with caplog.at_level(logging.WARNING, logger="loopi_brain.web.routes"):
        r = await web.get(f"/audio/{item_id}.opus")
    assert r.status_code == 200 and r.content == body
    assert f"audio {item_id} plays at its raw level" in caplog.text
    assert not _view(pki_settings, raw).exists() and _leftovers(pki_settings) == []
    assert raw.read_bytes() == body


@pytest.mark.parametrize(
    ("name", "value"),
    [("AUDIO_TIMEOUT_S", 0.001), ("AUDIO_LIMITER", "alimiter=limit=5")],  # limit must be <= 1: ffmpeg refuses it
    ids=["ffmpeg-stuck", "encoder-fails"],
)
async def test_a_stuck_or_failing_ffmpeg_leaves_no_copy_and_the_raw_file_plays(
    web, pool, pki_settings, dev, monkeypatch, name, value
):
    item_id, raw = await _audio(pool, pki_settings, dev["device_id"])
    monkeypatch.setattr(media, name, value)
    r = await web.get(f"/audio/{item_id}.opus")
    assert r.status_code == 200 and r.content == raw.read_bytes()
    assert not _view(pki_settings, raw).exists() and _leftovers(pki_settings) == []


async def test_a_noise_only_night_segment_plays_as_a_soft_hiss(web, pool, pki_settings, dev):
    item_id, raw = await _audio(pool, pki_settings, dev["device_id"], source=ROOM_NOISE)
    assert (await web.get(f"/audio/{item_id}.opus")).status_code == 200
    rms = {name: audio_level.dbfs(float(np.sqrt(np.mean(load_audio(path) ** 2))))
           for name, path in (("raw", raw), ("view", _view(pki_settings, raw)))}
    assert rms["raw"] == pytest.approx(-55.0, abs=1.0)
    assert rms["raw"] + 5 < rms["view"] <= -38.0, f"raised, but no louder than a soft hiss: {rms}"


async def test_a_ten_minute_segment_is_normalised_whole(web, pool, pki_settings, dev):
    # The agent's longest segment (capture.segment_seconds <= 600).
    item_id, raw = await _audio(pool, pki_settings, dev["device_id"], seconds=600)
    assert (await web.get(f"/audio/{item_id}.opus")).status_code == 200
    view = load_audio(_view(pki_settings, raw))
    assert len(view) == pytest.approx(600 * audio_level.SAMPLE_RATE, abs=audio_level.SAMPLE_RATE // 10)
    tail = view[-10 * audio_level.SAMPLE_RATE:]
    assert audio_level.dbfs(float(np.percentile(np.abs(tail), 99.9))) == pytest.approx(-6.0, abs=1.0), "the end is raised"


async def test_the_timeline_plays_audio_through_the_normalising_route(web, pool, pki_settings, dev):
    item_id, _raw = await _audio(pool, pki_settings, dev["device_id"])
    text = (await web.get("/", params={"day": DAY})).text
    assert f'<audio controls preload="none" src="/audio/{item_id}.opus"></audio>' in text
    assert "/media/raw/" not in text, "the raw file is not linked"

import datetime as dt

import numpy as np
import psycopg
import pytest

from helpers import QUIET_SPEECH, T0, fetch_all, fetch_one, insert_item, make_ctx, make_opus, paired_device
from loopi_brain.pipeline import asr, dispatch, grouping, jobs, status
from loopi_brain.pipeline.asr import Region


async def _audio(pool, pki_settings, device_id, start=T0, seconds=10):
    item_id = await insert_item(pool, device_id, kind="audio", started_at=start, seconds=seconds)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    make_opus(pki_settings.media_root / row["raw_path"], seconds=seconds)
    return item_id


async def test_speech_regions_are_stored_with_their_snr_and_the_item_waits(pool, pki_settings, monkeypatch):
    dev = await paired_device(pool, pki_settings)
    item_id = await _audio(pool, pki_settings, dev["device_id"])
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [Region(1.0, 3.5), Region(6.0, 8.0)])
    groupings = []

    async def fake_group(pool, now=None):
        groupings.append(now)

    monkeypatch.setattr(grouping, "group_speech", fake_group)
    assert await asr.detect_speech(make_ctx(pool, pki_settings), item_id) == 2
    rows = await fetch_all(
        pool, "SELECT device_id, start_ts, end_ts, offset_s, length_s, state FROM speech_region ORDER BY start_ts")
    assert [(r["offset_s"], r["length_s"], r["state"]) for r in rows] == [(1.0, 2.5, "waiting"), (6.0, 2.0, "waiting")]
    assert rows[0]["start_ts"] == T0 + dt.timedelta(seconds=1) and rows[0]["device_id"] == dev["device_id"]
    item = await fetch_one(pool, "SELECT analysis_state, speech_detected_at FROM item WHERE id = %s", (item_id,))
    assert item["analysis_state"] == "pending" and item["speech_detected_at"] is not None
    assert groupings == [None]  # grouping runs right after the detection


async def test_audio_without_speech_is_done_at_once(pool, pki_settings, monkeypatch):
    dev = await paired_device(pool, pki_settings)
    item_id = await _audio(pool, pki_settings, dev["device_id"])
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [])
    assert await asr.detect_speech(make_ctx(pool, pki_settings), item_id) == 0
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "done"


async def test_detecting_again_replaces_the_regions(pool, pki_settings, monkeypatch):
    dev = await paired_device(pool, pki_settings)
    item_id = await _audio(pool, pki_settings, dev["device_id"])
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [Region(1.0, 3.0)])
    ctx = make_ctx(pool, pki_settings)
    await asr.detect_speech(ctx, item_id)
    await asr.detect_speech(ctx, item_id)
    assert (await fetch_one(pool, "SELECT count(*) AS n FROM speech_region"))["n"] == 1


async def test_a_missing_file_skips_the_item(pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    item_id = await insert_item(pool, dev["device_id"], kind="audio")
    await asr.detect_speech(make_ctx(pool, pki_settings), item_id)
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "skipped"


async def test_a_grouping_failure_does_not_fail_the_detection(pool, pki_settings, monkeypatch):
    dev = await paired_device(pool, pki_settings)
    item_id = await _audio(pool, pki_settings, dev["device_id"])
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [Region(1.0, 3.0)])

    async def broken(pool, now=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(grouping, "group_speech", broken)
    assert await asr.detect_speech(make_ctx(pool, pki_settings), item_id) == 1


async def test_the_dispatcher_starts_audio_with_detection(pool, pki_settings, jobs_open):
    dev = await paired_device(pool, pki_settings)
    item_id = await insert_item(pool, dev["device_id"], kind="audio")
    await dispatch.defer_item(pool, item_id)
    job = await fetch_one(pool, "SELECT task_name, queue_name, lock, queueing_lock, args FROM procrastinate_jobs")
    assert (job["task_name"], job["queue_name"], job["lock"]) == (jobs.DETECT_SPEECH, "audio", None)
    assert job["queueing_lock"] == f"item:{item_id}" and job["args"] == {"item_id": item_id}


def test_regions_are_found_on_the_levelled_audio_and_measured_on_the_raw_audio(tmp_path, monkeypatch):
    path = tmp_path / "quiet.opus"
    make_opus(path, QUIET_SPEECH, seconds=4)  # -34 dBFS bursts at 0.5-1.0 s, 1.5-2.0 s ...; digital silence between
    heard = []

    def regions(audio, threshold):
        heard.append((float(np.abs(audio).max()), threshold))
        return [Region(0.5, 1.0), Region(1.5, 2.0)]

    monkeypatch.setattr(asr, "speech_regions", regions)
    found = asr.detect_file(path, 20.0, 0.4)
    raw_peak = float(np.abs(asr.load_audio(path)).max())
    assert heard[0][0] > 2 * raw_peak and heard[0][1] == 0.4  # the VAD hears what Whisper will hear
    assert [r for r, _snr in found] == [Region(0.5, 1.0), Region(1.5, 2.0)]
    assert all(25 < snr < 40 for _r, snr in found)  # ~ -37 dBFS over the -70 dBFS floor, not the levelled audio


async def test_only_a_pending_audio_item_is_detected(pool, pki_settings, monkeypatch):
    dev = await paired_device(pool, pki_settings)
    ctx = make_ctx(pool, pki_settings)
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [Region(1.0, 3.0)])
    video = await insert_item(pool, dev["device_id"], kind="video")
    done = await _audio(pool, pki_settings, dev["device_id"], start=T0 + dt.timedelta(minutes=1))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (done,))
    assert [await asr.detect_speech(ctx, i) for i in (video, done, "01NOSUCHITEM")] == [0, 0, 0]
    assert await fetch_all(pool, "SELECT id FROM speech_region") == []


async def test_a_corrupt_file_records_the_error_and_fails_the_job(pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    item_id = await insert_item(pool, dev["device_id"], kind="audio")
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    (pki_settings.media_root / row["raw_path"]).parent.mkdir(parents=True, exist_ok=True)
    (pki_settings.media_root / row["raw_path"]).write_bytes(b"OggS-fake")
    with pytest.raises(ValueError):  # PyAV's InvalidDataError
        await asr.detect_speech(make_ctx(pool, pki_settings), item_id)
    item = await fetch_one(pool, "SELECT analysis_state, analysis_error FROM item WHERE id = %s", (item_id,))
    assert item["analysis_state"] == "pending" and item["analysis_error"]


async def test_an_item_deleted_during_detection_stores_nothing(pool, pki_settings, monkeypatch):
    dev = await paired_device(pool, pki_settings)
    item_id = await _audio(pool, pki_settings, dev["device_id"])

    def regions(_audio, _t):  # runs in the worker thread, like the VAD
        with psycopg.connect(pki_settings.database_url, autocommit=True) as conn:
            conn.execute("DELETE FROM item WHERE id = %s", (item_id,))
        return [Region(1.0, 3.0)]

    monkeypatch.setattr(asr, "speech_regions", regions)
    groupings = []

    async def fake_group(pool, now=None):
        groupings.append(now)

    monkeypatch.setattr(grouping, "group_speech", fake_group)
    assert await asr.detect_speech(make_ctx(pool, pki_settings), item_id) == 0
    assert await fetch_all(pool, "SELECT id FROM speech_region") == [] and groupings == []


async def test_a_failure_after_the_item_was_deleted_is_not_an_error(pool, pki_settings, monkeypatch):
    dev = await paired_device(pool, pki_settings)
    item_id = await _audio(pool, pki_settings, dev["device_id"])

    def broken(_audio, _t):
        with psycopg.connect(pki_settings.database_url, autocommit=True) as conn:
            conn.execute("DELETE FROM item WHERE id = %s", (item_id,))
        raise RuntimeError("the decoder lost its file")

    monkeypatch.setattr(asr, "speech_regions", broken)
    assert await asr.detect_speech(make_ctx(pool, pki_settings), item_id) == 0


def test_the_two_new_jobs_are_named_labelled_and_never_retried_by_hand():
    assert (jobs.DETECT_SPEECH, jobs.GROUP_SPEECH) == ("detect_speech", "group_speech")
    assert jobs.DETECT_SPEECH in jobs.PIPELINE_TASKS and jobs.TRANSCRIBE_AUDIO in jobs.PIPELINE_TASKS
    assert jobs.GROUP_SPEECH in jobs.AUTO_RERUN_TASKS and jobs.DETECT_SPEECH not in jobs.AUTO_RERUN_TASKS
    assert status.TASK_LABELS[jobs.DETECT_SPEECH] == "Tìm tiếng nói"
    assert status.TASK_LABELS[jobs.GROUP_SPEECH] == "Ghép tiếng nói giữa các mic"

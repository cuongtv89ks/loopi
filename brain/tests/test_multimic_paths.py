import asyncio
import datetime as dt

from psycopg.types.json import Jsonb

from helpers import (QUIET_SPEECH, FakeTranscriber, fetch_all, fetch_one, insert_item, make_ctx, make_opus,
                     paired_device, run_jobs)
from loopi_brain import forget, reprocess
from loopi_brain.pipeline import asr, dispatch, grouping
from loopi_brain.pipeline.asr import Region, RegionResult, SegmentResult

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
HELLO = RegionResult("vi", 0.9, [SegmentResult(0.0, 1.5, "xin chào", -0.2, 0.05, 1.2)])


async def _recorded(pool, settings, device_id, **source):
    item_id = await insert_item(pool, device_id, kind="audio", started_at=NINE, seconds=60)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    make_opus(settings.media_root / row["raw_path"], seconds=60, **source)
    return item_id


async def _done_with_region(pool, device_id) -> str:
    """A transcribed audio item (no file) with one chosen speech region; return the item id."""
    item_id = await insert_item(pool, device_id, kind="audio", started_at=NINE, seconds=60)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE item SET analysis_state = 'done', analyzed_at = now(), speech_detected_at = now() WHERE id = %s",
            (item_id,))
        await conn.execute(
            "INSERT INTO speech_region (id, item_id, device_id, start_ts, end_ts, offset_s, length_s, snr_db, state)"
            " VALUES ('r1', %s, %s, %s, %s, 1, 2, 20, 'chosen')",
            (item_id, device_id, NINE + dt.timedelta(seconds=1), NINE + dt.timedelta(seconds=3)))
    return item_id


async def test_one_sentence_heard_by_two_microphones_gives_one_transcript(pool, pki_settings, jobs_open, monkeypatch):
    """§8.2 in miniature: the whole pipeline with real jobs."""
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    for dev in (pi, cam):
        async with pool.connection() as conn:
            await conn.execute("UPDATE device SET last_heartbeat = %s, last_heartbeat_at = now() WHERE id = %s",
                               (Jsonb({"recording_audio": True}), dev["device_id"]))
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [Region(10.0, 13.0)])
    # both hear the same sound, with a loudness shape to compare (silence has none: a flat envelope matches nothing)
    items = [await _recorded(pool, pki_settings, d["device_id"], source=QUIET_SPEECH) for d in (pi, cam)]
    for item_id in items:
        await dispatch.defer_item(pool, item_id)
    await run_jobs(make_ctx(pool, pki_settings, transcriber=FakeTranscriber(HELLO)))
    assert len(await fetch_all(pool, "SELECT id FROM moment WHERE kind = 'speech'")) == 1
    states = sorted(r["state"] for r in await fetch_all(pool, "SELECT state FROM speech_region"))
    assert states == ["chosen", "covered"]
    assert {r["analysis_state"] for r in await fetch_all(pool, "SELECT analysis_state FROM item")} == {"done"}


async def test_reprocessing_transcripts_detects_again(pool, pki_settings, jobs_open, monkeypatch):
    dev = await paired_device(pool, pki_settings)
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [Region(10.0, 13.0)])
    item_id = await _recorded(pool, pki_settings, dev["device_id"])
    await dispatch.defer_item(pool, item_id)
    await run_jobs(make_ctx(pool, pki_settings, transcriber=FakeTranscriber(HELLO)))
    await reprocess.reprocess_day(pool, NINE.date(), pki_settings.zone, {"transcripts"})
    row = await fetch_one(pool, "SELECT analysis_state, analysis_job_id, speech_detected_at FROM item WHERE id = %s",
                          (item_id,))
    assert row == {"analysis_state": "pending", "analysis_job_id": None, "speech_detected_at": None}
    assert await fetch_all(pool, "SELECT id FROM speech_region") == []


async def test_forgetting_a_range_takes_its_regions(pool, pki_settings, jobs_open, monkeypatch):
    dev = await paired_device(pool, pki_settings)
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [Region(10.0, 13.0)])
    item_id = await _recorded(pool, pki_settings, dev["device_id"])
    await dispatch.defer_item(pool, item_id)
    await run_jobs(make_ctx(pool, pki_settings, transcriber=FakeTranscriber(HELLO)))
    assert await fetch_all(pool, "SELECT id FROM speech_region")  # there is something to forget
    await forget.forget_range(pool, pki_settings, NINE, NINE + dt.timedelta(minutes=2))
    assert await fetch_all(pool, "SELECT id FROM speech_region") == []


async def test_the_pipeline_page_counts_speech_waiting_for_another_microphone(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    item_id = await insert_item(pool, dev["device_id"], kind="audio", started_at=NINE)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO speech_region (id, item_id, device_id, start_ts, end_ts, offset_s, length_s, snr_db)"
            " VALUES ('r1', %s, %s, %s, %s, 1, 2, 20)",
            (item_id, dev["device_id"], NINE + dt.timedelta(seconds=1), NINE + dt.timedelta(seconds=3)))
    assert "Tiếng nói chờ ghép (mic khác hoặc phút sau): 1 đoạn" in (await client.get("/pipeline")).text


async def test_the_pipeline_page_does_not_count_decided_regions(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    await _done_with_region(pool, dev["device_id"])  # chosen: decided
    assert "Tiếng nói chờ ghép (mic khác hoặc phút sau): 0 đoạn" in (await client.get("/pipeline")).text


async def test_reprocessing_waits_for_the_grouping_lock(pool, pki_settings):
    """Grouping locks regions then items; the reset updates items and deletes regions: both take the same advisory
    lock, so they never wait for each other in a circle."""
    dev = await paired_device(pool, pki_settings)
    item_id = await _done_with_region(pool, dev["device_id"])
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(grouping.LOCK_SQL)
        task = asyncio.create_task(reprocess.reprocess_day(pool, NINE.date(), pki_settings.zone, {"transcripts"}))
        await asyncio.sleep(0.5)
        assert not task.done()
    result = await asyncio.wait_for(task, 10)
    assert result.audio_items == 1
    row = await fetch_one(pool, "SELECT analysis_state, speech_detected_at FROM item WHERE id = %s", (item_id,))
    assert row == {"analysis_state": "pending", "speech_detected_at": None}
    assert await fetch_all(pool, "SELECT id FROM speech_region") == []


async def test_forgetting_waits_for_the_grouping_lock(pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    await _done_with_region(pool, dev["device_id"])
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(grouping.LOCK_SQL)
        task = asyncio.create_task(forget.forget_range(pool, pki_settings, NINE, NINE + dt.timedelta(minutes=2)))
        await asyncio.sleep(0.5)
        assert not task.done()
    counts = await asyncio.wait_for(task, 10)
    assert counts.items == 1
    assert await fetch_all(pool, "SELECT id FROM speech_region") == []

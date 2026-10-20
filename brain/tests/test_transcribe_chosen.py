import datetime as dt

from helpers import T0, FakeTranscriber, fetch_all, fetch_one, insert_item, make_ctx, make_opus, new_ulid, paired_device
from loopi_brain.pipeline import asr, grouping
from loopi_brain.pipeline.asr import Region, RegionResult, SegmentResult

HELLO = RegionResult("vi", 0.9, [SegmentResult(0.0, 1.5, "xin chào", -0.2, 0.05, 1.2)])


async def _item(pool, settings, device_id, regions):
    item_id = await insert_item(pool, device_id, kind="audio", started_at=T0, seconds=10)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    make_opus(settings.media_root / row["raw_path"], seconds=10)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET speech_detected_at = now() WHERE id = %s", (item_id,))
        for offset, length, state in regions:
            await conn.execute(
                "INSERT INTO speech_region (id, item_id, device_id, start_ts, end_ts, offset_s, length_s, snr_db, state)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, 20, %s)",
                (new_ulid(), item_id, device_id, T0 + dt.timedelta(seconds=offset),
                 T0 + dt.timedelta(seconds=offset + length), offset, length, state),
            )
    return item_id


async def test_only_the_chosen_regions_reach_whisper(pool, pki_settings, jobs_open):
    dev = await paired_device(pool, pki_settings)
    item_id = await _item(pool, pki_settings, dev["device_id"], [(1.0, 2.0, "chosen"), (5.0, 2.0, "covered")])
    whisper = FakeTranscriber(HELLO)
    assert await asr.transcribe_audio(make_ctx(pool, pki_settings, transcriber=whisper), item_id) == 1
    assert whisper.chunks == [2 * asr.SAMPLE_RATE]
    moment = await fetch_one(pool, "SELECT id, ts_start, text FROM moment")
    assert moment["text"] == "xin chào" and moment["ts_start"] == T0 + dt.timedelta(seconds=1)
    region = await fetch_one(pool, "SELECT moment_id FROM speech_region WHERE state = 'chosen'")
    assert region["moment_id"] == moment["id"]
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "done"


async def test_transcribing_again_replaces_the_transcript(pool, pki_settings, jobs_open):
    dev = await paired_device(pool, pki_settings)
    item_id = await _item(pool, pki_settings, dev["device_id"], [(1.0, 2.0, "chosen")])
    ctx = make_ctx(pool, pki_settings, transcriber=FakeTranscriber(HELLO))
    await asr.transcribe_audio(ctx, item_id)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'pending' WHERE id = %s", (item_id,))
    await asr.transcribe_audio(ctx, item_id)
    assert len(await fetch_all(pool, "SELECT id FROM moment")) == 1


async def test_a_job_from_before_this_version_detects_first(pool, pki_settings, monkeypatch):
    dev = await paired_device(pool, pki_settings)
    item_id = await insert_item(pool, dev["device_id"], kind="audio", started_at=T0, seconds=10)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    make_opus(pki_settings.media_root / row["raw_path"], seconds=10)
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [Region(1.0, 3.0)])

    async def no_grouping(pool, now=None):
        return None

    monkeypatch.setattr(grouping, "group_speech", no_grouping)
    whisper = FakeTranscriber(HELLO)
    await asr.transcribe_audio(make_ctx(pool, pki_settings, transcriber=whisper), item_id)
    assert whisper.chunks == []  # detected, now waiting for grouping; nothing lost
    assert (await fetch_one(pool, "SELECT state FROM speech_region"))["state"] == "waiting"
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "pending"


async def test_a_stray_job_leaves_an_item_whose_regions_are_still_waiting(pool, pki_settings, jobs_open):
    dev = await paired_device(pool, pki_settings)
    item_id = await _item(pool, pki_settings, dev["device_id"], [(1.0, 2.0, "waiting")])
    whisper = FakeTranscriber(HELLO)
    assert await asr.transcribe_audio(make_ctx(pool, pki_settings, transcriber=whisper), item_id) == 0
    assert whisper.chunks == []
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "pending"
    assert (await fetch_one(pool, "SELECT state FROM speech_region"))["state"] == "waiting"


async def test_a_chosen_item_whose_file_is_gone_is_skipped(pool, pki_settings, jobs_open):
    dev = await paired_device(pool, pki_settings)
    item_id = await _item(pool, pki_settings, dev["device_id"], [(1.0, 2.0, "chosen")])
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    (pki_settings.media_root / row["raw_path"]).unlink()
    whisper = FakeTranscriber(HELLO)
    assert await asr.transcribe_audio(make_ctx(pool, pki_settings, transcriber=whisper), item_id) == 0
    assert whisper.chunks == []
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "skipped"


async def test_a_stray_job_waits_even_when_only_some_regions_are_decided(pool, pki_settings, jobs_open):
    dev = await paired_device(pool, pki_settings)
    item_id = await _item(pool, pki_settings, dev["device_id"], [(1.0, 2.0, "chosen"), (5.0, 2.0, "waiting")])
    whisper = FakeTranscriber(HELLO)
    assert await asr.transcribe_audio(make_ctx(pool, pki_settings, transcriber=whisper), item_id) == 0
    assert whisper.chunks == []  # the item is transcribed once, when grouping has decided every region
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "pending"

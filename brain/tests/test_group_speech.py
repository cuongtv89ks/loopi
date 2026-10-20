import datetime as dt

from psycopg.types.json import Jsonb

from helpers import fetch_all, fetch_one, insert_item, new_ulid, paired_device
from loopi_brain.pipeline import grouping, jobs

M = dt.datetime(2026, 10, 21, 1, 0, tzinfo=dt.UTC)  # a minute's start
NOW = M + dt.timedelta(minutes=1, seconds=5)


async def _hb(pool, device_id, *, recording_audio=True, at=NOW):
    """A heartbeat 5 s before ``at`` (the grouping time it must be fresh for)."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE device SET last_heartbeat = %s, last_heartbeat_at = %s WHERE id = %s",
            (Jsonb({"recording_audio": recording_audio}), at - dt.timedelta(seconds=5), device_id),
        )


async def _detected(pool, device_id, regions, *, start=M, seconds=60, received=None, detected=True):
    """An audio item whose speech detection found ``regions`` [(offset, length, snr)]; return (item, region ids)."""
    item_id = await insert_item(pool, device_id, kind="audio", started_at=start, seconds=seconds)
    ids = []
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE item SET received_at = %s, speech_detected_at = %s WHERE id = %s",
            (received or start + dt.timedelta(seconds=seconds, milliseconds=500), NOW if detected else None, item_id),
        )
        for offset, length, snr in regions:
            ids.append(new_ulid())
            await conn.execute(
                "INSERT INTO speech_region (id, item_id, device_id, start_ts, end_ts, offset_s, length_s, snr_db)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (ids[-1], item_id, device_id, start + dt.timedelta(seconds=offset),
                 start + dt.timedelta(seconds=offset + length), offset, length, snr),
            )
    return item_id, ids


async def _states(pool):
    return {r["id"]: (r["state"], r["covered_by"])
            for r in await fetch_all(pool, "SELECT id, state, covered_by FROM speech_region")}


async def test_one_microphone_is_decided_without_waiting(pool, pki_settings, jobs_open):
    pi = await paired_device(pool, pki_settings)
    await _hb(pool, pi["device_id"])
    item, (region,) = await _detected(pool, pi["device_id"], [(10, 3, 20.0)])
    result = await grouping.group_speech(pool, now=NOW)
    assert await _states(pool) == {region: ("chosen", None)}
    assert result.transcribing == [item]
    job = await fetch_one(pool, "SELECT task_name, lock, queueing_lock, args FROM procrastinate_jobs")
    assert (job["task_name"], job["lock"], job["queueing_lock"]) == (jobs.TRANSCRIBE_AUDIO, "asr", f"asr:{item}")
    assert (await fetch_one(pool, "SELECT analysis_job_id FROM item WHERE id = %s", (item,)))["analysis_job_id"]


async def test_speech_at_a_minutes_end_waits_for_the_next_segment(pool, pki_settings, jobs_open):
    pi = await paired_device(pool, pki_settings)
    await _hb(pool, pi["device_id"])
    item, (region,) = await _detected(pool, pi["device_id"], [(57.5, 2.5, 20.0)])  # runs into the next minute
    await grouping.group_speech(pool, now=NOW)
    assert await _states(pool) == {region: ("waiting", None)}
    nxt, _ = await _detected(pool, pi["device_id"], [], start=M + dt.timedelta(minutes=1))
    await grouping.group_speech(pool, now=NOW + dt.timedelta(minutes=1))
    assert (await _states(pool))[region] == ("chosen", None)


async def test_two_microphones_wait_for_each_other_then_keep_the_clearer(pool, pki_settings, jobs_open):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await _hb(pool, pi["device_id"])
    await _hb(pool, cam["device_id"])
    pi_item, (pi_region,) = await _detected(pool, pi["device_id"], [(10, 3, 22.0)])
    await grouping.group_speech(pool, now=NOW)
    assert (await _states(pool))[pi_region] == ("waiting", None)  # the camera's minute has not arrived
    cam_item, (cam_region,) = await _detected(pool, cam["device_id"], [(10.8, 3, 15.0)])
    result = await grouping.group_speech(pool, now=NOW + dt.timedelta(seconds=20))
    assert await _states(pool) == {pi_region: ("chosen", None), cam_region: ("covered", pi_region)}
    assert result.transcribing == [pi_item] and result.finished == [cam_item]
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (cam_item,)))["analysis_state"] == "done"


async def test_a_silent_microphone_is_waited_for_three_minutes_only(pool, pki_settings, jobs_open):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    arrived = M + dt.timedelta(minutes=1, seconds=1)
    _, (region,) = await _detected(pool, pi["device_id"], [(10, 3, 22.0)], received=arrived)
    almost, late = arrived + dt.timedelta(minutes=2, seconds=59), arrived + dt.timedelta(minutes=3)
    for at in (almost, late):  # the camera keeps saying it records, but its audio never comes
        await _hb(pool, pi["device_id"], at=at)
        await _hb(pool, cam["device_id"], at=at)
        await grouping.group_speech(pool, now=at)
        assert (await _states(pool))[region] == (("waiting" if at == almost else "chosen"), None)


async def test_a_microphone_that_stopped_recording_is_not_waited_for(pool, pki_settings, jobs_open):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await _hb(pool, pi["device_id"])
    await _hb(pool, cam["device_id"], recording_audio=False)  # paused
    _, (region,) = await _detected(pool, pi["device_id"], [(10, 3, 22.0)])
    await grouping.group_speech(pool, now=NOW)
    assert (await _states(pool))[region] == ("chosen", None)


async def test_a_microphone_still_detecting_its_audio_is_waited_for(pool, pki_settings, jobs_open):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await _hb(pool, pi["device_id"])
    await _hb(pool, cam["device_id"])
    _, (region,) = await _detected(pool, pi["device_id"], [(10, 3, 22.0)])
    await _detected(pool, cam["device_id"], [], detected=False)  # arrived, detection still running
    await grouping.group_speech(pool, now=NOW)
    assert (await _states(pool))[region] == ("waiting", None)


async def test_late_audio_does_not_change_the_decision(pool, pki_settings, jobs_open):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await _hb(pool, cam["device_id"], recording_audio=False)
    await _hb(pool, pi["device_id"])
    _, (pi_region,) = await _detected(pool, pi["device_id"], [(10, 3, 12.0)])
    await grouping.group_speech(pool, now=NOW)
    assert (await _states(pool))[pi_region] == ("chosen", None)
    late_item, (late_region,) = await _detected(pool, cam["device_id"], [(10.5, 3, 30.0)],
                                                received=NOW + dt.timedelta(minutes=10))
    await grouping.group_speech(pool, now=NOW + dt.timedelta(minutes=10))
    assert await _states(pool) == {pi_region: ("chosen", None), late_region: ("covered", pi_region)}


async def test_an_item_is_not_queued_twice(pool, pki_settings, jobs_open):
    pi = await paired_device(pool, pki_settings)
    await _hb(pool, pi["device_id"])
    await _detected(pool, pi["device_id"], [(10, 3, 20.0)])
    await grouping.group_speech(pool, now=NOW)
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'doing'")  # Whisper is running
    await grouping.group_speech(pool, now=NOW + dt.timedelta(minutes=1))
    assert (await fetch_one(pool, "SELECT count(*) AS n FROM procrastinate_jobs"))["n"] == 1


async def test_speech_at_a_minutes_end_waits_three_minutes_for_a_next_segment_that_never_comes(
        pool, pki_settings, jobs_open):
    pi = await paired_device(pool, pki_settings)
    _, (region,) = await _detected(pool, pi["device_id"], [(57.5, 2.5, 20.0)])
    end = M + dt.timedelta(minutes=2)  # the end of the next minute, which never delivers
    await _hb(pool, pi["device_id"], at=end + dt.timedelta(minutes=2, seconds=59))
    await grouping.group_speech(pool, now=end + dt.timedelta(minutes=2, seconds=59))
    assert (await _states(pool))[region] == ("waiting", None)  # still recording: the next segment may yet arrive
    await _hb(pool, pi["device_id"], at=end + dt.timedelta(minutes=3))
    await grouping.group_speech(pool, now=end + dt.timedelta(minutes=3))
    assert (await _states(pool))[region] == ("chosen", None)


async def test_speech_at_a_minutes_end_is_decided_at_once_when_no_microphone_records(pool, pki_settings, jobs_open):
    pi = await paired_device(pool, pki_settings)
    await _hb(pool, pi["device_id"], recording_audio=False)  # paused after this segment
    _, (region,) = await _detected(pool, pi["device_id"], [(57.5, 2.5, 20.0)])
    await grouping.group_speech(pool, now=NOW)
    assert (await _states(pool))[region] == ("chosen", None)


async def test_the_periodic_job_groups_with_the_pool_of_the_current_context(pool, pki_settings, jobs_open):
    from helpers import make_ctx
    from loopi_brain.pipeline.context import set_context

    pi = await paired_device(pool, pki_settings)
    item, (region,) = await _detected(pool, pi["device_id"], [(10, 3, 20.0)])
    set_context(make_ctx(pool, pki_settings))
    try:
        await grouping.group_speech_job(0)
    finally:
        set_context(None)
    assert (await _states(pool))[region] == ("chosen", None)
    assert (await fetch_one(pool, "SELECT task_name FROM procrastinate_jobs"))["task_name"] == jobs.TRANSCRIBE_AUDIO
    task = jobs.job_app.tasks[jobs.GROUP_SPEECH]
    assert (task.queue, task.queueing_lock) == ("light", "group_speech")

import asyncio
import datetime as dt

import pytest
from psycopg.types.json import Jsonb

from helpers import fetch_all, fetch_one, insert_item, new_ulid, paired_device
from loopi_brain.pipeline import asr, dispatch, grouping, jobs

M = dt.datetime(2026, 10, 21, 1, 0, tzinfo=dt.UTC)  # a minute's start
NOW = M + dt.timedelta(minutes=1, seconds=5)
TAIL = dt.timedelta(milliseconds=20)  # the agent cuts a segment at the first frame at or after the boundary


async def _hb(pool, device_id, *, recording_audio=True, at=NOW, age=5, **flags):
    """A heartbeat ``age`` seconds before ``at`` (the grouping time it must be fresh for)."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE device SET last_heartbeat = %s, last_heartbeat_at = %s WHERE id = %s",
            (Jsonb({"recording_audio": recording_audio, **flags}), at - dt.timedelta(seconds=age), device_id),
        )


async def _regions(conn, item_id, device_id, start, regions):
    ids = []
    for offset, length, snr in regions:
        ids.append(new_ulid())
        await conn.execute(
            "INSERT INTO speech_region (id, item_id, device_id, start_ts, end_ts, offset_s, length_s, snr_db)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (ids[-1], item_id, device_id, start + dt.timedelta(seconds=offset),
             start + dt.timedelta(seconds=offset + length), offset, length, snr),
        )
    return ids


async def _detected(pool, device_id, regions, *, start=M, seconds=60, received=None, detected=True):
    """An audio item (ending ``TAIL`` after its nominal length, like the agent's) whose speech detection found
    ``regions`` [(offset, length, snr)]; return (item, region ids)."""
    item_id = await insert_item(pool, device_id, kind="audio", started_at=start, seconds=seconds)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE item SET ended_at = %s, received_at = %s, speech_detected_at = %s WHERE id = %s",
            (start + dt.timedelta(seconds=seconds) + TAIL,
             received or start + dt.timedelta(seconds=seconds, milliseconds=500), NOW if detected else None, item_id),
        )
        ids = await _regions(conn, item_id, device_id, start, regions)
    return item_id, ids


async def _detect_now(pool, item_id, device_id, regions, *, start=M):
    """The detection of an item that had arrived but was not detected yet finishes with ``regions``."""
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET speech_detected_at = %s WHERE id = %s", (NOW, item_id))
        return await _regions(conn, item_id, device_id, start, regions)


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
    cam_row = await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (cam_item,))
    assert cam_row["analysis_state"] == "done"


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


async def test_a_lagging_worker_still_waits_for_audio_that_has_arrived(pool, pki_settings, jobs_open):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    arrived = M + dt.timedelta(minutes=1, seconds=0.5)
    lag = arrived + dt.timedelta(minutes=3, seconds=1)  # the worker reaches minute M's detections 3 min late
    for device in (pi, cam):
        await _hb(pool, device["device_id"], at=lag)
    _, (pi_region,) = await _detected(pool, pi["device_id"], [(10, 3, 10.0)], received=arrived)
    cam_item, _ = await _detected(pool, cam["device_id"], [], received=arrived, detected=False)  # detection is next
    await grouping.group_speech(pool, now=lag)  # the pass at the end of the Pi's detection
    assert (await _states(pool))[pi_region] == ("waiting", None)  # WAIT excuses audio that has not arrived, not this
    (cam_region,) = await _detect_now(pool, cam_item, cam["device_id"], [(10.8, 3, 30.0)])  # it heard better
    await grouping.group_speech(pool, now=lag + dt.timedelta(seconds=3))
    assert await _states(pool) == {cam_region: ("chosen", None), pi_region: ("covered", cam_region)}


async def test_a_dead_lettered_detection_is_not_waited_for(pool, pki_settings, jobs_open):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await _hb(pool, pi["device_id"])
    await _hb(pool, cam["device_id"])
    _, (region,) = await _detected(pool, pi["device_id"], [(10, 3, 22.0)])
    cam_item, _ = await _detected(pool, cam["device_id"], [], detected=False)
    job = await dispatch.defer_item(pool, cam_item)  # the camera's detection job: queued, then dead-lettered
    await grouping.group_speech(pool, now=NOW)
    assert (await _states(pool))[region] == ("waiting", None)
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed' WHERE id = %s", (job,))
    await grouping.group_speech(pool, now=NOW)
    assert (await _states(pool))[region] == ("chosen", None)


async def test_the_next_segments_tail_does_not_shorten_the_wait(pool, pki_settings, jobs_open):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    before = M - dt.timedelta(minutes=1)  # the previous segments end 20 ms into minute M and arrived early
    for device in (pi, cam):
        await _detected(pool, device["device_id"], [], start=before, received=M + dt.timedelta(seconds=0.5))
    arrived = M + dt.timedelta(minutes=1, seconds=0.5)
    _, (region,) = await _detected(pool, pi["device_id"], [(10, 3, 22.0)], received=arrived)  # the camera's never comes
    for at, state in ((arrived + dt.timedelta(minutes=2, seconds=1), "waiting"),
                      (arrived + dt.timedelta(minutes=2, seconds=59), "waiting"),
                      (arrived + dt.timedelta(minutes=3), "chosen")):
        for device in (pi, cam):
            await _hb(pool, device["device_id"], at=at)
        await grouping.group_speech(pool, now=at)
        assert (await _states(pool))[region] == (state, None)


@pytest.mark.parametrize(("seconds", "state"), [(58.5, "chosen"), (57.0, "waiting")])
async def test_an_item_ending_within_two_seconds_of_the_boundary_has_delivered_the_minute(
        pool, pki_settings, jobs_open, seconds, state):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await _hb(pool, pi["device_id"])
    await _hb(pool, cam["device_id"])
    _, (region,) = await _detected(pool, pi["device_id"], [(10, 3, 22.0)])
    await _detected(pool, cam["device_id"], [], seconds=seconds)  # ends 1.5 s / 3 s before the minute's end
    await grouping.group_speech(pool, now=NOW)
    assert (await _states(pool))[region] == (state, None)


@pytest.mark.parametrize(("age", "state"), [(30, "waiting"), (50, "chosen")])
async def test_a_camera_heard_from_within_45_seconds_is_waited_for(pool, pki_settings, jobs_open, age, state):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await _hb(pool, pi["device_id"])
    await _hb(pool, cam["device_id"], age=age)
    _, (region,) = await _detected(pool, pi["device_id"], [(10, 3, 22.0)])
    await grouping.group_speech(pool, now=NOW)
    assert (await _states(pool))[region] == (state, None)


async def test_a_camera_without_a_microphone_is_not_waited_for(pool, pki_settings, jobs_open):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await _hb(pool, pi["device_id"])
    await _hb(pool, cam["device_id"], mic_missing=True)  # its sound keeps failing, so recording_audio comes and goes
    _, (region,) = await _detected(pool, pi["device_id"], [(10, 3, 22.0)])
    await grouping.group_speech(pool, now=NOW)
    assert (await _states(pool))[region] == ("chosen", None)


async def test_a_dead_lettered_transcription_is_not_queued_again_but_a_finished_one_is(pool, pki_settings, jobs_open):
    pi = await paired_device(pool, pki_settings)
    await _hb(pool, pi["device_id"])
    item, _ = await _detected(pool, pi["device_id"], [(10, 3, 20.0)])
    first = (await grouping.group_speech(pool, now=NOW)).transcribing
    assert first == [item]
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed'")  # on the dead-letter list
    assert (await grouping.group_speech(pool, now=NOW + dt.timedelta(minutes=1))).transcribing == []
    assert (await fetch_one(pool, "SELECT count(*) AS n FROM procrastinate_jobs"))["n"] == 1
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'succeeded'")  # done, yet the item is pending again
    assert (await grouping.group_speech(pool, now=NOW + dt.timedelta(minutes=2))).transcribing == [item]
    jobs_now = await fetch_all(pool, "SELECT id, status FROM procrastinate_jobs ORDER BY id")
    assert [j["status"] for j in jobs_now] == ["succeeded", "todo"]
    row = await fetch_one(pool, "SELECT analysis_job_id FROM item WHERE id = %s", (item,))
    assert row["analysis_job_id"] == jobs_now[1]["id"]


async def test_a_one_hour_item_reaching_into_the_minute_is_still_waited_for(pool, pki_settings, jobs_open):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await _hb(pool, pi["device_id"])
    _, (region,) = await _detected(pool, pi["device_id"], [(10, 3, 22.0)])
    await _detected(pool, cam["device_id"], [], start=M + dt.timedelta(seconds=30) - dt.timedelta(hours=1),
                    seconds=3600, detected=False)  # the longest item an upload may be, ending in the minute
    await grouping.group_speech(pool, now=NOW)
    assert (await _states(pool))[region] == ("waiting", None)


def test_the_decided_regions_lookup_reaches_back_further_than_the_longest_region():
    assert grouping.MAX_REGION >= dt.timedelta(seconds=asr.MAX_REGION_S)  # asr imports grouping: the bound is local


async def test_grouping_takes_the_advisory_lock_the_other_writers_share(pool, pki_settings, jobs_open):
    pi = await paired_device(pool, pki_settings)
    await _hb(pool, pi["device_id"])
    _, (region,) = await _detected(pool, pi["device_id"], [(10, 3, 20.0)])
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(grouping.LOCK_SQL)  # forget / reprocess (plan Task 6) hold it while they rewrite regions
        task = asyncio.create_task(grouping.group_speech(pool, now=NOW))
        await asyncio.sleep(0.5)
        assert not task.done()
        assert (await _states(pool))[region] == ("waiting", None)
    await asyncio.wait_for(task, 10)
    assert (await _states(pool))[region] == ("chosen", None)

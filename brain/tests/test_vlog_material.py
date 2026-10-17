import datetime as dt

import pytest

from helpers import insert_item, insert_keyframe, insert_moment, paired_device
from loopi_brain.vlog import material

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
DAY = dt.date(2026, 10, 14)


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


async def _video(pool, device_id, start, *, view=True, done=True, expired=False):
    item = await insert_item(pool, device_id, kind="video", started_at=start, seconds=60)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE item SET analysis_state = %s, view_path = %s, raw_deleted_at = %s WHERE id = %s",
            ("done" if done else "pending", f"view/{device_id}/{item}.mp4" if view else None,
             start if expired else None, item),
        )
    return item


async def _caption(pool, keyframe, caption):
    from psycopg.types.json import Jsonb

    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption = %s WHERE id = %s", (Jsonb(caption), keyframe))


async def test_only_recordings_near_a_moment_and_still_kept_count_as_active(pool, pki_settings, dev):
    device_id = dev["device_id"]
    near = await _video(pool, device_id, NINE)
    await _video(pool, device_id, NINE + dt.timedelta(hours=3))  # nothing happened within 5 minutes
    await _video(pool, device_id, NINE + dt.timedelta(minutes=2), view=False)  # no MP4 yet
    await _video(pool, device_id, NINE + dt.timedelta(minutes=3), done=False)  # not analysed
    await _video(pool, device_id, NINE + dt.timedelta(minutes=4), expired=True)  # raw deleted by retention
    await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=6), text="x")
    found = await material.vlog_material(pool, DAY, pki_settings.zone)
    assert [s.item_id for s in found.segments] == [near]
    assert found.segments[0].path.endswith(".mp4") and found.segments[0].start == NINE


async def test_a_day_with_nothing_is_empty(pool, pki_settings, dev):
    found = await material.vlog_material(pool, DAY, pki_settings.zone)
    assert found.empty and found.candidates == []


async def test_candidates_markers_notable_pictures_and_dense_talk(pool, pki_settings, dev):
    device_id = dev["device_id"]
    video = await _video(pool, device_id, NINE)
    frames = {n: await insert_keyframe(pool, pki_settings, video, device_id, NINE + dt.timedelta(minutes=n))
              for n in (0, 20, 40, 60)}
    await insert_moment(pool, device_id, kind="marker", ts=NINE + dt.timedelta(minutes=20, seconds=10), text="Mốc")
    await _caption(pool, frames[40], {"activity": "nấu ăn", "scene": "bếp", "notable": True})
    await _caption(pool, frames[0], {"activity": "ngồi", "scene": "phòng khách", "notable": False})
    for k in range(7):  # 7 speech moments within 5 minutes around minute 60
        await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=59, seconds=20 * k),
                            text="câu dài nhất ở đây" if k == 3 else "ừ")
    found = await material.vlog_material(pool, DAY, pki_settings.zone)
    kinds = [(c.ref, c.kind, c.frame.keyframe_id) for c in found.candidates]
    assert kinds == [("H1", "marker", frames[20]), ("H2", "notable", frames[40]), ("H3", "talk", frames[60])]
    marker, notable, talk = found.candidates
    assert (marker.score, notable.score, notable.text) == (3.0, 2.0, "nấu ăn")
    assert talk.score == pytest.approx(1.7) and talk.text == "câu dài nhất ở đây"


async def test_close_candidates_merge_and_the_higher_score_stays(pool, pki_settings, dev):
    device_id = dev["device_id"]
    video = await _video(pool, device_id, NINE)
    frame = await insert_keyframe(pool, pki_settings, video, device_id, NINE)
    await _caption(pool, frame, {"activity": "a", "notable": True})
    marker = await insert_moment(pool, device_id, kind="marker", ts=NINE + dt.timedelta(seconds=30), text="m")
    found = await material.vlog_material(pool, DAY, pki_settings.zone)
    assert [c.kind for c in found.candidates] == ["marker"] and marker


async def test_a_marker_without_a_picture_nearby_is_no_candidate(pool, pki_settings, dev):
    device_id = dev["device_id"]
    video = await _video(pool, device_id, NINE)
    await insert_keyframe(pool, pki_settings, video, device_id, NINE)
    await insert_moment(pool, device_id, kind="marker", ts=NINE + dt.timedelta(minutes=5), text="m")
    found = await material.vlog_material(pool, DAY, pki_settings.zone)
    assert found.candidates == []


async def test_talk_sound_comes_from_the_longest_line_when_its_raw_audio_is_kept(pool, pki_settings, dev):
    device_id = dev["device_id"]
    video = await _video(pool, device_id, NINE)
    await insert_keyframe(pool, pki_settings, video, device_id, NINE + dt.timedelta(minutes=1))
    audio = await insert_item(pool, device_id, kind="audio", started_at=NINE, seconds=600)
    for k in range(6):
        await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(seconds=30 * k), item_id=audio,
                            text="dài nhất" if k == 2 else "x")
    [talk] = (await material.vlog_material(pool, DAY, pki_settings.zone)).candidates
    assert (talk.audio_item, talk.audio_offset) == (audio, 59.5)  # 0.5 s before the line starts
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE id = %s", (audio,))
    [talk] = (await material.vlog_material(pool, DAY, pki_settings.zone)).candidates
    assert talk.audio_item is None


async def test_at_most_40_candidates_named_in_time_order(pool, pki_settings, dev):
    device_id = dev["device_id"]
    video = await _video(pool, device_id, NINE)
    for n in range(45):
        frame = await insert_keyframe(pool, pki_settings, video, device_id, NINE + dt.timedelta(minutes=3 * n))
        await _caption(pool, frame, {"activity": f"a{n}", "notable": True})
    found = await material.vlog_material(pool, DAY, pki_settings.zone)
    assert len(found.candidates) == material.CANDIDATES_MAX
    assert [c.ref for c in found.candidates] == [f"H{k}" for k in range(1, 41)]
    assert [c.at for c in found.candidates] == sorted(c.at for c in found.candidates)


async def test_the_slideshow_frames_are_spread_over_the_day(pool, pki_settings, dev, monkeypatch):
    monkeypatch.setattr(material, "SLIDES_MAX", 5)
    device_id = dev["device_id"]
    video = await _video(pool, device_id, NINE, expired=True)
    for n in range(12):
        await insert_keyframe(pool, pki_settings, video, device_id, NINE + dt.timedelta(minutes=n))
    found = await material.vlog_material(pool, DAY, pki_settings.zone)
    assert found.segments == [] and len(found.frames) == 5 and not found.empty
    assert [f.at for f in found.frames] == [NINE + dt.timedelta(minutes=m) for m in (0, 2, 4, 7, 9)]  # llm.spread


def test_talk_windows_need_six_lines_within_five_minutes():
    t0 = NINE
    rows = [{"ts_start": t0 + dt.timedelta(seconds=50 * k)} for k in range(6)]  # 6 lines over 250 s
    assert len(material.talk_windows(rows)) == 1
    spread_out = [{"ts_start": t0 + dt.timedelta(seconds=70 * k)} for k in range(6)]  # 350 s: no window
    assert material.talk_windows(spread_out) == []

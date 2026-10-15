import datetime as dt

import pytest

from helpers import NIGHT, T0, TWO_SCENES, fetch_all, fetch_one, insert_item, jobs_by_lock, make_ctx, make_h264, paired_device
from loopi_brain.pipeline import context, video
from loopi_brain.pipeline.frames import FrameError


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def ctx(pool, pki_settings):
    return make_ctx(pool, pki_settings)


async def _video_item(pool, settings, device_id, source, seconds, started_at=T0) -> str:
    item_id = await insert_item(pool, device_id, kind="video", started_at=started_at, seconds=seconds)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    make_h264(settings.media_root / row["raw_path"], source, seconds)
    return item_id


def _jpegs(settings) -> list:
    root = settings.media_root / "keyframes"
    return sorted(root.rglob("*.jpg")) if root.exists() else []


async def test_new_scenes_become_keyframes_with_caption_jobs(pool, pki_settings, dev, ctx, jobs_open):
    item_id = await _video_item(pool, pki_settings, dev["device_id"], TWO_SCENES, 80)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_error = 'old failure' WHERE id = %s", (item_id,))
    ids = await video.analyze_video(ctx, item_id)
    rows = await fetch_all(pool, "SELECT * FROM keyframe WHERE item_id = %s ORDER BY ts", (item_id,))
    assert [r["id"] for r in rows] == ids and len(ids) == 2
    assert [r["ts"] for r in rows] == [T0, T0 + dt.timedelta(seconds=40.5)]
    assert [r["frame_index"] for r in rows] == [0, 81]
    for row in rows:
        assert row["path"] == f"keyframes/{dev['device_id']}/2026/10/14/{row['id']}.jpg"
        assert (pki_settings.media_root / row["path"]).is_file()
        assert row["device_id"] == dev["device_id"] and row["luma"] > 24 and row["caption"] is None
    item = await fetch_one(pool, "SELECT analysis_state, analyzed_at, analysis_error FROM item WHERE id = %s", (item_id,))
    assert item["analysis_state"] == "done" and item["analyzed_at"] is not None and item["analysis_error"] is None
    by_lock = await jobs_by_lock(pool)
    for row in await fetch_all(pool, "SELECT id, caption_job_id FROM keyframe"):
        assert by_lock[f"kf:{row['id']}"][0]["id"] == row["caption_job_id"]


async def test_night_segment_gives_no_keyframes_and_no_caption_jobs(pool, pki_settings, dev, ctx, jobs_open):
    item_id = await _video_item(pool, pki_settings, dev["device_id"], NIGHT, 60)
    assert await video.analyze_video(ctx, item_id) == []
    assert await fetch_all(pool, "SELECT id FROM keyframe") == []
    assert await jobs_by_lock(pool) == {}
    assert _jpegs(pki_settings) == []
    item = await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,))
    assert item["analysis_state"] == "done"


async def test_next_segment_compares_with_the_previous_keyframe_of_the_device(pool, pki_settings, dev, ctx, jobs_open):
    first = await _video_item(pool, pki_settings, dev["device_id"], TWO_SCENES, 80)
    await video.analyze_video(ctx, first)
    same_scene = await _video_item(pool, pki_settings, dev["device_id"], "smptebars=size=640x360:rate=2", 60,
                                   started_at=T0 + dt.timedelta(seconds=80))
    assert await video.analyze_video(ctx, same_scene) == []  # still the bars of the last keyframe
    other = await paired_device(pool, pki_settings)
    elsewhere = await _video_item(pool, pki_settings, other["device_id"], "smptebars=size=640x360:rate=2", 60,
                                  started_at=T0 + dt.timedelta(seconds=80))
    assert len(await video.analyze_video(ctx, elsewhere)) == 1  # another device has its own history


async def test_rerun_never_creates_a_second_set_of_keyframes(pool, pki_settings, dev, ctx, jobs_open):
    item_id = await _video_item(pool, pki_settings, dev["device_id"], TWO_SCENES, 80)
    first = await video.analyze_video(ctx, item_id)
    assert await video.analyze_video(ctx, item_id) == []  # already done
    async with pool.connection() as conn:  # crash after the commit, before the job was acknowledged
        await conn.execute("UPDATE item SET analysis_state = 'pending' WHERE id = %s", (item_id,))
    assert await video.analyze_video(ctx, item_id) == []
    assert [r["id"] for r in await fetch_all(pool, "SELECT id FROM keyframe ORDER BY ts")] == first
    assert len(_jpegs(pki_settings)) == 2
    item = await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,))
    assert item["analysis_state"] == "done"


async def test_missing_raw_file_or_failed_remux_is_skipped(pool, pki_settings, dev, ctx, jobs_open):
    gone = await insert_item(pool, dev["device_id"], kind="video")
    broken = await _video_item(pool, pki_settings, dev["device_id"], TWO_SCENES, 4, started_at=T0 + dt.timedelta(hours=1))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET status = 'error' WHERE id = %s", (broken,))
    for item_id in (gone, broken):
        assert await video.analyze_video(ctx, item_id) == []
        row = await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,))
        assert row["analysis_state"] == "skipped"


async def test_other_kinds_and_unknown_ids_are_ignored(pool, pki_settings, dev, ctx):
    audio = await insert_item(pool, dev["device_id"], kind="audio")
    assert await video.analyze_video(ctx, audio) == []
    assert await video.analyze_video(ctx, "01NOSUCHITEM0000000000000") == []
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (audio,)))["analysis_state"] == "pending"


async def test_undecodable_video_records_the_error_and_raises(pool, pki_settings, dev, ctx):
    item_id = await insert_item(pool, dev["device_id"], kind="video")
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    raw = pki_settings.media_root / row["raw_path"]
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"definitely not h264")
    with pytest.raises(FrameError):
        await video.analyze_video(ctx, item_id)
    item = await fetch_one(pool, "SELECT analysis_state, analysis_error FROM item WHERE id = %s", (item_id,))
    assert item["analysis_state"] == "pending" and item["analysis_error"].startswith("FrameError: ")
    assert len(item["analysis_error"]) <= 500


async def test_failed_transaction_removes_the_written_jpegs(pool, pki_settings, dev, ctx, monkeypatch):
    item_id = await _video_item(pool, pki_settings, dev["device_id"], TWO_SCENES, 80)
    monkeypatch.setattr(video.devices, "new_id", lambda: "01SAMEIDFORBOTHKEYFRAMES00")
    with pytest.raises(Exception, match="duplicate key"):
        await video.analyze_video(ctx, item_id)
    assert await fetch_all(pool, "SELECT id FROM keyframe") == []
    assert _jpegs(pki_settings) == []
    item = await fetch_one(pool, "SELECT analysis_state, analysis_error FROM item WHERE id = %s", (item_id,))
    assert item["analysis_state"] == "pending" and "duplicate key" in item["analysis_error"]


async def test_item_deleted_during_analysis_stores_nothing(pool, pki_settings, dev, ctx, jobs_open, monkeypatch):
    item_id = await _video_item(pool, pki_settings, dev["device_id"], TWO_SCENES, 80)
    real_extract = video.extract_jpeg

    async def extract_then_delete(src, fps, index, dst):
        await real_extract(src, fps, index, dst)
        async with pool.connection() as conn:  # e.g. the smoke test's cleanup
            await conn.execute("DELETE FROM item WHERE id = %s", (item_id,))

    monkeypatch.setattr(video, "extract_jpeg", extract_then_delete)
    assert await video.analyze_video(ctx, item_id) == []
    assert await fetch_all(pool, "SELECT id FROM keyframe") == []
    assert _jpegs(pki_settings) == [] and await jobs_by_lock(pool) == {}


async def test_item_deleted_with_its_raw_file_is_a_no_op(pool, pki_settings, dev, ctx, monkeypatch):
    item_id = await _video_item(pool, pki_settings, dev["device_id"], TWO_SCENES, 4)
    real_read = video.read_small_frames

    async def delete_then_read(src, fps):
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM item WHERE id = %s", (item_id,))
        src.unlink()
        return await real_read(src, fps)  # ffmpeg fails: the file is gone

    monkeypatch.setattr(video, "read_small_frames", delete_then_read)
    assert await video.analyze_video(ctx, item_id) == []  # no exception: the job succeeds


async def test_job_wrapper_uses_the_process_context(pool, pki_settings, dev, ctx, jobs_open):
    item_id = await _video_item(pool, pki_settings, dev["device_id"], NIGHT, 10)
    context.set_context(ctx)
    try:
        await video.analyze_video_job(item_id=item_id)
    finally:
        context.set_context(None)
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "done"
    with pytest.raises(RuntimeError, match="pipeline context not set"):
        context.get_context()

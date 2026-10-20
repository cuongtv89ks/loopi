import asyncio
import datetime as dt

import pytest

from helpers import fetch_one, insert_item, make_ctx, paired_device, write_media
from loopi_brain import forget, store
from loopi_brain.pipeline import context, jobs, one_copy, retention, status

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


async def _segment(pool, settings, device_id, *, start=NINE, state="done", status="ready", mp4=b"mp4") -> dict:
    """A video segment with its raw H.264 on disk and, unless ``mp4`` is None, its MP4 view; analysis in ``state``."""
    item = await insert_item(pool, device_id, kind="video", started_at=start, status=status)
    raw = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item,)))["raw_path"]
    write_media(settings, raw, b"h264")
    view = raw.replace("raw/", "view/", 1).replace(".h264", ".mp4")
    if mp4 is not None:
        write_media(settings, view, mp4)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE item SET analysis_state = %s, view_path = %s WHERE id = %s",
            (state, view if status == "ready" else None, item),
        )
    return {"id": item, "raw": raw, "view": view}


def _on_disk(settings, rel: str) -> bool:
    return (settings.media_root / rel).exists()


async def test_an_analysed_segment_with_its_mp4_loses_its_raw_h264(pool, pki_settings, dev):
    seg = await _segment(pool, pki_settings, dev["device_id"])
    assert await one_copy.drop_raw_video(pool, pki_settings) == one_copy.OneCopyResult(dropped=1, kept=0)
    assert not _on_disk(pki_settings, seg["raw"]) and _on_disk(pki_settings, seg["view"])
    row = await fetch_one(pool, "SELECT h264_deleted_at, raw_deleted_at, view_path, status FROM item WHERE id = %s",
                          (seg["id"],))
    assert row["h264_deleted_at"] is not None and row["raw_deleted_at"] is None
    assert (row["view_path"], row["status"]) == (seg["view"], "ready")
    assert await one_copy.drop_raw_video(pool, pki_settings) == one_copy.OneCopyResult()  # nothing left to do


async def test_a_skipped_analysis_counts_as_done(pool, pki_settings, dev):
    seg = await _segment(pool, pki_settings, dev["device_id"], state="skipped")
    assert (await one_copy.drop_raw_video(pool, pki_settings)).dropped == 1
    assert not _on_disk(pki_settings, seg["raw"])


@pytest.mark.parametrize(("state", "status"), [("pending", "ready"), ("done", "stored"), ("done", "error")])
async def test_segments_not_ready_for_one_copy_keep_their_raw_file(pool, pki_settings, dev, state, status):
    seg = await _segment(pool, pki_settings, dev["device_id"], state=state, status=status)
    assert await one_copy.drop_raw_video(pool, pki_settings) == one_copy.OneCopyResult()
    assert _on_disk(pki_settings, seg["raw"])
    assert (await fetch_one(pool, "SELECT h264_deleted_at FROM item WHERE id = %s", (seg["id"],)))[
        "h264_deleted_at"] is None


@pytest.mark.parametrize("mp4", [None, b""])
async def test_a_segment_whose_mp4_is_missing_or_empty_keeps_its_raw_file(pool, pki_settings, dev, mp4):
    seg = await _segment(pool, pki_settings, dev["device_id"], mp4=mp4)
    assert await one_copy.drop_raw_video(pool, pki_settings) == one_copy.OneCopyResult(dropped=0, kept=1)
    assert _on_disk(pki_settings, seg["raw"])
    assert (await fetch_one(pool, "SELECT h264_deleted_at FROM item WHERE id = %s", (seg["id"],)))[
        "h264_deleted_at"] is None


async def test_audio_markers_and_expired_segments_are_not_touched(pool, pki_settings, dev):
    audio = await insert_item(pool, dev["device_id"], kind="audio", started_at=NINE)
    raw_audio = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (audio,)))["raw_path"]
    write_media(pki_settings, raw_audio)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (audio,))
    expired = await _segment(pool, pki_settings, dev["device_id"], start=NINE + dt.timedelta(minutes=1))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE id = %s", (expired["id"],))
    assert await one_copy.drop_raw_video(pool, pki_settings) == one_copy.OneCopyResult()
    assert _on_disk(pki_settings, raw_audio) and _on_disk(pki_settings, expired["raw"])


async def test_a_raw_file_that_is_already_gone_is_recorded_as_dropped(pool, pki_settings, dev):
    seg = await _segment(pool, pki_settings, dev["device_id"])
    (pki_settings.media_root / seg["raw"]).unlink()  # a run that died after the unlink, before the update
    assert (await one_copy.drop_raw_video(pool, pki_settings)).dropped == 1
    assert (await fetch_one(pool, "SELECT h264_deleted_at FROM item WHERE id = %s", (seg["id"],)))[
        "h264_deleted_at"] is not None


async def test_a_raw_file_that_cannot_be_deleted_is_tried_again_later(pool, pki_settings, dev, monkeypatch):
    seg = await _segment(pool, pki_settings, dev["device_id"])
    monkeypatch.setattr(one_copy.media, "remove_files", lambda root, rels: (0, list(rels)))
    assert await one_copy.drop_raw_video(pool, pki_settings) == one_copy.OneCopyResult(dropped=0, kept=1)
    monkeypatch.undo()
    assert (await one_copy.drop_raw_video(pool, pki_settings)).dropped == 1  # the next run
    assert not _on_disk(pki_settings, seg["raw"])


async def test_a_backlog_goes_in_batches_in_one_run(pool, pki_settings, dev, monkeypatch):
    monkeypatch.setattr(one_copy, "BATCH", 2)
    segs = [await _segment(pool, pki_settings, dev["device_id"], start=NINE + dt.timedelta(minutes=n))
            for n in range(5)]
    assert (await one_copy.drop_raw_video(pool, pki_settings)).dropped == 5
    assert not any(_on_disk(pki_settings, s["raw"]) for s in segs)


async def test_a_segment_someone_else_holds_is_left_for_the_next_run(pool, pki_settings, dev):
    held = await _segment(pool, pki_settings, dev["device_id"])
    free = await _segment(pool, pki_settings, dev["device_id"], start=NINE + dt.timedelta(minutes=1))
    async with pool.connection() as conn, conn.transaction():  # e.g. "Quên khoảng thời gian" is deleting it
        await conn.execute("SELECT 1 FROM item WHERE id = %s FOR UPDATE", (held["id"],))
        result = await asyncio.wait_for(one_copy.drop_raw_video(pool, pki_settings), 10)
    assert result.dropped == 1 and _on_disk(pki_settings, held["raw"]) and not _on_disk(pki_settings, free["raw"])


async def test_the_mp4_still_plays_on_the_hours_page(client, pool, pki_settings, dev):
    seg = await _segment(pool, pki_settings, dev["device_id"])
    await one_copy.drop_raw_video(pool, pki_settings)
    text = (await client.get("/hours", params={"day": "2026-10-14"})).text
    assert f'<video controls preload="none" src="/media/{seg["view"]}"></video>' in text


async def test_retention_and_forget_still_delete_a_segment_whose_raw_file_went(pool, pki_settings, dev):
    old = await _segment(pool, pki_settings, dev["device_id"])
    later = await _segment(pool, pki_settings, dev["device_id"], start=NINE + dt.timedelta(days=1))
    await one_copy.drop_raw_video(pool, pki_settings)
    await store.set_retention(pool, store.Retention(enabled=True))
    result = await retention.apply_retention(pool, pki_settings, now=NINE + dt.timedelta(days=8))
    assert result.video == 1 and result.files_failed == 0 and not _on_disk(pki_settings, old["view"])
    await forget.forget_range(pool, pki_settings, NINE + dt.timedelta(days=1), NINE + dt.timedelta(days=1, hours=1))
    assert not _on_disk(pki_settings, later["view"])
    assert await fetch_one(pool, "SELECT id FROM item WHERE id = %s", (later["id"],)) is None
    pending = await fetch_one(pool, "SELECT pending_files FROM forget_log ORDER BY id DESC LIMIT 1")
    assert pending["pending_files"] == []


async def test_the_job_runs_every_ten_minutes_and_has_a_label(pool, pki_settings, dev):
    periodic = {(p.task.name, p.cron) for p in jobs.job_app.periodic_registry.periodic_tasks.values()}
    assert (jobs.DROP_RAW_VIDEO, "*/10 * * * *") in periodic
    assert jobs.DROP_RAW_VIDEO in jobs.AUTO_RERUN_TASKS
    assert status.TASK_LABELS[jobs.DROP_RAW_VIDEO] == "Bỏ bản H.264 thô"
    seg = await _segment(pool, pki_settings, dev["device_id"])
    context.set_context(make_ctx(pool, pki_settings))
    try:
        await one_copy.drop_raw_video_job(timestamp=int(NINE.timestamp()))
    finally:
        context.set_context(None)
    assert not _on_disk(pki_settings, seg["raw"])

import asyncio
import datetime as dt
import logging
from pathlib import PurePosixPath

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb

from helpers import (
    FakeOllama,
    fetch_all,
    fetch_one,
    insert_item,
    insert_keyframe,
    make_ctx,
    paired_device,
    write_media,
)
from loopi_brain import media, store
from loopi_brain.app import create_app
from loopi_brain.pipeline import context, jobs, retention, status

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)
NOW = NINE + dt.timedelta(days=8)  # NINE's recordings are 8 days old
THREE_IN_SEOUL = int(dt.datetime(2026, 10, 14, 18, 0, tzinfo=dt.UTC).timestamp())


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
async def on(pool):
    """The owner turned retention on in Cài đặt (it ships off)."""
    await store.set_retention(pool, store.Retention(enabled=True))


async def _recording(pool, settings, device_id, kind, start, state="done") -> dict:
    """An item with its raw file (and the copy the web plays) on disk, analysis in ``state``."""
    item = await insert_item(pool, device_id, kind=kind, started_at=start)
    raw = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item,)))["raw_path"]
    files = [write_media(settings, raw)]
    if kind == "video":
        view = raw.replace("raw/", "view/", 1).replace(".h264", ".mp4")
        files.append(write_media(settings, view))
    else:
        view = None
        if kind == "audio":
            files.append(write_media(settings, str(media.audio_view_path(PurePosixPath(raw)))))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = %s, view_path = %s WHERE id = %s", (state, view, item))
    return {"id": item, "files": files}


def _exists(settings, rec) -> list[bool]:
    return [(settings.media_root / rel).exists() for rel in rec["files"]]


async def test_old_finished_recordings_lose_their_raw_files_and_keep_their_rows(pool, pki_settings, dev, on):
    device_id = dev["device_id"]
    video = await _recording(pool, pki_settings, device_id, "video", NINE)
    keyframe = await insert_keyframe(pool, pki_settings, video["id"], device_id, NINE)
    audio = await _recording(pool, pki_settings, device_id, "audio", NINE)
    pending = await _recording(pool, pki_settings, device_id, "audio", NINE, state="pending")
    skipped = await _recording(pool, pki_settings, device_id, "video", NINE, state="skipped")
    recent = await _recording(pool, pki_settings, device_id, "video", NOW - dt.timedelta(days=6))
    marker = await _recording(pool, pki_settings, device_id, "marker", NINE)
    result = await retention.apply_retention(pool, pki_settings, now=NOW)
    assert result == retention.RetentionResult(video=2, audio=1, files=6, files_failed=0)
    assert _exists(pki_settings, video) == [False, False] and _exists(pki_settings, audio) == [False, False]
    assert _exists(pki_settings, skipped) == [False, False]
    for kept in (pending, recent, marker):  # analysis not finished, too recent, markers never
        assert all(_exists(pki_settings, kept))
    rows = {r["id"]: r for r in await fetch_all(pool, "SELECT id, raw_deleted_at, view_path FROM item")}
    assert rows[video["id"]]["raw_deleted_at"] is not None and rows[video["id"]]["view_path"] is None
    assert rows[pending["id"]]["raw_deleted_at"] is None and rows[marker["id"]]["raw_deleted_at"] is None
    kf_path = (await fetch_one(pool, "SELECT path FROM keyframe WHERE id = %s", (keyframe,)))["path"]
    assert (pki_settings.media_root / kf_path).exists()  # keyframes are kept for ever
    assert await retention.apply_retention(pool, pki_settings, now=NOW) == retention.RetentionResult()  # idempotent


async def test_the_kept_days_come_from_the_settings(pool, pki_settings, dev):
    video = await _recording(pool, pki_settings, dev["device_id"], "video", NINE)
    audio = await _recording(pool, pki_settings, dev["device_id"], "audio", NINE)
    await store.set_retention(pool, store.Retention(video_days=7, audio_days=30, enabled=True))
    result = await retention.apply_retention(pool, pki_settings, now=NOW)
    assert (result.video, result.audio) == (1, 0)
    assert _exists(pki_settings, video) == [False, False] and all(_exists(pki_settings, audio))


async def test_a_video_whose_mp4_is_not_recorded_yet_loses_it_too(pool, pki_settings, dev, on):
    video = await _recording(pool, pki_settings, dev["device_id"], "video", NINE)
    async with pool.connection() as conn:  # a remux wrote the MP4 and had not said so yet
        await conn.execute("UPDATE item SET view_path = NULL WHERE id = %s", (video["id"],))
    assert (await retention.apply_retention(pool, pki_settings, now=NOW)).files == 2
    assert not any(_exists(pki_settings, video))


async def test_a_file_that_cannot_be_deleted_is_tried_again_next_night(pool, pki_settings, dev, on, caplog):
    outside = pki_settings.media_root.parent / "outside.h264"
    pki_settings.media_root.mkdir(parents=True, exist_ok=True)  # the media root of a running brain exists
    outside.write_bytes(b"keep")
    item = await insert_item(pool, dev["device_id"], kind="video", started_at=NINE, raw_path="../outside.h264")
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (item,))
    with caplog.at_level(logging.WARNING):
        result = await retention.apply_retention(pool, pki_settings, now=NOW)
    assert result == retention.RetentionResult(files_failed=1) and outside.exists()
    assert (await fetch_one(pool, "SELECT raw_deleted_at FROM item"))["raw_deleted_at"] is None
    assert "could not delete media file ../outside.h264" in caplog.text


async def test_the_raw_file_stays_when_a_copy_cannot_be_deleted(pool, pki_settings, dev, on):
    """A recording is never left without its raw file yet counted as kept (a reprocess would then lose its
    transcript for nothing)."""
    outside = pki_settings.media_root.parent / "outside.mp4"
    outside.write_bytes(b"keep")
    video = await _recording(pool, pki_settings, dev["device_id"], "video", NINE)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET view_path = '../outside.mp4' WHERE id = %s", (video["id"],))
    result = await retention.apply_retention(pool, pki_settings, now=NOW)
    assert result == retention.RetentionResult(files=1, files_failed=1)  # the MP4 went, the odd copy was refused
    assert _exists(pki_settings, video) == [True, False] and outside.exists()
    row = await fetch_one(pool, "SELECT raw_deleted_at, view_path FROM item")
    assert row == {"raw_deleted_at": None, "view_path": "../outside.mp4"}


async def test_the_mark_is_committed_before_any_file_is_deleted(pool, pki_settings, dev, on, monkeypatch):
    """A reprocess (FOR UPDATE on the row) must see raw_deleted_at, never NULL for a file being deleted, and must
    not wait for the unlinking: the row is already marked and unlocked when the first file goes."""
    audio = await _recording(pool, pki_settings, dev["device_id"], "audio", NINE)
    real, seen = media.remove_files, []

    def spy(root, rels):  # runs in a worker thread, like the real deletion
        with psycopg.connect(pki_settings.database_url, autocommit=True) as other:
            row = other.execute("SELECT raw_deleted_at FROM item WHERE id = %s FOR UPDATE NOWAIT", (audio["id"],))
            seen.append((row.fetchone()[0] is not None, [(root / rel).exists() for rel in rels]))
        return real(root, rels)

    monkeypatch.setattr(media, "remove_files", spy)
    assert (await retention.apply_retention(pool, pki_settings, now=NOW)).audio == 1
    assert seen and all(marked for marked, _ in seen)
    assert seen[0][1] == [True]  # the playback copy was still there when the mark was already visible


@pytest.mark.parametrize("where", ["item_files", "remove_files"])
async def test_a_row_that_raises_unexpectedly_keeps_its_mark_and_the_finished_rows_theirs(
    pool, pki_settings, dev, on, monkeypatch, caplog, where
):
    """Fail closed: nothing is known about the poison row's files after an unexpected error, so its mark stays (the
    next run's sweep retries it; a row marked expired is never reprocessed) and the row before it, finished, is not
    undone either. It is not retried again within the run."""
    first = await _recording(pool, pki_settings, dev["device_id"], "video", NINE)
    second = await _recording(pool, pki_settings, dev["device_id"], "video", NINE + dt.timedelta(minutes=1))
    real_files, real_remove = media.item_files, media.remove_files

    def poisoned_files(kind, raw_path, view_path):
        if second["id"] in raw_path:
            raise RuntimeError("poison")
        return real_files(kind, raw_path, view_path)

    def poisoned_remove(root, rels):
        if any(second["id"] in rel for rel in rels):
            raise RuntimeError("poison")
        return real_remove(root, rels)

    monkeypatch.setattr(media, where, poisoned_files if where == "item_files" else poisoned_remove)
    with caplog.at_level(logging.ERROR, logger="loopi_brain.pipeline.retention"):
        result = await retention.apply_retention(pool, pki_settings, now=NOW)
    assert result == retention.RetentionResult(video=1, files=2, files_failed=1)  # once, not 40 times
    assert f"item {second['id']}" in caplog.text and "RuntimeError: poison" in caplog.text
    cols = "id, raw_deleted_at, raw_files_gone_at, view_path"
    rows = {r["id"]: r for r in await fetch_all(pool, f"SELECT {cols} FROM item")}
    done = rows[first["id"]]
    assert done["raw_deleted_at"] and done["raw_files_gone_at"] and done["view_path"] is None
    assert not any(_exists(pki_settings, first))
    stuck = rows[second["id"]]
    assert stuck["raw_deleted_at"] and stuck["raw_files_gone_at"] is None  # still marked, still to finish
    assert _exists(pki_settings, second)[0]  # (its raw file happens to be intact here)
    monkeypatch.undo()  # fixed: the next run's sweep finishes it
    assert (await retention.apply_retention(pool, pki_settings, now=NOW)).video == 1
    assert not any(_exists(pki_settings, second))
    assert (await fetch_one(pool, "SELECT raw_files_gone_at FROM item WHERE id = %s", (second["id"],)))[
        "raw_files_gone_at"]


async def test_a_swept_row_whose_raw_file_is_already_gone_stays_marked_when_it_raises(
    pool, pki_settings, dev, on, monkeypatch
):
    """The dead run unlinked the raw file and died before recording it. If the sweep then hits an unexpected error on
    this row, un-marking it would make a row whose raw file is gone look kept: a reprocess of its day would delete its
    transcript for nothing. It stays marked."""
    audio = await _recording(pool, pki_settings, dev["device_id"], "audio", NINE)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE id = %s", (audio["id"],))
    for rel in audio["files"]:  # everything the dead run had removed
        (pki_settings.media_root / rel).unlink()
    real = media.item_files

    def poisoned(kind, raw_path, view_path):
        raise RuntimeError("poison")

    monkeypatch.setattr(media, "item_files", poisoned)
    assert (await retention.apply_retention(pool, pki_settings, now=NOW)) == retention.RetentionResult(files_failed=1)
    row = await fetch_one(pool, "SELECT raw_deleted_at, raw_files_gone_at FROM item")
    assert row["raw_deleted_at"] is not None and row["raw_files_gone_at"] is None
    monkeypatch.setattr(media, "item_files", real)
    assert (await retention.apply_retention(pool, pki_settings, now=NOW)) == retention.RetentionResult(audio=1)
    row = await fetch_one(pool, "SELECT raw_deleted_at, raw_files_gone_at FROM item")
    assert row["raw_deleted_at"] is not None and row["raw_files_gone_at"] is not None


async def test_a_failure_with_the_raw_file_already_gone_keeps_the_mark_too(pool, pki_settings, dev, on):
    """Only a row whose raw file is verifiably still there loses its mark again."""
    outside = pki_settings.media_root.parent / "outside.mp4"
    outside.write_bytes(b"keep")
    video = await _recording(pool, pki_settings, dev["device_id"], "video", NINE)
    async with pool.connection() as conn:  # marked by a dead run that had removed the raw file and the MP4
        await conn.execute("UPDATE item SET raw_deleted_at = now(), view_path = '../outside.mp4' WHERE id = %s",
                           (video["id"],))
    for rel in video["files"]:
        (pki_settings.media_root / rel).unlink()
    result = await retention.apply_retention(pool, pki_settings, now=NOW)
    assert result == retention.RetentionResult(files_failed=1) and outside.exists()
    row = await fetch_one(pool, "SELECT raw_deleted_at, raw_files_gone_at FROM item")
    assert row["raw_deleted_at"] is not None and row["raw_files_gone_at"] is None


class _Crash(BaseException):
    """The worker dies (not an Exception: nothing in the code under test may catch it)."""


async def _marked_then_crashed(pool, settings, monkeypatch, *recordings) -> None:
    """Run retention with the file deletion dying after the mark was committed."""
    real = media.remove_files

    def crash(root, rels):
        raise _Crash

    monkeypatch.setattr(media, "remove_files", crash)
    with pytest.raises(_Crash):
        await retention.apply_retention(pool, settings, now=NOW)
    monkeypatch.setattr(media, "remove_files", real)
    marked = await fetch_all(pool, "SELECT raw_deleted_at, raw_files_gone_at FROM item")
    assert marked and all(r["raw_deleted_at"] and r["raw_files_gone_at"] is None for r in marked)
    assert all(all(_exists(settings, rec)) for rec in recordings)  # what the dead run left on disk


async def test_files_left_by_a_crashed_run_are_removed_at_the_next_tick(pool, pki_settings, dev, on, monkeypatch):
    video = await _recording(pool, pki_settings, dev["device_id"], "video", NINE)
    audio = await _recording(pool, pki_settings, dev["device_id"], "audio", NINE)
    async with pool.connection() as conn:  # the run died in the video batch; its audio batch left the same state
        await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE id = %s", (audio["id"],))
    await _marked_then_crashed(pool, pki_settings, monkeypatch, video, audio)
    # the next tick finishes them, whatever the kept days say now: the owner already saw them as expired
    await store.set_retention(pool, store.Retention(video_days=3000, audio_days=3000, enabled=True))
    result = await retention.apply_retention(pool, pki_settings, now=NOW)
    assert result == retention.RetentionResult(video=1, audio=1, files=4)
    assert not any(_exists(pki_settings, video)) and not any(_exists(pki_settings, audio))
    rows = await fetch_all(pool, "SELECT raw_deleted_at, raw_files_gone_at, view_path FROM item")
    assert len(rows) == 2 and all(r["raw_deleted_at"] and r["raw_files_gone_at"] and r["view_path"] is None for r in rows)
    assert await retention.apply_retention(pool, pki_settings, now=NOW) == retention.RetentionResult()  # finished


async def test_the_tick_finishes_a_recording_the_dead_run_left_half_deleted(pool, pki_settings, dev, on, monkeypatch):
    video = await _recording(pool, pki_settings, dev["device_id"], "video", NINE)
    await _marked_then_crashed(pool, pki_settings, monkeypatch, video)
    (pki_settings.media_root / video["files"][1]).unlink()  # the MP4 had gone before the worker died
    assert (await retention.apply_retention(pool, pki_settings, now=NOW)).files == 1  # the raw file, the MP4 is gone
    assert not any(_exists(pki_settings, video))


async def test_the_unfinished_are_taken_in_bounded_batches_and_never_while_retention_is_off(
    pool, pki_settings, dev, on, monkeypatch
):
    recs = [await _recording(pool, pki_settings, dev["device_id"], "audio", NINE + dt.timedelta(minutes=n))
            for n in range(3)]
    await _marked_then_crashed(pool, pki_settings, monkeypatch, *recs)
    await store.set_retention(pool, store.Retention(enabled=False))
    assert await retention.apply_retention(pool, pki_settings, now=NOW) == retention.RetentionResult()
    assert all(all(_exists(pki_settings, rec)) for rec in recs)
    await store.set_retention(pool, store.Retention(enabled=True))
    monkeypatch.setattr(retention, "BATCH", 2)
    monkeypatch.setattr(retention, "MAX_BATCHES", 1)
    assert (await retention.apply_retention(pool, pki_settings, now=NOW)).audio == 2
    assert (await retention.apply_retention(pool, pki_settings, now=NOW)).audio == 1


async def test_an_unfinished_recording_that_still_cannot_be_deleted_is_tried_once_and_again_next_night(
    pool, pki_settings, dev, on
):
    outside = pki_settings.media_root.parent / "outside.h264"
    pki_settings.media_root.mkdir(parents=True, exist_ok=True)  # the media root of a running brain exists
    outside.write_bytes(b"keep")
    item = await insert_item(pool, dev["device_id"], kind="video", started_at=NINE, raw_path="../outside.h264")
    async with pool.connection() as conn:  # the dead run had marked it
        await conn.execute("UPDATE item SET analysis_state = 'done', raw_deleted_at = now() WHERE id = %s", (item,))
    result = await retention.apply_retention(pool, pki_settings, now=NOW)
    assert result == retention.RetentionResult(files_failed=1) and outside.exists()  # not retried within the run
    assert (await fetch_one(pool, "SELECT raw_deleted_at FROM item"))["raw_deleted_at"] is None


async def test_a_video_not_remuxed_yet_is_not_due_so_no_mp4_is_written_for_an_expired_row(
    pool, pki_settings, dev, on
):
    stored = await _recording(pool, pki_settings, dev["device_id"], "video", NINE)
    failed = await _recording(pool, pki_settings, dev["device_id"], "video", NINE)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET status = 'stored' WHERE id = %s", (stored["id"],))
        await conn.execute("UPDATE item SET status = 'error' WHERE id = %s", (failed["id"],))
    assert (await retention.preview_retention(pool, now=NOW))["video"]["recordings"] == 1
    assert (await retention.apply_retention(pool, pki_settings, now=NOW)).video == 1
    assert all(_exists(pki_settings, stored)) and not any(_exists(pki_settings, failed))  # a failed remux is expired
    async with pool.connection() as conn:  # remuxed since: due like any other
        await conn.execute("UPDATE item SET status = 'ready' WHERE id = %s", (stored["id"],))
    assert (await retention.apply_retention(pool, pki_settings, now=NOW)).video == 1


async def test_a_large_backlog_is_taken_in_bounded_batches(pool, pki_settings, dev, on, monkeypatch):
    monkeypatch.setattr(retention, "BATCH", 2)
    monkeypatch.setattr(retention, "MAX_BATCHES", 2)
    for n in range(5):
        await _recording(pool, pki_settings, dev["device_id"], "video", NINE + dt.timedelta(minutes=n))
    assert (await retention.apply_retention(pool, pki_settings, now=NOW)).video == 4
    assert (await retention.apply_retention(pool, pki_settings, now=NOW)).video == 1


def test_the_hourly_tick_works_only_at_three_local_time(pki_settings):
    assert retention.due(THREE_IN_SEOUL, pki_settings.zone)
    assert not retention.due(THREE_IN_SEOUL - 3600, pki_settings.zone)
    assert not retention.due(THREE_IN_SEOUL + 3600, pki_settings.zone)
    periodic = {(p.task.name, p.cron) for p in jobs.job_app.periodic_registry.periodic_tasks.values()}
    assert (jobs.APPLY_RETENTION, "0 * * * *") in periodic


async def test_the_job_applies_retention_only_on_the_three_oclock_tick(pool, pki_settings, dev, on):
    video = await _recording(pool, pki_settings, dev["device_id"], "video", NINE - dt.timedelta(days=30))
    context.set_context(make_ctx(pool, pki_settings))
    try:
        await retention.apply_retention_job(timestamp=int(dt.datetime(2026, 10, 14, 17, 0, tzinfo=dt.UTC).timestamp()))
        assert all(_exists(pki_settings, video))
        await retention.apply_retention_job(timestamp=THREE_IN_SEOUL)
        assert not any(_exists(pki_settings, video))
    finally:
        context.set_context(None)


async def test_nothing_is_deleted_until_the_owner_turns_retention_on(pool, pki_settings, dev, caplog):
    video = await _recording(pool, pki_settings, dev["device_id"], "video", NINE)
    audio = await _recording(pool, pki_settings, dev["device_id"], "audio", NINE)
    await _recording(pool, pki_settings, dev["device_id"], "audio", NOW - dt.timedelta(days=1))  # too recent
    assert await retention.preview_retention(pool, now=NOW) == {
        "video": {"recordings": 1, "bytes": 1}, "audio": {"recordings": 1, "bytes": 1}}  # what it would delete
    with caplog.at_level(logging.INFO, logger="loopi_brain.pipeline.retention"):
        assert await retention.apply_retention(pool, pki_settings, now=NOW) == retention.RetentionResult()
    assert all(_exists(pki_settings, video)) and all(_exists(pki_settings, audio))
    assert "retention is off" in caplog.text
    context.set_context(make_ctx(pool, pki_settings))  # ... nor does the 03:00 tick, however old the recordings
    try:
        await retention.apply_retention_job(timestamp=THREE_IN_SEOUL)
    finally:
        context.set_context(None)
    assert all(_exists(pki_settings, video)) and all(_exists(pki_settings, audio))


async def test_a_recording_someone_else_holds_is_left_for_the_next_night(pool, pki_settings, dev, on):
    held = await _recording(pool, pki_settings, dev["device_id"], "audio", NINE)
    free = await _recording(pool, pki_settings, dev["device_id"], "audio", NINE + dt.timedelta(minutes=1))
    async with pool.connection() as conn, conn.transaction():  # e.g. "Xử lý lại" is resetting it right now
        await conn.execute("SELECT 1 FROM item WHERE id = %s FOR UPDATE", (held["id"],))
        result = await asyncio.wait_for(retention.apply_retention(pool, pki_settings, now=NOW), 10)
    assert result.audio == 1 and all(_exists(pki_settings, held)) and not any(_exists(pki_settings, free))
    assert (await retention.apply_retention(pool, pki_settings, now=NOW)).audio == 1  # the next night


async def test_files_a_forget_left_behind_are_deleted_at_three_even_with_retention_off(pool, pki_settings, dev):
    left = write_media(pki_settings, "raw/x/2026/10/14/left.opus")
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO forget_log (id, range_start, range_end, counts, pending_files)"
                           " VALUES ('F1', %s, %s, '{}', %s)", (NINE, NINE + dt.timedelta(hours=1), Jsonb([left])))
    context.set_context(make_ctx(pool, pki_settings))
    try:
        await retention.apply_retention_job(timestamp=THREE_IN_SEOUL)
    finally:
        context.set_context(None)
    assert not (pki_settings.media_root / left).exists()
    assert (await fetch_one(pool, "SELECT pending_files FROM forget_log"))["pending_files"] == []


async def test_forgotten_paths_outside_the_media_root_are_dropped_not_deleted(pool, pki_settings, dev, caplog):
    outside = pki_settings.media_root.parent / "precious.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"keep")
    gone = "raw/x/2026/10/14/already-gone.opus"  # a file that is not there counts as deleted
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO forget_log (id, range_start, range_end, counts, pending_files)"
                           " VALUES ('F1', %s, %s, '{}', %s)",
                           (NINE, NINE + dt.timedelta(hours=1), Jsonb(["../precious.txt", "/etc/hostname", gone])))
    with caplog.at_level(logging.WARNING):
        assert await retention.retry_forgotten_files(pool, pki_settings) == 0
    assert outside.exists() and (await fetch_one(pool, "SELECT pending_files FROM forget_log"))["pending_files"] == []
    assert "it is not deleted and not retried" in caplog.text


async def test_a_malformed_forget_row_blocks_neither_the_others_nor_retention(
    pool, pki_settings, dev, on, caplog
):
    left = write_media(pki_settings, "raw/x/2026/10/14/left.opus")
    async with pool.connection() as conn:
        for log_id, files in (("F1", [5, None]), ("F2", [left])):  # F1 cannot be read as paths
            await conn.execute("INSERT INTO forget_log (id, range_start, range_end, counts, pending_files)"
                               " VALUES (%s, %s, %s, '{}', %s)",
                               (log_id, NINE, NINE + dt.timedelta(hours=1), Jsonb(files)))
    old = await _recording(pool, pki_settings, dev["device_id"], "audio", NINE - dt.timedelta(days=30))
    context.set_context(make_ctx(pool, pki_settings))
    try:
        with caplog.at_level(logging.ERROR, logger="loopi_brain.pipeline.retention"):
            await retention.apply_retention_job(timestamp=THREE_IN_SEOUL)
    finally:
        context.set_context(None)
    assert not (pki_settings.media_root / left).exists()  # the good row was handled ...
    assert not any(_exists(pki_settings, old))  # ... and retention still ran
    pending = {r["id"]: r["pending_files"] for r in await fetch_all(pool, "SELECT id, pending_files FROM forget_log")}
    assert pending == {"F1": [5, None], "F2": []}  # the malformed row is left as it was, and logged
    assert "forget row F1" in caplog.text


async def test_forget_rows_with_nothing_left_are_not_visited(pool, pki_settings, dev, monkeypatch):
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO forget_log (id, range_start, range_end, counts) VALUES ('F1', %s, %s, '{}')",
                           (NINE, NINE + dt.timedelta(hours=1)))

    def boom(*args):
        raise AssertionError("no file to delete")

    monkeypatch.setattr(media, "remove_files", boom)
    assert await retention.retry_forgotten_files(pool, pki_settings) == 0


async def test_the_settings_page_shows_what_retention_would_delete(pool, pki_settings, dev, jobs_open):
    await _recording(pool, pki_settings, dev["device_id"], "video", dt.datetime.now(dt.UTC) - dt.timedelta(days=30))
    app = create_app(pki_settings, pool=pool, http_client=FakeOllama().client(), llm_client=FakeOllama().client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        text = (await c.get("/settings")).text
    assert "Nếu chạy bây giờ sẽ xoá: video thô 1 đoạn (0.0 GB)" in text and "audio thô 0 đoạn" in text


async def test_a_failed_retention_run_waits_for_its_schedule(pool, jobs_open):
    async with pool.connection() as conn:
        job_id = await jobs.enqueue(conn, jobs.APPLY_RETENTION, queue="light", queueing_lock="test-retention")
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 1 WHERE id = %s", (job_id,))
    [row] = (await status.pipeline_status(pool))["failed"]
    assert (row["label"], row["retryable"], row["rerun"]) == ("Dọn dữ liệu cũ", False, "tự chạy lại theo lịch")


async def test_turning_retention_on_asks_first_and_says_what_the_next_run_deletes(pool, pki_settings, dev, jobs_open):
    await _recording(pool, pki_settings, dev["device_id"], "video", dt.datetime.now(dt.UTC) - dt.timedelta(days=30))
    await store.set_retention(pool, store.Retention(video_days=9, audio_days=5))
    app = create_app(pki_settings, pool=pool, http_client=FakeOllama().client(), llm_client=FakeOllama().client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        text = (await c.get("/settings")).text
    [form] = [part for part in text.split("<form") if 'hx-post="/settings/retention"' in part]
    form = form.split(">", 1)[0]
    assert ('hx-confirm="Bật dọn dữ liệu cũ? Lúc 03:00 lần chạy tới sẽ xoá vĩnh viễn ít nhất: video thô 1 đoạn '
            '(0.0 GB), audio thô 0 đoạn (0.0 GB) (đếm lúc mở trang, theo số ngày đã lưu: video 9, audio 5; đoạn quá hạn '
            'thêm từ lúc đó cũng bị xoá). Không thể hoàn tác."') in form
    # switched off (or only the days changed with it off), the save needs no question
    assert "hx-on::confirm=" in form and "this.elements.enabled.checked" in form and "issueRequest(true)" in form


async def test_keyframes_and_their_thumbnails_are_still_served_after_retention(pool, pki_settings, dev, on, jobs_open):
    video = await _recording(pool, pki_settings, dev["device_id"], "video", NINE)
    keyframe = await insert_keyframe(pool, pki_settings, video["id"], dev["device_id"], NINE)
    assert (await retention.apply_retention(pool, pki_settings, now=NOW)).video == 1
    app = create_app(pki_settings, pool=pool, http_client=FakeOllama().client(), llm_client=FakeOllama().client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        r = await c.get(f"/thumb/{keyframe}.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"

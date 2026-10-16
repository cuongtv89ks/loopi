import asyncio
import datetime as dt
import logging
import os
from dataclasses import replace
from pathlib import PurePosixPath

import httpx
import psycopg
import pytest

from helpers import (
    FakeOllama,
    fetch_all,
    fetch_one,
    insert_chapter,
    insert_item,
    insert_keyframe,
    insert_moment,
    jobs_by_lock,
    make_ctx,
    new_ulid,
    paired_device,
    write_jpeg,
    write_media,
)
from loopi_brain import forget, media
from loopi_brain.app import create_app
from loopi_brain.days import day_bounds_utc
from loopi_brain.pipeline import chapters as chapter_rules
from loopi_brain.pipeline import recap as recap_rules
from loopi_brain.pipeline.state import forgotten, lock_day

HX = {"HX-Request": "true"}
DAY = dt.date(2026, 10, 14)
NEXT_DAY = DAY + dt.timedelta(days=1)
NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
HALF_PAST = NINE + dt.timedelta(minutes=30)
MIDNIGHT = dt.datetime(2026, 10, 14, 15, 0, tzinfo=dt.UTC)  # 00:00 on Oct 15 in Seoul


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
async def web(pool, pki_settings, jobs_open):
    app = create_app(pki_settings, pool=pool, http_client=FakeOllama().client(), llm_client=FakeOllama().client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


async def _recorded(pool, settings, device_id, kind, start, seconds=60) -> dict:
    """An item with its raw file (and its view copy for video and audio) on disk."""
    item = await insert_item(pool, device_id, kind=kind, started_at=start, seconds=seconds)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item,))
    files = [write_media(settings, row["raw_path"])]
    if kind == "video":
        view = row["raw_path"].replace("raw/", "view/", 1).replace(".h264", ".mp4")
        files.append(write_media(settings, view))
        async with pool.connection() as conn:
            await conn.execute("UPDATE item SET view_path = %s WHERE id = %s", (view, item))
    if kind == "audio":
        files.append(write_media(settings, str(media.audio_view_path(PurePosixPath(row["raw_path"])))))
    return {"id": item, "files": files}


async def _morning(pool, settings, device_id) -> dict:
    video = await _recorded(pool, settings, device_id, "video", NINE)
    kf = await insert_keyframe(pool, settings, video["id"], device_id, NINE + dt.timedelta(seconds=10))
    kf_path = (await fetch_one(pool, "SELECT path FROM keyframe WHERE id = %s", (kf,)))["path"]
    thumb = kf_path.replace("keyframes/", "thumbs/", 1)
    write_jpeg(settings.media_root / thumb, (32, 18))
    visual = await insert_moment(pool, device_id, kind="visual", ts=NINE + dt.timedelta(seconds=10), item_id=video["id"],
                                 text="A person reads")
    audio = await _recorded(pool, settings, device_id, "audio", NINE + dt.timedelta(minutes=5))
    segment = new_ulid()
    async with pool.connection() as conn:
        await conn.execute("UPDATE moment SET keyframe_id = %s WHERE id = %s", (kf, visual))
        await conn.execute(
            "INSERT INTO transcript_segment (id, item_id, device_id, start_ts, end_ts, lang, text, model_version)"
            " VALUES (%s, %s, %s, %s, %s, 'vi', 'mai đi khám răng', 'x')",
            (segment, audio["id"], device_id, NINE + dt.timedelta(minutes=5), NINE + dt.timedelta(minutes=5)))
    speech = await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=5),
                                 item_id=audio["id"], text="mai đi khám răng", lang="vi")
    marker = await _recorded(pool, settings, device_id, "marker", NINE + dt.timedelta(minutes=10), seconds=0)
    await insert_moment(pool, device_id, kind="marker", ts=NINE + dt.timedelta(minutes=10), item_id=marker["id"])
    later = await _recorded(pool, settings, device_id, "audio", NINE + dt.timedelta(hours=1))
    kept_moment = await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(hours=1),
                                      item_id=later["id"], text="chiều nay họp", lang="vi")
    inside = await insert_chapter(pool, DAY, NINE, NINE + dt.timedelta(minutes=10), title="Buổi sáng",
                                  summary="Hẹn đi khám răng.")
    outside = await insert_chapter(pool, DAY, NINE + dt.timedelta(hours=1), NINE + dt.timedelta(hours=1),
                                   title="Họp", summary="Chiều họp.")
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, 'Khám răng', 'x')", (DAY,))
        await conn.execute("INSERT INTO commitment (id, day, text, source_moment_id, model_version)"
                           " VALUES ('C1', %s, 'Đi khám răng', %s, 'x')", (DAY, speech))
        await conn.execute("INSERT INTO commitment (id, day, text, source_moment_id, model_version)"
                           " VALUES ('C2', %s, 'Đi họp', %s, 'x')", (DAY, kept_moment))
    return {"files": video["files"] + [kf_path, thumb] + audio["files"] + marker["files"], "later": later,
            "inside": inside, "outside": outside, "kept_moment": kept_moment}


async def _straddler(pool, settings, device_id) -> dict:
    """An audio recording 23:59:30 -> 00:00:30 over Seoul midnight, with one speech moment before midnight."""
    audio = await _recorded(pool, settings, device_id, "audio", MIDNIGHT - dt.timedelta(seconds=30))
    moment = await insert_moment(pool, device_id, kind="speech", ts=MIDNIGHT - dt.timedelta(seconds=25),
                                 item_id=audio["id"], text="chuyện riêng", lang="vi")
    return {**audio, "moment": moment}


async def _blocked_while_locked(pool, settings, held: dt.date, start, end) -> int:
    """Start a forget of [start, end) while another transaction holds ``held``'s day lock: it must wait for it."""
    async with pool.connection() as conn, conn.transaction():
        await lock_day(conn, held)
        task = asyncio.create_task(forget.forget_range(pool, settings, start, end))
        await asyncio.sleep(0.3)
        assert not task.done()
    return (await task).items


async def test_forgetting_removes_the_rows_files_and_derived_text_of_the_range(pool, pki_settings, dev, jobs_open):
    morning = await _morning(pool, pki_settings, dev["device_id"])
    async with pool.connection() as conn:
        generation = await forgotten(conn, *day_bounds_utc(DAY, pki_settings.zone))
    counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    assert counts == forget.ForgetCounts(items=3, keyframes=1, transcript_segments=1, moments=3, commitments=1,
                                         chapters=1, recaps=1, files=7, files_failed=0)
    async with pool.connection() as conn:  # what a summary or recap writer of that day compares before it stores
        assert await forgotten(conn, *day_bounds_utc(DAY, pki_settings.zone)) == generation + 1
    assert [r["id"] for r in await fetch_all(pool, "SELECT id FROM item")] == [morning["later"]["id"]]
    assert await fetch_all(pool, "SELECT id FROM keyframe") == []
    assert await fetch_all(pool, "SELECT id FROM transcript_segment") == []
    assert [r["id"] for r in await fetch_all(pool, "SELECT id FROM moment")] == [morning["kept_moment"]]
    assert [r["id"] for r in await fetch_all(pool, "SELECT id FROM commitment")] == ["C2"]
    assert await fetch_all(pool, "SELECT day FROM day_recap") == []  # it quoted what is forgotten
    chapters = {r["id"]: r for r in await fetch_all(pool, "SELECT * FROM chapter")}
    assert chapters[morning["inside"]]["title"] is None and chapters[morning["inside"]]["summary"] is None
    assert chapters[morning["outside"]]["title"] == "Họp"
    assert not any(c["closed"] for c in chapters.values())  # the day is rebuilt
    assert all(not (pki_settings.media_root / rel).exists() for rel in morning["files"])
    assert all((pki_settings.media_root / rel).exists() for rel in morning["later"]["files"])
    [audit] = await fetch_all(pool, "SELECT * FROM forget_log")
    assert (audit["range_start"], audit["range_end"]) == (NINE, HALF_PAST)
    assert audit["counts"]["files"] == 7 and "khám" not in str(audit["counts"])  # counts only, no content
    assert audit["pending_files"] == []  # every file went
    assert "chapters:2026-10-14" in await jobs_by_lock(pool)


async def _failed_recap(pool, day: dt.date, error: str) -> int:
    """A dead-lettered recap run of ``day`` with its stored error text."""
    job_id = await recap_rules.defer_recap(pool, day)
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 4 WHERE id = %s", (job_id,))
        await conn.execute("INSERT INTO day_job_error (task, day, error) VALUES ('write_recap', %s, %s)", (day, error))
    return job_id


async def test_a_forget_clears_the_errors_that_may_quote_what_it_forgets_and_the_day_is_recapped_again(
    pool, pki_settings, dev, jobs_open
):
    morning = await _morning(pool, pki_settings, dev["device_id"])
    # An error text as parse_answer wrote it before it stopped quoting the model's answer.
    quote = "AnswerInvalid: Unterminated string; got '{\"main_events\": [\"Hẹn đi khám răng'"
    await _failed_recap(pool, DAY, quote)
    other_day = DAY - dt.timedelta(days=1)
    kept_job = await _failed_recap(pool, other_day, "ProviderError: x")
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_job_error (task, day, error) VALUES ('build_chapters', %s, %s)",
                           (DAY, f"ChapterSummaryFailed: 1 chapter summaries failed; last: {quote}"))
        await conn.execute("UPDATE chapter SET summary_error = %s, summary_error_at = now() WHERE id = %s",
                           (quote, morning["inside"]))
    router = make_ctx(pool, pki_settings).router
    next_morning = dt.datetime(2026, 10, 15, 8, 0, tzinfo=pki_settings.zone)
    assert await recap_rules.plan_recaps(pool, router, pki_settings.zone, next_morning) == []  # the failed run blocks

    await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    inside = await fetch_one(pool, "SELECT summary_error, summary_error_at FROM chapter WHERE id = %s",
                             (morning["inside"],))
    assert inside == {"summary_error": None, "summary_error_at": None}
    assert [(r["task"], r["day"]) for r in await fetch_all(pool, "SELECT task, day FROM day_job_error")] == [
        ("write_recap", other_day)]  # another day keeps its error and its failed run
    failed = await fetch_all(pool, "SELECT id, queueing_lock FROM procrastinate_jobs WHERE status = 'failed'")
    assert failed == [{"id": kept_job, "queueing_lock": f"recap:{other_day}"}]
    assert "khám" not in str(await fetch_all(pool, "SELECT error FROM day_job_error"))
    # Once the rebuilt chapters have settled again, the planner asks for the forgotten day's recap by itself.
    async with pool.connection() as conn:
        await conn.execute("UPDATE chapter SET closed = true")
    assert await recap_rules.plan_recaps(pool, router, pki_settings.zone, next_morning) == [DAY]


async def test_a_recording_that_straddles_the_edge_goes_whole_one_that_only_touches_it_stays(pool, pki_settings, dev):
    device_id = dev["device_id"]
    straddling = await insert_item(pool, device_id, kind="audio", started_at=NINE - dt.timedelta(seconds=30))
    before = await insert_item(pool, device_id, kind="video", started_at=NINE - dt.timedelta(minutes=1))
    marker = await insert_item(pool, device_id, kind="marker", started_at=NINE, seconds=0)
    at_end = await insert_item(pool, device_id, kind="marker", started_at=HALF_PAST, seconds=0)
    counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    assert counts.items == 2
    remaining = {r["id"] for r in await fetch_all(pool, "SELECT id FROM item")}
    assert remaining == {before, at_end} and not remaining & {straddling, marker}


async def test_recordings_of_other_days_and_markers_outside_the_range_keep_their_rows_and_files(pool, pki_settings, dev):
    device_id = dev["device_id"]
    keep = [
        await _recorded(pool, pki_settings, device_id, "audio", NINE - dt.timedelta(days=1)),
        await _recorded(pool, pki_settings, device_id, "video", NINE + dt.timedelta(days=1)),
        await _recorded(pool, pki_settings, device_id, "marker", NINE - dt.timedelta(seconds=1), seconds=0),
        await _recorded(pool, pki_settings, device_id, "marker", HALF_PAST, seconds=0),
    ]
    gone = await _recorded(pool, pki_settings, device_id, "marker", NINE, seconds=0)
    other_days = [DAY - dt.timedelta(days=1), NEXT_DAY]
    chapter_ids = []
    for day in other_days:
        noon = NINE + dt.timedelta(days=(day - DAY).days)
        chapter_ids.append(await insert_chapter(pool, day, noon, noon, title="Giữ", summary="Giữ lại."))
    async with pool.connection() as conn:
        for day in other_days:
            await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, 'giữ', 'x')", (day,))
    counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    assert (counts.items, counts.files, counts.chapters, counts.recaps) == (1, 1, 0, 0)
    assert not any((pki_settings.media_root / rel).exists() for rel in gone["files"])
    assert all((pki_settings.media_root / rel).exists() for kept in keep for rel in kept["files"])
    assert {r["id"] for r in await fetch_all(pool, "SELECT id FROM item")} == {k["id"] for k in keep}
    assert {r["day"] for r in await fetch_all(pool, "SELECT day FROM day_recap")} == set(other_days)
    chapters = await fetch_all(pool, "SELECT id, title, closed FROM chapter")
    assert {c["id"] for c in chapters} == set(chapter_ids)
    assert all(c["title"] == "Giữ" and c["closed"] for c in chapters)  # untouched days are not reopened


async def test_the_preview_counts_and_changes_nothing(pool, pki_settings, dev):
    morning = await _morning(pool, pki_settings, dev["device_id"])
    counts = await forget.preview(pool, NINE, HALF_PAST, pki_settings.zone)
    assert counts == forget.ForgetCounts(items=3, keyframes=1, transcript_segments=1, moments=3, commitments=1,
                                         chapters=1, recaps=1)
    assert len(await fetch_all(pool, "SELECT id FROM item")) == 4
    assert all((pki_settings.media_root / rel).exists() for rel in morning["files"])
    assert await fetch_all(pool, "SELECT id FROM forget_log") == []


async def test_a_range_across_midnight_touches_both_days(pool, pki_settings, dev):
    midnight = dt.datetime(2026, 10, 14, 15, 0, tzinfo=dt.UTC)  # 00:00 on Oct 15 in Seoul
    for day, ts in ((DAY, midnight - dt.timedelta(hours=1)), (DAY + dt.timedelta(days=1), midnight)):
        await insert_chapter(pool, day, ts, ts, title="x", summary="y")
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, 'r', 'x')", (day,))
    assert forget.days_touched(midnight - dt.timedelta(hours=2), midnight + dt.timedelta(hours=1),
                               pki_settings.zone) == [DAY, DAY + dt.timedelta(days=1)]
    assert forget.days_touched(NINE, midnight, pki_settings.zone) == [DAY]  # the end is not included
    counts = await forget.forget_range(pool, pki_settings, midnight - dt.timedelta(hours=2),
                                       midnight + dt.timedelta(hours=1))
    assert (counts.chapters, counts.recaps) == (2, 2)


async def test_ranges_are_checked(pool, pki_settings):
    for start, end in ((HALF_PAST, NINE), (NINE, NINE), (NINE, NINE + dt.timedelta(days=8))):
        with pytest.raises(ValueError):
            await forget.forget_range(pool, pki_settings, start, end)
    with pytest.raises(ValueError):
        forget.check_range(NINE.replace(tzinfo=None), HALF_PAST)
    forget.check_range(NINE, NINE + forget.MAX_RANGE)  # exactly seven days is allowed


async def test_files_outside_the_media_root_are_never_deleted(pool, pki_settings, dev, caplog):
    outside = pki_settings.media_root.parent / "outside.opus"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"keep me")
    item = await insert_item(pool, dev["device_id"], kind="audio", started_at=NINE, raw_path="../outside.opus")
    with caplog.at_level(logging.WARNING, logger="loopi_brain.forget"):
        counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    assert counts.items == 1 and counts.files_failed == 1  # the raw path leaves the media root: refused
    assert outside.read_bytes() == b"keep me"
    assert (await fetch_one(pool, "SELECT pending_files FROM forget_log"))["pending_files"] == []  # never retried
    assert any(item in r.getMessage() and "outside the media root" in r.getMessage() for r in caplog.records)


async def test_a_symlink_inside_the_media_root_cannot_delete_a_file_outside_it(pool, pki_settings, dev, caplog):
    secret = pki_settings.media_root.parent / "secret.opus"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_bytes(b"keep me")
    rel = f"raw/{dev['device_id']}/2026/10/14/link.opus"
    link = pki_settings.media_root / rel
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(secret)
    item = await insert_item(pool, dev["device_id"], kind="audio", started_at=NINE, raw_path=rel)
    with caplog.at_level(logging.WARNING, logger="loopi_brain.forget"):
        counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    assert (counts.items, counts.files, counts.files_failed) == (1, 0, 1)
    assert secret.read_bytes() == b"keep me" and link.is_symlink()
    assert any(item in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


async def test_a_keyframe_that_is_not_under_keyframes_is_reported_and_its_thumbnail_is_not_guessed(
    pool, pki_settings, dev, caplog
):
    item = await insert_item(pool, dev["device_id"], kind="video", started_at=NINE)
    kf = await insert_keyframe(pool, pki_settings, item, dev["device_id"], NINE + dt.timedelta(seconds=5))
    old = (await fetch_one(pool, "SELECT path FROM keyframe WHERE id = %s", (kf,)))["path"]
    odd = old.replace("keyframes/", "frames/", 1)
    (pki_settings.media_root / odd).parent.mkdir(parents=True, exist_ok=True)
    (pki_settings.media_root / old).rename(pki_settings.media_root / odd)
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET path = %s WHERE id = %s", (odd, kf))
    with caplog.at_level(logging.WARNING, logger="loopi_brain.forget"):
        counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    assert counts.keyframes == 1 and not (pki_settings.media_root / odd).exists()  # the file itself still goes
    assert any(kf in r.getMessage() for r in caplog.records)


async def test_a_file_that_cannot_be_deleted_now_stays_listed_for_a_retry(pool, pki_settings, dev):
    if os.geteuid() == 0:
        pytest.skip("root can delete from a read-only folder")
    item = await insert_item(pool, dev["device_id"], kind="marker", started_at=NINE, seconds=0)
    raw = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item,)))["raw_path"]
    write_media(pki_settings, raw)
    folder = (pki_settings.media_root / raw).parent
    folder.chmod(0o555)  # the file cannot be unlinked right now
    try:
        counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    finally:
        folder.chmod(0o755)
    assert (counts.items, counts.files, counts.files_failed) == (1, 0, 1)
    assert (await fetch_one(pool, "SELECT pending_files FROM forget_log"))["pending_files"] == [raw]


async def test_files_go_only_after_the_commit_and_every_path_is_on_the_audit_row_until_then(
    pool, pki_settings, dev, monkeypatch, caplog
):
    morning = await _morning(pool, pki_settings, dev["device_id"])
    real = media.remove_files

    def crash(media_root, rels):
        raise RuntimeError("the disk fell off")

    monkeypatch.setattr(media, "remove_files", crash)
    with caplog.at_level(logging.ERROR, logger="loopi_brain.forget"):
        counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)  # the rows are gone: it still reports
    assert (counts.items, counts.files, counts.files_failed) == (3, 0, 7)
    assert "the disk fell off" in caplog.text
    assert len(await fetch_all(pool, "SELECT id FROM item")) == 1
    assert all((pki_settings.media_root / rel).exists() for rel in morning["files"])  # nothing was deleted
    [audit] = await fetch_all(pool, "SELECT pending_files FROM forget_log")
    assert sorted(audit["pending_files"]) == sorted(morning["files"])  # a later run deletes exactly these
    assert real(pki_settings.media_root, audit["pending_files"]) == (7, [])


async def test_if_the_process_dies_after_the_commit_the_audit_row_already_lists_every_path(
    pool, pki_settings, dev, monkeypatch
):
    class Died(BaseException):  # not an Exception: nothing in forget_range may swallow it
        pass

    morning = await _morning(pool, pki_settings, dev["device_id"])

    def die(media_root, rels):
        raise Died

    monkeypatch.setattr(media, "remove_files", die)
    with pytest.raises(Died):
        await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    assert len(await fetch_all(pool, "SELECT id FROM item")) == 1  # the rows went in one transaction ...
    assert all((pki_settings.media_root / rel).exists() for rel in morning["files"])  # ... no file was touched yet
    [audit] = await fetch_all(pool, "SELECT range_start, range_end, pending_files FROM forget_log")
    assert (audit["range_start"], audit["range_end"]) == (NINE, HALF_PAST)
    assert sorted(audit["pending_files"]) == sorted(morning["files"])  # written by the same transaction


async def test_an_audit_update_that_fails_is_logged_and_leaves_every_path_listed(pool, pki_settings, dev, caplog):
    morning = await _morning(pool, pki_settings, dev["device_id"])
    async with pool.connection() as conn:  # the second write to the audit row (after the files went) is refused
        await conn.execute("CREATE FUNCTION refuse_forget_update() RETURNS trigger LANGUAGE plpgsql AS"
                           " $$ BEGIN RAISE EXCEPTION 'audit row is read only'; END $$")
        await conn.execute("CREATE TRIGGER refuse_forget_update BEFORE UPDATE ON forget_log"
                           " FOR EACH ROW EXECUTE FUNCTION refuse_forget_update()")
    try:
        with caplog.at_level(logging.ERROR, logger="loopi_brain.forget"):
            counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    finally:
        async with pool.connection() as conn:
            await conn.execute("DROP TRIGGER refuse_forget_update ON forget_log")
            await conn.execute("DROP FUNCTION refuse_forget_update()")
    assert (counts.items, counts.files, counts.files_failed) == (3, 7, 0)  # forgotten, and it says so
    assert "could not record the deleted files" in caplog.text
    assert not any((pki_settings.media_root / rel).exists() for rel in morning["files"])
    [audit] = await fetch_all(pool, "SELECT pending_files FROM forget_log")
    assert sorted(audit["pending_files"]) == sorted(morning["files"])  # a later run finds them gone: harmless


async def test_retryable_keeps_the_paths_inside_the_media_root_and_says_what_it_drops(pki_settings, caplog):
    with caplog.at_level(logging.WARNING, logger="loopi_brain.forget"):
        kept = forget.retryable(pki_settings.media_root, ["raw/a.opus", "../x.opus", "/etc/passwd", "view/a.opus"])
    assert kept == ["raw/a.opus", "view/a.opus"]
    assert "../x.opus" in caplog.text and "/etc/passwd" in caplog.text


async def test_a_failing_chapter_request_is_logged_and_the_forget_still_reports(pool, pki_settings, dev, monkeypatch,
                                                                                 caplog):
    await insert_item(pool, dev["device_id"], kind="marker", started_at=NINE, seconds=0)
    await insert_chapter(pool, DAY, NINE + dt.timedelta(hours=1), NINE + dt.timedelta(hours=1), title="Họp", summary="x")

    async def down(*args, **kwargs):
        raise RuntimeError("the job queue is down")

    monkeypatch.setattr(chapter_rules.jobs, "enqueue", down)
    with caplog.at_level(logging.ERROR):
        counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    assert counts.items == 1 and await fetch_all(pool, "SELECT id FROM item") == []
    assert "could not ask for the chapters of 2026-10-14" in caplog.text


async def test_a_forget_waits_while_a_summary_or_recap_of_its_day_is_being_stored(pool, pki_settings, dev):
    await insert_item(pool, dev["device_id"], kind="marker", started_at=NINE, seconds=0)
    async with pool.connection() as conn, conn.transaction():
        await lock_day(conn, DAY)  # a recap of that day is being stored
        task = asyncio.create_task(forget.forget_range(pool, pki_settings, NINE, HALF_PAST))
        await asyncio.sleep(0.3)
        assert not task.done()
    assert (await task).items == 1  # it went on once the recap was stored


@pytest.mark.parametrize("held", [DAY, NEXT_DAY])
async def test_a_forget_across_midnight_waits_for_the_lock_of_either_day(pool, pki_settings, dev, held):
    await insert_item(pool, dev["device_id"], kind="marker", started_at=MIDNIGHT - dt.timedelta(minutes=5), seconds=0)
    items = await _blocked_while_locked(pool, pki_settings, held, MIDNIGHT - dt.timedelta(hours=1),
                                        MIDNIGHT + dt.timedelta(hours=1))
    assert items == 1


async def test_a_recording_from_the_previous_day_makes_the_forget_wait_for_that_days_lock_too(pool, pki_settings, dev):
    await _straddler(pool, pki_settings, dev["device_id"])
    items = await _blocked_while_locked(pool, pki_settings, DAY, MIDNIGHT, MIDNIGHT + dt.timedelta(minutes=10))
    assert items == 1  # the range is on Oct 15, the recording (and its moment) on Oct 14


async def test_the_day_locks_are_taken_in_day_order_before_anything_else(pool, pki_settings, dev, monkeypatch):
    await _straddler(pool, pki_settings, dev["device_id"])
    taken = []

    async def recording(conn, day):
        taken.append(day)
        await lock_day(conn, day)

    monkeypatch.setattr(forget, "lock_day", recording)
    await forget.forget_range(pool, pki_settings, MIDNIGHT, MIDNIGHT + dt.timedelta(minutes=10))
    assert taken == [DAY, NEXT_DAY]  # ascending, one pass: two forgets cannot wait for each other's days


async def test_a_recording_that_turns_up_after_the_first_look_restarts_the_locking_in_order(
    pool, pki_settings, dev, monkeypatch
):
    straddler = await _straddler(pool, pki_settings, dev["device_id"])
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, 'r', 'x')", (DAY,))
    real = forget._targets

    async def stale_first_look(conn, start, end, lock):
        got = await real(conn, start, end, lock)
        if lock:
            return got
        return {**got, "items": [i for i in got["items"] if i["id"] != straddler["id"]], "moments": []}

    taken = []

    async def recording(conn, day):
        taken.append(day)
        await lock_day(conn, day)

    monkeypatch.setattr(forget, "_targets", stale_first_look)
    monkeypatch.setattr(forget, "lock_day", recording)
    counts = await forget.forget_range(pool, pki_settings, MIDNIGHT, MIDNIGHT + dt.timedelta(minutes=10))
    assert taken == [NEXT_DAY, DAY, NEXT_DAY]  # the first pass found out too late; the second takes both, in order
    assert (counts.items, counts.moments, counts.recaps) == (1, 1, 1)
    assert await fetch_all(pool, "SELECT id FROM item") == []
    assert await fetch_all(pool, "SELECT day FROM day_recap") == []


async def test_a_forget_gives_up_without_changing_anything_when_the_days_keep_changing(
    pool, pki_settings, dev, monkeypatch
):
    await insert_item(pool, dev["device_id"], kind="marker", started_at=NINE, seconds=0)
    counter = iter(range(100))
    monkeypatch.setattr(forget, "_affected_days", lambda *args: [DAY + dt.timedelta(days=next(counter))])
    with pytest.raises(RuntimeError, match="nothing was forgotten"):
        await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    assert len(await fetch_all(pool, "SELECT id FROM item")) == 1
    assert await fetch_all(pool, "SELECT id FROM forget_log") == []


async def test_a_recording_across_midnight_takes_the_text_and_the_recap_of_both_days_with_it(pool, pki_settings, dev):
    straddler = await _straddler(pool, pki_settings, dev["device_id"])
    evening = await insert_chapter(pool, DAY, MIDNIGHT - dt.timedelta(minutes=10), MIDNIGHT - dt.timedelta(seconds=10),
                                   title="Buổi tối", summary="Nói chuyện riêng.")  # ends before the range starts
    morning = await insert_chapter(pool, NEXT_DAY, MIDNIGHT + dt.timedelta(minutes=30),
                                   MIDNIGHT + dt.timedelta(minutes=30), title="Sáng sớm", summary="Dậy sớm.")
    async with pool.connection() as conn:
        for day in (DAY, NEXT_DAY):
            await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, 'r', 'x')", (day,))
        generation = await forgotten(conn, *day_bounds_utc(DAY, pki_settings.zone))
    start, end = MIDNIGHT, MIDNIGHT + dt.timedelta(minutes=10)
    counts = await forget.forget_range(pool, pki_settings, start, end)
    assert (counts.items, counts.moments, counts.chapters, counts.recaps) == (1, 1, 1, 2)
    assert await fetch_all(pool, "SELECT day FROM day_recap") == []
    chapters = {r["id"]: r for r in await fetch_all(pool, "SELECT * FROM chapter")}
    assert chapters[evening]["title"] is None and chapters[evening]["summary"] is None  # it quoted the speech
    assert chapters[morning]["title"] == "Sáng sớm"
    assert not any(c["closed"] for c in chapters.values())
    assert not any((pki_settings.media_root / rel).exists() for rel in straddler["files"])
    # the audit row covers the whole recording, so a summary of Oct 14 being written meanwhile is not stored
    [audit] = await fetch_all(pool, "SELECT range_start, range_end FROM forget_log")
    assert (audit["range_start"], audit["range_end"]) == (MIDNIGHT - dt.timedelta(seconds=30), end)
    async with pool.connection() as conn:
        assert await forgotten(conn, *day_bounds_utc(DAY, pki_settings.zone)) == generation + 1
    assert await forget.preview(pool, start, end, pki_settings.zone) == forget.ForgetCounts()  # nothing left


async def test_the_preview_of_a_range_with_a_recording_across_midnight_counts_what_forget_will_remove(
    pool, pki_settings, dev
):
    await _straddler(pool, pki_settings, dev["device_id"])
    await insert_chapter(pool, DAY, MIDNIGHT - dt.timedelta(minutes=10), MIDNIGHT - dt.timedelta(seconds=10),
                         title="Buổi tối", summary="x")
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, 'r', 'x')", (DAY,))
    start, end = MIDNIGHT, MIDNIGHT + dt.timedelta(minutes=10)
    counts = await forget.preview(pool, start, end, pki_settings.zone)
    assert (counts.items, counts.moments, counts.chapters, counts.recaps) == (1, 1, 1, 1)
    forgot = await forget.forget_range(pool, pki_settings, start, end)
    assert counts == replace(forgot, files=0, files_failed=0)


async def test_the_settings_page_previews_then_forgets_after_confirmation(web, pool, pki_settings, dev):
    await _morning(pool, pki_settings, dev["device_id"])
    assert 'hx-post="/settings/forget/preview"' in (await web.get("/settings")).text
    form = {"start": "2026-10-14T09:00", "end": "2026-10-14T09:30"}
    assert (await web.post("/settings/forget/preview", data=form)).status_code == 403
    assert (await web.post("/settings/forget", data={**form, "confirm": "yes"})).status_code == 403
    r = await web.post("/settings/forget/preview", data=form, headers=HX)
    assert r.status_code == 200 and "Từ 14/10/2026 09:00 đến 14/10/2026 09:30 sẽ bị xoá vĩnh viễn" in r.text
    assert "3 đoạn ghi, 1 khung hình, 1 đoạn transcript" in r.text
    assert 'name="confirm" value="yes"' in r.text and 'value="2026-10-14T09:00"' in r.text
    assert len(await fetch_all(pool, "SELECT id FROM item")) == 4
    assert (await web.post("/settings/forget", data=form, headers=HX)).status_code == 422  # not confirmed
    assert len(await fetch_all(pool, "SELECT id FROM item")) == 4
    r = await web.post("/settings/forget", data={**form, "confirm": "yes"}, headers=HX)
    assert r.status_code == 200 and "Đã quên khoảng 14/10/2026 09:00 – 14/10/2026 09:30" in r.text
    assert "7 file" in r.text and len(await fetch_all(pool, "SELECT id FROM item")) == 1
    for bad in ({"start": "hôm qua", "end": "2026-10-14T09:30"}, {"start": "2026-10-14T10:00", "end": "2026-10-14T09:30"},
                {"start": "2026-09-01T00:00", "end": "2026-10-14T00:00"}):
        assert (await web.post("/settings/forget/preview", data=bad, headers=HX)).status_code == 422


async def test_the_form_values_are_local_times_without_an_offset(web, pool, pki_settings, dev):
    await insert_item(pool, dev["device_id"], kind="marker", started_at=NINE, seconds=0)
    for bad in (
        {"start": "2026-10-14T09:00+00:00", "end": "2026-10-14T09:30+00:00"},  # an offset would be silently replaced
        {"start": "2026-10-14T09:00", "end": "2026-10-14T09:30Z"},
    ):
        for url, extra in (("/settings/forget/preview", {}), ("/settings/forget", {"confirm": "yes"})):
            r = await web.post(url, data={**bad, **extra}, headers=HX)
            assert r.status_code == 422, (url, bad, r.status_code)
    assert len(await fetch_all(pool, "SELECT id FROM item")) == 1
    assert await fetch_all(pool, "SELECT id FROM forget_log") == []


@pytest.mark.parametrize(
    "start, end, day",
    [("0001-01-01T00:00", "0001-01-02T00:00", "0001-01-01"), ("9999-12-30T00:00", "9999-12-31T23:59", "9999-12-31")],
)
async def test_a_range_at_the_edge_of_the_calendar_forgets_nothing_and_does_not_fail(
    web, pool, pki_settings, dev, start, end, day, caplog
):
    await insert_item(pool, dev["device_id"], kind="marker", started_at=NINE, seconds=0)
    form = {"start": start, "end": end}
    assert (await web.post("/settings/forget/preview", data=form, headers=HX)).status_code == 200
    with caplog.at_level(logging.ERROR):  # the chapter request for a day without UTC bounds is logged, not raised
        r = await web.post("/settings/forget", data={**form, "confirm": "yes"}, headers=HX)
    assert r.status_code == 200 and "0 đoạn ghi" in r.text
    assert f"could not ask for the chapters of {day}" in caplog.text
    assert len(await fetch_all(pool, "SELECT id FROM item")) == 1


async def test_recordings_of_every_device_in_the_range_go_and_only_those(pool, pki_settings, dev):
    other = await paired_device(pool, pki_settings)
    first, second = dev["device_id"], other["device_id"]
    gone = [
        await _recorded(pool, pki_settings, first, "video", NINE),
        await _recorded(pool, pki_settings, second, "audio", NINE + dt.timedelta(minutes=5)),
        await _recorded(pool, pki_settings, second, "marker", NINE + dt.timedelta(minutes=6), seconds=0),
    ]
    kept = [
        await _recorded(pool, pki_settings, first, "audio", NINE + dt.timedelta(hours=2)),
        await _recorded(pool, pki_settings, second, "video", NINE - dt.timedelta(hours=2)),
    ]
    counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    assert (counts.items, counts.files, counts.files_failed) == (3, 5, 0)
    assert {r["id"] for r in await fetch_all(pool, "SELECT id FROM item")} == {k["id"] for k in kept}
    assert not any((pki_settings.media_root / rel).exists() for g in gone for rel in g["files"])
    assert all((pki_settings.media_root / rel).exists() for k in kept for rel in k["files"])


async def test_a_video_that_was_not_remuxed_yet_loses_an_mp4_that_already_exists(pool, pki_settings, dev):
    item = await insert_item(pool, dev["device_id"], kind="video", started_at=NINE, status="stored")
    raw = (await fetch_one(pool, "SELECT raw_path, view_path FROM item WHERE id = %s", (item,)))
    assert raw["view_path"] is None  # the remux has not recorded its result
    mp4 = raw["raw_path"].replace("raw/", "view/", 1).replace(".h264", ".mp4")
    write_media(pki_settings, raw["raw_path"])
    write_media(pki_settings, mp4)  # ... but its output is already on disk
    counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    assert (counts.items, counts.files, counts.files_failed) == (1, 2, 0)
    assert not (pki_settings.media_root / mp4).exists()


async def test_a_remux_that_ends_after_a_forget_leaves_no_mp4_behind(pool, pki_settings, dev, monkeypatch):
    item = await insert_item(pool, dev["device_id"], kind="video", started_at=NINE, status="stored")
    raw = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item,)))["raw_path"]
    write_media(pki_settings, raw)
    made = []

    async def forgotten_while_ffmpeg_ran(src, dst, fps):
        await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)  # commits and deletes its files first ...
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"mp4")  # ... then the remux finishes and writes its output
        made.append(dst)

    monkeypatch.setattr(media, "remux_to_mp4", forgotten_while_ffmpeg_ran)
    await media.remux_item(pool, pki_settings, item)
    assert len(made) == 1 and not made[0].exists()
    assert await fetch_all(pool, "SELECT id FROM item") == []
    assert [p for p in pki_settings.media_root.rglob("*") if p.is_file()] == []


async def test_a_link_to_a_file_that_stays_is_removed_without_the_file(pool, pki_settings, dev):
    kept = await _recorded(pool, pki_settings, dev["device_id"], "audio", NINE + dt.timedelta(hours=1))
    rel = f"raw/{dev['device_id']}/2026/10/14/link.opus"
    link = pki_settings.media_root / rel
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(pki_settings.media_root / kept["files"][0])  # a recording of the range whose "file" is a link
    await insert_item(pool, dev["device_id"], kind="audio", started_at=NINE, raw_path=rel)
    counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    assert (counts.items, counts.files, counts.files_failed) == (1, 1, 0)
    assert not link.is_symlink()
    assert all((pki_settings.media_root / f).exists() for f in kept["files"])


async def test_a_forget_that_loses_a_deadlock_tries_again(pool, pki_settings, dev, monkeypatch, caplog):
    await insert_item(pool, dev["device_id"], kind="marker", started_at=NINE, seconds=0)
    real = forget._delete_rows
    attempts = []

    async def victim_once(*args):
        attempts.append(args)
        if len(attempts) == 1:
            raise psycopg.errors.DeadlockDetected("deadlock detected")
        return await real(*args)

    monkeypatch.setattr(forget, "_delete_rows", victim_once)
    with caplog.at_level(logging.WARNING, logger="loopi_brain.forget"):
        counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    assert counts.items == 1 and len(attempts) == 2 and await fetch_all(pool, "SELECT id FROM item") == []
    assert "deadlock" in caplog.text
    assert len(await fetch_all(pool, "SELECT id FROM forget_log")) == 1  # one audit row, written once


async def test_a_forget_that_keeps_losing_deadlocks_gives_up_without_changing_anything(pool, pki_settings, dev,
                                                                                       monkeypatch):
    await insert_item(pool, dev["device_id"], kind="marker", started_at=NINE, seconds=0)

    async def always_victim(*args):
        raise psycopg.errors.DeadlockDetected("deadlock detected")

    monkeypatch.setattr(forget, "_delete_rows", always_victim)
    with pytest.raises(RuntimeError, match="nothing was forgotten"):
        await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    assert len(await fetch_all(pool, "SELECT id FROM item")) == 1


async def test_the_preview_says_when_a_recording_on_the_edge_widens_what_is_deleted(web, pool, pki_settings, dev):
    await _straddler(pool, pki_settings, dev["device_id"])  # 23:59:30 on the 14th -> 00:00:30 on the 15th
    form = {"start": "2026-10-15T00:00", "end": "2026-10-15T00:10"}
    r = await web.post("/settings/forget/preview", data=form, headers=HX)
    assert r.status_code == 200
    assert "Thực tế sẽ xoá từ 14/10/2026 23:59:30 đến 15/10/2026 00:10:00" in r.text  # what the confirm will remove
    assert "trước khi bạn bấm xác nhận" in r.text  # recordings uploaded meanwhile go too
    assert 'value="2026-10-15T00:00"' in r.text  # the confirm still carries what was typed


async def test_the_preview_of_a_range_no_recording_crosses_does_not_claim_a_wider_span(web, pool, pki_settings, dev):
    await _morning(pool, pki_settings, dev["device_id"])
    r = await web.post("/settings/forget/preview", data={"start": "2026-10-14T09:00", "end": "2026-10-14T09:30"},
                       headers=HX)
    assert "Thực tế sẽ xoá" not in r.text and "trước khi bạn bấm xác nhận" in r.text

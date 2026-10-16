import datetime as dt
import json
import logging

import pytest

from helpers import (
    FakeOllama,
    fetch_all,
    fetch_one,
    insert_chapter,
    insert_item,
    insert_moment,
    jobs_by_lock,
    make_ctx,
    paired_device,
    run_jobs,
)
from loopi_brain.llm import ASR_CAVEAT
from loopi_brain.pipeline import chapters, jobs, status
from loopi_brain.pipeline.context import set_context

DAY = dt.date(2026, 10, 14)
NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
NEXT_NOON = dt.datetime(2026, 10, 15, 3, 0, tzinfo=dt.UTC)  # the day is over and settled


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def ollama():
    fake = FakeOllama()
    count = iter(range(1, 1000))
    fake.reply = lambda body: {"title": f"Chương {next(count)}", "summary": "Buổi sáng ở nhà. Mọi người làm việc."}
    return fake


@pytest.fixture
def ctx(pool, pki_settings, ollama):
    return make_ctx(pool, pki_settings, ollama=ollama)


async def _run(pool, device_id, start, count, axis, kind="visual", every=60, text=None, lang=None):
    for n in range(count):
        await insert_moment(pool, device_id, kind=kind, ts=start + dt.timedelta(seconds=n * every), axis=axis,
                            text=text or f"{kind} {axis} {n}", lang=lang, seconds=5)


async def _two_scenes(pool, device_id):
    await _run(pool, device_id, NINE, 15, axis=0, text="A person reads at the desk")
    await _run(pool, device_id, NINE + dt.timedelta(minutes=15), 15, axis=1, text="Two people cook in the kitchen")
    await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=20), axis=7,
                        text="Mai mình đi chợ nhé", lang="vi", seconds=3)


def _chats(ollama) -> list[dict]:
    return [body for path, body in ollama.requests if path == "/api/chat"]


async def _chapters(pool) -> list[dict]:
    return await fetch_all(pool, "SELECT * FROM chapter ORDER BY ts_start")


async def test_a_settled_day_becomes_closed_chapters_with_vietnamese_summaries(pool, dev, ctx, ollama):
    await _two_scenes(pool, dev["device_id"])
    result = await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    assert result == chapters.BuildResult(chapters=2, closed=2, summarised=2)
    first, second = await _chapters(pool)
    assert (first["ts_start"], first["last_moment_at"], first["moment_count"]) == (
        NINE, NINE + dt.timedelta(minutes=14), 15)
    assert (second["ts_start"], second["moment_count"]) == (NINE + dt.timedelta(minutes=15), 16)
    assert first["closed"] and second["closed"]
    assert (first["title"], first["summary"]) == ("Chương 1", "Buổi sáng ở nhà. Mọi người làm việc.")
    assert first["model_version"] == "ollama:qwen3:4b" and first["summary_signature"] == first["signature"]
    body = _chats(ollama)[1]
    assert body["model"] == "qwen3:4b" and body["format"] == chapters.SUMMARY_SCHEMA and body["think"] is False
    system, user = body["messages"]
    assert ASR_CAVEAT in system["content"] and "Vietnamese" in system["content"]
    assert user["content"].startswith("Part of the day from 09:15 to 09:29:")
    assert "09:15 picture: Two people cook in the kitchen" in user["content"]
    assert "09:20 speech (vi): Mai mình đi chợ nhé" in user["content"]
    assert user["content"].count("picture:") == 1  # the same caption fifteen times is one line
    assert await fetch_all(pool, "SELECT * FROM day_job_error") == []


async def test_a_second_build_of_a_settled_day_calls_no_model(pool, dev, ctx, ollama):
    await _two_scenes(pool, dev["device_id"])
    await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    before = [(c["id"], c["updated_at"]) for c in await _chapters(pool)]
    result = await chapters.build_chapters(ctx, DAY, now=NEXT_NOON + dt.timedelta(hours=5))
    assert result == chapters.BuildResult(chapters=2, closed=2, summarised=0)
    # Nothing changed, so no row was touched (the recap planner compares updated_at with its recap).
    assert [(c["id"], c["updated_at"]) for c in await _chapters(pool)] == before and len(_chats(ollama)) == 2


async def test_an_unchanged_open_chapter_is_not_touched_by_the_next_build(pool, dev, ctx):
    await _run(pool, dev["device_id"], NINE, 5, axis=0)
    await chapters.build_chapters(ctx, DAY, now=NINE + dt.timedelta(minutes=10))
    [before] = await _chapters(pool)
    await chapters.build_chapters(ctx, DAY, now=NINE + dt.timedelta(minutes=20))
    [after] = await _chapters(pool)
    assert (after["id"], after["updated_at"], after["closed"]) == (before["id"], before["updated_at"], False)


async def test_today_keeps_its_tail_open_and_rewrites_only_what_changed(pool, dev, ctx, ollama):
    device_id = dev["device_id"]
    await _two_scenes(pool, device_id)
    result = await chapters.build_chapters(ctx, DAY, now=NINE + dt.timedelta(minutes=40))
    assert result == chapters.BuildResult(chapters=2, closed=0, summarised=2)  # nothing is 30 minutes old yet
    first_id, second_id = [c["id"] for c in await _chapters(pool)]
    await _run(pool, device_id, NINE + dt.timedelta(minutes=31), 9, axis=1)  # the kitchen scene goes on
    result = await chapters.build_chapters(ctx, DAY, now=NINE + dt.timedelta(minutes=80))
    assert result == chapters.BuildResult(chapters=2, closed=1, summarised=1)
    first, second = await _chapters(pool)
    assert (first["id"], first["closed"], first["title"]) == (first_id, True, "Chương 1")  # untouched
    assert (second["id"], second["closed"], second["moment_count"]) == (second_id, False, 25)
    assert second["title"] == "Chương 3" and second["summary_signature"] == second["signature"]
    assert len(_chats(ollama)) == 3


async def test_analysis_still_to_come_keeps_chapters_open(pool, dev, ctx):
    device_id = dev["device_id"]
    await _two_scenes(pool, device_id)
    item = await insert_item(pool, device_id, kind="audio", started_at=NINE + dt.timedelta(minutes=20))
    result = await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    assert (result.chapters, result.closed) == (2, 1)  # the second chapter waits for that recording
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (item,))
    assert (await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)).closed == 2


async def test_moments_appearing_inside_closed_chapters_reopen_the_day(pool, dev, ctx, ollama, caplog):
    caplog.set_level(logging.INFO, logger="loopi_brain.pipeline.chapters")
    device_id = dev["device_id"]
    await _two_scenes(pool, device_id)
    await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    # A backlog recording of 09:05 is analysed only now.
    await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=5), axis=9,
                        text="Con đi học về rồi", lang="vi")
    result = await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    assert result == chapters.BuildResult(chapters=2, closed=2, summarised=1)
    first, second = await _chapters(pool)
    assert first["moment_count"] == 16 and first["title"] == "Chương 3" and second["title"] == "Chương 2"
    assert "recomputing the day" in caplog.text


async def test_a_model_server_outage_leaves_summaries_for_the_next_build(pool, dev, ctx, ollama):
    await _two_scenes(pool, dev["device_id"])
    ollama.down = True
    result = await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    assert result == chapters.BuildResult(chapters=2, closed=2, summarised=0)  # no error: no retry is spent
    assert [c["title"] for c in await _chapters(pool)] == [None, None]
    ollama.down = False
    assert (await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)).summarised == 2


async def test_bad_answers_are_kept_on_the_chapter_and_fail_the_job(pool, dev, ctx, ollama):
    await _two_scenes(pool, dev["device_id"])
    good = ollama.reply
    ollama.reply = lambda body: "Sorry, I cannot help with that."
    with pytest.raises(chapters.ChapterSummaryFailed, match="2 chapter summaries failed; last: AnswerInvalid"):
        await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    assert all(c["summary_error"].startswith("AnswerInvalid") and c["summary_error_at"] for c in await _chapters(pool))
    error = await fetch_one(pool, "SELECT task, day, error FROM day_job_error")
    assert (error["task"], error["day"]) == ("build_chapters", DAY) and "2 chapter summaries" in error["error"]
    ollama.reply = good
    await chapters.build_chapters(ctx, DAY, attempt=1, now=NEXT_NOON)
    assert [(c["summary_error"], c["summary_error_at"]) for c in await _chapters(pool)] == [(None, None)] * 2
    assert await fetch_all(pool, "SELECT * FROM day_job_error") == []
    assert _chats(ollama)[-1]["options"]["temperature"] == chapters.RETRY_TEMPERATURE


async def test_a_chapter_whose_summary_just_failed_is_left_alone_by_the_next_first_attempt(pool, dev, ctx, ollama):
    await _two_scenes(pool, dev["device_id"])
    good = ollama.reply
    ollama.reply = lambda body: "Sorry, I cannot help with that."
    with pytest.raises(chapters.ChapterSummaryFailed):
        await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    ollama.reply = good
    asked = len(_chats(ollama))
    # The planner asks again next hour: a chapter whose answer is always invalid must not cost a model call each time.
    result = await chapters.build_chapters(ctx, DAY, attempt=0, now=NEXT_NOON)
    assert result == chapters.BuildResult(chapters=2, closed=2, summarised=0) and len(_chats(ollama)) == asked
    assert all(c["summary_error"] and c["title"] is None for c in await _chapters(pool))
    async with pool.connection() as conn:  # six hours later
        await conn.execute("UPDATE chapter SET summary_error_at = now() - interval '7 hours'")
    assert (await chapters.build_chapters(ctx, DAY, attempt=0, now=NEXT_NOON)).summarised == 2
    assert [c["summary_error"] for c in await _chapters(pool)] == [None, None]


async def test_marker_presses_are_part_of_the_chapter_with_their_note(pool, dev, ctx, ollama):
    device_id = dev["device_id"]
    await _run(pool, device_id, NINE, 5, axis=0)
    for minutes, meta in ((2, {"note": "  Con bắt đầu tập đi  "}), (3, {})):
        at = NINE + dt.timedelta(minutes=minutes)
        item = await insert_item(pool, device_id, kind="marker", started_at=at, seconds=0, meta=meta)
        await insert_moment(pool, device_id, kind="marker", ts=at, item_id=item, text="Marked moment")
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done'")
    await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    lines = _chats(ollama)[0]["messages"][1]["content"].split("\n")
    assert "09:02 the user pressed the mark button: Con bắt đầu tập đi" in lines
    assert "09:03 the user pressed the mark button" in lines


async def test_overlong_answers_are_cut_not_rejected(pool, dev, ctx, ollama):
    await _run(pool, dev["device_id"], NINE, 5, axis=0)
    ollama.reply = lambda body: {"title": "Tiêu đề " * 30, "summary": "Câu. " * 300}
    await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    [row] = await _chapters(pool)
    assert len(row["title"]) <= chapters.TITLE_MAX and len(row["summary"]) <= chapters.SUMMARY_MAX
    assert row["summary"].endswith("…")


async def test_the_open_chapter_waits_for_three_moments(pool, dev, ctx, ollama):
    await _run(pool, dev["device_id"], NINE, 2, axis=0)
    result = await chapters.build_chapters(ctx, DAY, now=NINE + dt.timedelta(minutes=5))
    assert result == chapters.BuildResult(chapters=1, closed=0, summarised=0) and _chats(ollama) == []


async def test_a_long_chapter_fits_the_prompt_budget(pool, dev, ctx, ollama):
    await _run(pool, dev["device_id"], NINE, 300, axis=4, kind="speech", every=30, lang="ko",
               text="오늘은 정말 긴 하루였어요 " * 20)
    await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    user = _chats(ollama)[0]["messages"][1]["content"]
    lines = user.split("\n")[1:]
    assert 0 < len(lines) <= chapters.MAX_SPEECH_LINES
    assert sum(len(line) + 1 for line in lines) <= chapters.PROMPT_BUDGET
    assert lines[0].startswith("09:00 speech (ko): 오늘은")


async def test_a_day_whose_moments_are_gone_loses_its_chapters(pool, dev, ctx):
    await _two_scenes(pool, dev["device_id"])
    await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM moment")
    assert await chapters.build_chapters(ctx, DAY, now=NEXT_NOON) == chapters.BuildResult(0, 0, 0)
    assert await _chapters(pool) == []


async def test_the_planner_asks_once_for_each_day_with_data(pool, dev, ctx, pki_settings, jobs_open):
    device_id = dev["device_id"]
    await insert_moment(pool, device_id, ts=NINE)  # today
    await insert_moment(pool, device_id, ts=NINE - dt.timedelta(days=5))  # a backlog day, analysed just now
    now = NINE + dt.timedelta(hours=3)
    zone = pki_settings.zone
    assert await chapters.plan_chapters(pool, ctx.router, zone, now) == [DAY - dt.timedelta(days=5), DAY]
    assert await chapters.plan_chapters(pool, ctx.router, zone, now) == [DAY - dt.timedelta(days=5), DAY]
    locks = await jobs_by_lock(pool)
    for day in ("2026-10-09", "2026-10-14"):
        [job] = locks[f"chapters:{day}"]
        assert (job["task_name"], job["queue_name"], job["lock"], job["args"]) == (
            "build_chapters", "understand", "llm", {"day": day})
        assert job["priority"] == jobs.LLM_PRIORITY  # live captions and transcripts go first
    assert "chapters:2026-10-13" not in locks  # yesterday had no data


async def _old(pool, device_id, days_ago: int) -> str:
    """A moment of ``days_ago`` days before DAY that was analysed (written) that long ago, by the planner's clock."""
    moment = await insert_moment(pool, device_id, ts=NINE - dt.timedelta(days=days_ago))
    async with pool.connection() as conn:
        await conn.execute("UPDATE moment SET created_at = %s WHERE id = %s",
                           (NINE - dt.timedelta(days=days_ago), moment))
    return moment


async def test_the_planner_catches_up_one_unbuilt_day_per_run_newest_first(pool, dev, ctx, pki_settings, jobs_open):
    # Days P2a analysed before P2b existed have moments and no chapter.
    for days_ago in (30, 31):
        await _old(pool, dev["device_id"], days_ago)
    now = NINE + dt.timedelta(days=60)
    zone = pki_settings.zone
    assert await chapters.plan_chapters(pool, ctx.router, zone, now) == [DAY - dt.timedelta(days=30)]
    await insert_chapter(pool, DAY - dt.timedelta(days=30), NINE - dt.timedelta(days=30), NINE - dt.timedelta(days=30),
                         title="x", summary="y")
    assert await chapters.plan_chapters(pool, ctx.router, zone, now) == [DAY - dt.timedelta(days=31)]


async def test_the_planner_comes_back_for_summaries_an_outage_or_a_failure_left_out(pool, dev, ctx, ollama,
                                                                                    pki_settings, jobs_open):
    old_day = DAY - dt.timedelta(days=10)
    await _old(pool, dev["device_id"], 10)
    chapter = await insert_chapter(pool, old_day, NINE - dt.timedelta(days=10), NINE - dt.timedelta(days=10))
    now, zone = NINE + dt.timedelta(days=1), pki_settings.zone
    ollama.models.remove("qwen3:4b")  # the summary model cannot run: no build just to fail
    assert old_day not in await chapters.plan_chapters(pool, ctx.router, zone, now)
    ollama.models.append("qwen3:4b")
    async with pool.connection() as conn:
        await conn.execute("UPDATE chapter SET summary_error = 'x', summary_error_at = %s WHERE id = %s",
                           (now - dt.timedelta(hours=1), chapter))
    assert old_day not in await chapters.plan_chapters(pool, ctx.router, zone, now)  # failed an hour ago: wait
    async with pool.connection() as conn:
        await conn.execute("UPDATE chapter SET summary_error_at = %s", (now - dt.timedelta(hours=7),))
    assert old_day in await chapters.plan_chapters(pool, ctx.router, zone, now)


async def test_a_forget_while_the_model_writes_stores_no_summary(pool, dev, ctx, monkeypatch):
    await _two_scenes(pool, dev["device_id"])
    chat = ctx.router.chat

    async def chat_during_a_forget(task, messages, **kwargs):
        async with pool.connection() as conn:  # "Quên khoảng thời gian" commits while the model is writing
            await conn.execute("INSERT INTO forget_log (id, range_start, range_end, counts) VALUES ('F1', %s, %s, '{}')",
                               (NINE, NINE + dt.timedelta(minutes=5)))
        return await chat(task, messages, **kwargs)

    monkeypatch.setattr(ctx.router, "chat", chat_during_a_forget)
    result = await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    assert result.summarised == 0 and [c["title"] for c in await _chapters(pool)] == [None, None]


async def test_the_worker_builds_the_chapters_a_planner_asked_for(pool, dev, ctx, pki_settings, jobs_open):
    await _two_scenes(pool, dev["device_id"])
    assert await chapters.defer_build(pool, DAY, pki_settings.zone) is not None
    await run_jobs(ctx)
    assert [(c["closed"], c["title"]) for c in await _chapters(pool)] == [(True, "Chương 1"), (True, "Chương 2")]


async def test_a_failed_build_is_listed_with_its_error_and_runs_again_by_itself(pool, dev, pki_settings, jobs_open):
    await insert_moment(pool, dev["device_id"], ts=NINE)
    job_id = await chapters.defer_build(pool, DAY, pki_settings.zone)
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 4 WHERE id = %s", (job_id,))
        await conn.execute("INSERT INTO day_job_error (task, day, error) VALUES ('build_chapters', %s, 'boom')", (DAY,))
    [row] = (await status.pipeline_status(pool))["failed"]
    assert (row["label"], row["target"], row["error"], row["retryable"]) == ("Chia chương", "2026-10-14", "boom", False)
    assert row["rerun"] == "tự chạy lại ở lần lên lịch chia chương (mỗi giờ; chương tóm tắt lỗi: sau 6 giờ)"


def test_the_planner_runs_every_hour():
    periodic = {(p.task.name, p.cron) for p in jobs.job_app.periodic_registry.periodic_tasks.values()}
    assert (jobs.PLAN_CHAPTERS, "5 * * * *") in periodic
    assert json.dumps(chapters.SUMMARY_SCHEMA)  # sent as Ollama's `format`


async def test_a_planner_that_cannot_defer_logs_and_skips_the_day(pool, dev, pki_settings, monkeypatch, caplog):
    await insert_moment(pool, dev["device_id"], ts=NINE)

    async def broken(*args, **kwargs):
        raise RuntimeError("no connection")

    monkeypatch.setattr(jobs, "enqueue", broken)
    assert await chapters.defer_build(pool, DAY, pki_settings.zone) is None
    assert "could not ask for the chapters of 2026-10-14" in caplog.text


async def test_the_planner_job_asks_for_the_days_of_the_current_context(pool, dev, ctx, jobs_open):
    ts = dt.datetime.now(dt.UTC)  # written just now: its day is among the recent ones whatever the date is
    await insert_moment(pool, dev["device_id"], ts=ts)
    set_context(ctx)
    try:
        await chapters.plan_chapters_job(0)
    finally:
        set_context(None)
    locks = await jobs_by_lock(pool)
    assert f"chapters:{ts.astimezone(ctx.settings.zone).date()}" in locks


async def test_a_chapter_build_never_runs_ahead_of_live_work_even_when_deferred_directly(pool, jobs_open):
    await jobs.job_app.tasks[jobs.BUILD_CHAPTERS].defer_async(day="2026-10-14")  # not through defer_build
    job = await fetch_one(pool, "SELECT queue_name, lock, priority FROM procrastinate_jobs")
    assert (job["queue_name"], job["lock"], job["priority"]) == ("understand", "llm", jobs.LLM_PRIORITY)

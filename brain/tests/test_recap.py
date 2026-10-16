import datetime as dt

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
from loopi_brain.days import day_bounds_utc
from loopi_brain.llm import ASR_CAVEAT, AnswerInvalid
from loopi_brain.pipeline import jobs, recap, status
from loopi_brain.pipeline.context import set_context

DAY = dt.date(2026, 10, 14)
NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
ANSWER = {
    "main_events": ["Buổi sáng đọc sách", "Trưa nấu ăn cùng nhau"],
    "memorable": ["Đánh dấu lúc tìm thấy chìa khoá"],
    "commitments": [
        {"text": "Đi khám răng lúc 9 giờ sáng mai", "source": "S2"},
        {"text": "Một việc bịa ra", "source": "S99"},  # not a line of the day: dropped
    ],
}
MARKDOWN = (
    "## Diễn biến chính\n- Buổi sáng đọc sách\n- Trưa nấu ăn cùng nhau\n\n"
    "## Khoảnh khắc đáng nhớ\n- Đánh dấu lúc tìm thấy chìa khoá\n\n"
    "## Việc cần làm hoặc đã hứa\n- Đi khám răng lúc 9 giờ sáng mai (09:20)"
)


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def ollama():
    fake = FakeOllama()
    fake.reply = lambda body: ANSWER
    return fake


@pytest.fixture
def ctx(pool, pki_settings, ollama):
    return make_ctx(pool, pki_settings, ollama=ollama)


async def _day(pool, device_id) -> dict:
    await insert_chapter(pool, DAY, NINE, NINE + dt.timedelta(minutes=30), title="Đọc sách",
                         summary="Một người đọc sách bên bàn.")
    await insert_chapter(pool, DAY, NINE + dt.timedelta(hours=1), NINE + dt.timedelta(hours=2), title="Nấu ăn",
                         summary="Hai người nấu ăn trong bếp.")
    thanks = await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=10),
                                 text="Thank you.", lang="en")
    dentist = await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=20),
                                  text="Mai 9 giờ mình phải đi khám răng nhé", lang="vi")
    item = await insert_item(pool, device_id, kind="marker", started_at=NINE + dt.timedelta(minutes=40), seconds=0,
                             meta={"note": "chìa khoá"})
    marker = await insert_moment(pool, device_id, kind="marker", ts=NINE + dt.timedelta(minutes=40), item_id=item,
                                 text="chìa khoá")
    return {"thanks": thanks, "dentist": dentist, "marker": marker}


def _chats(ollama) -> list[dict]:
    return [body for path, body in ollama.requests if path == "/api/chat"]


async def test_a_day_gets_a_vietnamese_recap_and_its_sourced_commitments(pool, dev, ctx, ollama):
    ids = await _day(pool, dev["device_id"])
    assert await recap.write_recap(ctx, DAY) is True
    row = await fetch_one(pool, "SELECT * FROM day_recap WHERE day = %s", (DAY,))
    assert row["markdown"] == MARKDOWN and row["model_version"] == "ollama:qwen3:8b" and row["stale"] is False
    [commitment] = await fetch_all(pool, "SELECT * FROM commitment")
    assert (commitment["day"], commitment["text"], commitment["source_moment_id"], commitment["status"]) == (
        DAY, "Đi khám răng lúc 9 giờ sáng mai", ids["dentist"], "open")
    [body] = _chats(ollama)
    assert body["model"] == "qwen3:8b" and body["format"] == recap.RECAP_SCHEMA and body["keep_alive"] == "1m"
    system, user = body["messages"]
    assert ASR_CAVEAT in system["content"]
    assert "09:00–09:30 Đọc sách: Một người đọc sách bên bàn." in user["content"]
    assert "[S1] 09:10 (en) Thank you." in user["content"]
    assert "[S2] 09:20 (vi) Mai 9 giờ mình phải đi khám răng nhé" in user["content"]
    assert "[M1] 09:40 the user pressed the mark button: chìa khoá" in user["content"]


async def test_a_day_without_data_gets_no_recap_row(pool, dev, ctx, ollama):
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, 'old', 'x')", (DAY,))
    assert await recap.write_recap(ctx, DAY) is False  # e.g. the whole day was forgotten
    assert await fetch_all(pool, "SELECT * FROM day_recap") == [] and _chats(ollama) == []


async def test_writing_again_keeps_the_users_decisions(pool, dev, ctx, ollama):
    ids = await _day(pool, dev["device_id"])
    await recap.write_recap(ctx, DAY)
    [first] = await fetch_all(pool, "SELECT id FROM commitment")
    await recap.set_commitment_status(pool, first["id"], "done")
    ollama.reply = lambda body: {**ANSWER, "commitments": [
        {"text": "Đi khám răng lúc 9 giờ sáng mai", "source": "S2"},  # already done: not added again
        {"text": "Mua sữa", "source": "[s1]"},
    ]}
    await recap.write_recap(ctx, DAY)
    rows = await fetch_all(pool, "SELECT text, source_moment_id, status FROM commitment ORDER BY status")
    assert rows == [
        {"text": "Đi khám răng lúc 9 giờ sáng mai", "source_moment_id": ids["dentist"], "status": "done"},
        {"text": "Mua sữa", "source_moment_id": ids["thanks"], "status": "open"},
    ]


async def test_a_model_server_outage_writes_nothing_and_spends_no_retry(pool, dev, ctx, ollama):
    await _day(pool, dev["device_id"])
    ollama.down = True
    assert await recap.write_recap(ctx, DAY) is False
    assert await fetch_all(pool, "SELECT * FROM day_recap") == []
    assert await fetch_all(pool, "SELECT * FROM day_job_error") == []


async def test_an_unusable_answer_fails_the_job_and_leaves_its_error(pool, dev, ctx, ollama):
    await _day(pool, dev["device_id"])
    ollama.reply = lambda body: "Here is your recap: it was a nice day."
    with pytest.raises(AnswerInvalid):
        await recap.write_recap(ctx, DAY)
    error = await fetch_one(pool, "SELECT task, error FROM day_job_error")
    assert error["task"] == "write_recap" and error["error"].startswith("AnswerInvalid")
    ollama.reply = lambda body: ANSWER
    assert await recap.write_recap(ctx, DAY, attempt=1) is True
    assert await fetch_all(pool, "SELECT * FROM day_job_error") == []
    assert _chats(ollama)[-1]["options"]["temperature"] == recap.RETRY_TEMPERATURE


async def test_an_answer_with_too_much_is_cut_and_blank_lines_dropped(pool, dev, ctx, ollama):
    await _day(pool, dev["device_id"])
    ollama.reply = lambda body: {"main_events": ["  ", *[f"Việc {n}" for n in range(30)]], "memorable": [],
                                 "commitments": []}
    await recap.write_recap(ctx, DAY)
    markdown = (await fetch_one(pool, "SELECT markdown FROM day_recap"))["markdown"]
    assert markdown.count("- Việc ") == recap.MAX_EVENTS and "- Việc 0\n" in markdown
    assert "## Khoảnh khắc đáng nhớ\nKhông có." in markdown and markdown.endswith("## Việc cần làm hoặc đã hứa\nKhông có.")


async def test_a_long_day_fits_the_prompt_budget(pool, dev, ctx, ollama):
    device_id = dev["device_id"]
    await insert_chapter(pool, DAY, NINE, NINE + dt.timedelta(hours=10), title="Cả ngày", summary="Một ngày dài.")
    for n in range(500):
        await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=n), lang="vi",
                            text=" ".join(["lời"] * (n % 40 + 1)))
    await recap.write_recap(ctx, DAY)
    user = _chats(ollama)[0]["messages"][1]["content"]
    speech = [line for line in user.split("\n") if line.startswith("[S")]
    assert 0 < len(speech) <= recap.MAX_SPEECH_LINES
    assert sum(len(line) + 1 for line in user.split("\n") if line.startswith(("[S", "[M", "09:"))) <= recap.PROMPT_BUDGET
    assert all(line.count("lời") >= 30 for line in speech)  # the longest lines, not the first ones


async def test_the_planner_writes_today_at_23_30_and_earlier_days_once_settled(pool, dev, ctx, pki_settings, jobs_open):
    zone = pki_settings.zone
    await insert_chapter(pool, DAY, NINE, NINE + dt.timedelta(hours=1), closed=False)  # today, still open
    await insert_chapter(pool, DAY - dt.timedelta(days=1), NINE - dt.timedelta(days=1),
                         NINE - dt.timedelta(days=1, hours=-1))  # yesterday: settled
    await insert_chapter(pool, DAY - dt.timedelta(days=3), NINE - dt.timedelta(days=3),
                         NINE - dt.timedelta(days=3, hours=-1), closed=False)  # a backlog day still analysed
    evening = dt.datetime(2026, 10, 14, 22, 0, tzinfo=zone)
    assert await recap.plan_recaps(pool, ctx.router, zone, evening) == [DAY - dt.timedelta(days=1)]
    late = dt.datetime(2026, 10, 14, 23, 31, tzinfo=zone)
    assert await recap.plan_recaps(pool, ctx.router, zone, late) == [DAY]  # yesterday's job already waits
    locks = await jobs_by_lock(pool)
    assert [j["args"] for j in locks["recap:2026-10-13"]] == [{"day": "2026-10-13"}]
    [job] = locks["recap:2026-10-14"]
    assert (job["task_name"], job["queue_name"], job["lock"], job["priority"]) == (
        "write_recap", "understand", "llm", jobs.LLM_PRIORITY)
    assert "recap:2026-10-11" not in locks


async def test_today_is_written_again_at_23_30_after_an_earlier_recap(pool, dev, ctx, pki_settings, jobs_open):
    zone = pki_settings.zone
    await insert_chapter(pool, DAY, NINE, NINE + dt.timedelta(hours=1), closed=False)
    async with pool.connection() as conn:  # "Tạo lại recap" pressed at noon
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version, generated_at) VALUES (%s, 'x', 'x', %s)",
                           (DAY, dt.datetime(2026, 10, 14, 12, 0, tzinfo=zone)))
    assert await recap.plan_recaps(pool, ctx.router, zone, dt.datetime(2026, 10, 14, 23, 0, tzinfo=zone)) == []
    assert await recap.plan_recaps(pool, ctx.router, zone, dt.datetime(2026, 10, 14, 23, 31, tzinfo=zone)) == [DAY]
    async with pool.connection() as conn:  # written at 23:35: done for today
        await conn.execute("DELETE FROM procrastinate_jobs")
        await conn.execute("UPDATE day_recap SET generated_at = %s", (dt.datetime(2026, 10, 14, 23, 35, tzinfo=zone),))
    assert await recap.plan_recaps(pool, ctx.router, zone, dt.datetime(2026, 10, 14, 23, 50, tzinfo=zone)) == []


async def test_an_earlier_day_is_written_again_once_after_its_chapters_change(pool, dev, ctx, pki_settings, jobs_open):
    zone = pki_settings.zone
    past = DAY - dt.timedelta(days=2)
    await insert_chapter(pool, past, NINE - dt.timedelta(days=2), NINE - dt.timedelta(days=2, hours=-1))
    written = dt.datetime(2026, 10, 12, 23, 30, tzinfo=zone)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version, generated_at) VALUES (%s, 'x', 'x', %s)",
                           (past, written))
        await conn.execute("UPDATE chapter SET updated_at = %s", (written - dt.timedelta(hours=1),))
    now = dt.datetime(2026, 10, 14, 10, 0, tzinfo=zone)
    assert await recap.plan_recaps(pool, ctx.router, zone, now) == []  # the recap is newer than its chapters
    async with pool.connection() as conn:  # a summary arrived after the recap (an outage, a late upload)
        await conn.execute("UPDATE chapter SET updated_at = %s", (written + dt.timedelta(hours=3),))
    assert await recap.plan_recaps(pool, ctx.router, zone, now) == [past]
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM procrastinate_jobs")
        await conn.execute("UPDATE day_recap SET generated_at = %s", (written + dt.timedelta(hours=4),))
    assert await recap.plan_recaps(pool, ctx.router, zone, now) == []  # written again once
    async with pool.connection() as conn:  # reopened: wait until it settles again
        await conn.execute("UPDATE chapter SET closed = false, updated_at = %s", (written + dt.timedelta(hours=5),))
    assert await recap.plan_recaps(pool, ctx.router, zone, now) == []


async def test_a_forget_while_the_recap_is_written_stores_nothing(pool, dev, ctx, monkeypatch):
    await _day(pool, dev["device_id"])
    chat = ctx.router.chat

    async def chat_during_a_forget(task, messages, **kwargs):
        async with pool.connection() as conn:  # "Quên khoảng thời gian" commits while the model is writing
            await conn.execute("INSERT INTO forget_log (id, range_start, range_end, counts) VALUES ('F1', %s, %s, '{}')",
                               (NINE + dt.timedelta(minutes=15), NINE + dt.timedelta(minutes=25)))
        return await chat(task, messages, **kwargs)

    monkeypatch.setattr(ctx.router, "chat", chat_during_a_forget)
    assert await recap.write_recap(ctx, DAY) is False  # its text may quote the forgotten line
    assert await fetch_all(pool, "SELECT * FROM day_recap") == [] and await fetch_all(pool, "SELECT * FROM commitment") == []


async def test_the_planner_skips_fresh_and_failed_recaps_and_waits_for_the_model(pool, dev, ctx, ollama, pki_settings,
                                                                                 jobs_open):
    zone = pki_settings.zone
    late = dt.datetime(2026, 10, 14, 23, 45, tzinfo=zone)
    await insert_chapter(pool, DAY, NINE, NINE + dt.timedelta(hours=1))
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, 'x', 'x')", (DAY,))
    assert await recap.plan_recaps(pool, ctx.router, zone, late) == []
    async with pool.connection() as conn:
        await conn.execute("UPDATE day_recap SET stale = true")  # reprocessing changed the day
    ollama.models.remove("qwen3:8b")
    assert await recap.plan_recaps(pool, ctx.router, zone, late) == []  # no job just to fail
    ollama.models.append("qwen3:8b")
    assert "recap:2026-10-14" not in await jobs_by_lock(pool)
    assert await recap.plan_recaps(pool, ctx.router, zone, late) == [DAY]
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed' WHERE queueing_lock = 'recap:2026-10-14'")
    assert await recap.plan_recaps(pool, ctx.router, zone, late) == []  # on the dead-letter list instead


async def test_the_planner_asks_again_once_a_failed_recap_job_is_deleted(pool, dev, ctx, pki_settings, jobs_open):
    zone = pki_settings.zone
    late = dt.datetime(2026, 10, 14, 23, 45, tzinfo=zone)
    await insert_chapter(pool, DAY, NINE, NINE + dt.timedelta(hours=1))
    assert await recap.plan_recaps(pool, ctx.router, zone, late) == [DAY]
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed' WHERE queueing_lock = 'recap:2026-10-14'")
    assert await recap.plan_recaps(pool, ctx.router, zone, late) == []
    async with pool.connection() as conn:  # what "Tạo lại recap" does first: the job's events go with it (cascade)
        await conn.execute("DELETE FROM procrastinate_jobs WHERE queueing_lock = %s AND status = 'failed'",
                           ("recap:2026-10-14",))
    assert await fetch_all(pool, "SELECT * FROM procrastinate_events") == []
    assert await recap.plan_recaps(pool, ctx.router, zone, late) == [DAY]


async def test_the_worker_writes_the_recap_and_a_failure_can_be_retried(pool, dev, ctx, jobs_open):
    await _day(pool, dev["device_id"])
    job_id = await recap.defer_recap(pool, DAY)
    assert await recap.defer_recap(pool, DAY) == job_id  # one waiting request per day
    await run_jobs(ctx)
    assert (await fetch_one(pool, "SELECT markdown FROM day_recap"))["markdown"] == MARKDOWN
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 4 WHERE id = %s", (job_id,))
        await conn.execute("INSERT INTO day_job_error (task, day, error) VALUES ('write_recap', %s, 'boom')", (DAY,))
    [row] = (await status.pipeline_status(pool))["failed"]
    assert (row["label"], row["target"], row["error"], row["retryable"]) == ("Viết recap", "2026-10-14", "boom", True)


async def test_commitment_status_changes(pool, dev, ctx):
    await _day(pool, dev["device_id"])
    await recap.write_recap(ctx, DAY)
    [row] = await fetch_all(pool, "SELECT id FROM commitment")
    assert (await recap.set_commitment_status(pool, row["id"], "dismissed"))["status"] == "dismissed"
    assert await recap.set_commitment_status(pool, "01NOSUCHCOMMITMENT00000000", "done") is None
    with pytest.raises(ValueError):
        await recap.set_commitment_status(pool, row["id"], "later")


async def test_the_planner_job_asks_for_the_days_of_the_current_context(pool, dev, ctx, jobs_open):
    past = dt.datetime.now(dt.UTC).astimezone(ctx.settings.zone).date() - dt.timedelta(days=2)  # settled whatever today is
    first = day_bounds_utc(past, ctx.settings.zone)[0] + dt.timedelta(hours=9)
    await insert_chapter(pool, past, first, first + dt.timedelta(hours=1))
    set_context(ctx)
    try:
        await recap.plan_recaps_job(0)
    finally:
        set_context(None)
    assert f"recap:{past}" in await jobs_by_lock(pool)


async def test_a_recap_job_never_runs_ahead_of_live_work_even_when_deferred_directly(pool, jobs_open):
    await jobs.job_app.tasks[jobs.WRITE_RECAP].defer_async(day="2026-10-14")  # not through defer_recap
    job = await fetch_one(pool, "SELECT queue_name, lock, priority FROM procrastinate_jobs")
    assert (job["queue_name"], job["lock"], job["priority"]) == ("understand", "llm", jobs.LLM_PRIORITY)


def test_the_recap_planner_runs_every_quarter_hour():
    periodic = {(p.task.name, p.cron) for p in jobs.job_app.periodic_registry.periodic_tasks.values()}
    assert (jobs.PLAN_RECAPS, "*/15 * * * *") in periodic
    assert jobs.WRITE_RECAP not in jobs.AUTO_RERUN_TASKS and jobs.PLAN_RECAPS in jobs.AUTO_RERUN_TASKS

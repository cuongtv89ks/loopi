import asyncio
import datetime as dt
import logging
import os
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from helpers import (FakeOllama, fetch_all, fetch_one, insert_item, insert_keyframe, insert_moment, jobs_by_lock,
                     make_ctx, make_mp4, make_opus, paired_device, run_jobs)
from loopi_brain import forget, media, store
from loopi_brain.pipeline import jobs, retention, vlogs
from loopi_brain.pipeline.context import set_context
from loopi_brain.vlog import render

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
DAY = dt.date(2026, 10, 14)
LATE = dt.datetime(2026, 10, 14, 14, 45, tzinfo=dt.UTC)  # 23:45 in Seoul


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


async def _recap(pool, day, at=LATE):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO day_recap (day, markdown, model_version, generated_at) VALUES (%s, '# x', 'm', %s)", (day, at)
        )


async def _fail_job(pool, job_id):
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 4 WHERE id = %s", (job_id,))


async def _day(pool, settings, device_id, *, talk=True):
    """Three 6-s recordings at 09:00, 09:10 and 09:20 (MP4 views on disk, a keyframe and a visual moment each), the
    09:10 picture notable, and with ``talk`` a dense conversation at 09:20 whose audio item has its raw Opus file."""
    for n in range(3):
        start = NINE + dt.timedelta(minutes=10 * n)
        item = await insert_item(pool, device_id, kind="video", started_at=start, seconds=6)
        view = f"view/{device_id}/2026/10/14/{item}.mp4"
        make_mp4(settings.media_root / view)
        async with pool.connection() as conn:
            await conn.execute("UPDATE item SET analysis_state = 'done', view_path = %s WHERE id = %s", (view, item))
        frame = await insert_keyframe(pool, settings, item, device_id, start)
        await insert_moment(pool, device_id, kind="visual", ts=start, text="phòng")
        if n == 1:
            async with pool.connection() as conn:
                await conn.execute("UPDATE keyframe SET caption = %s WHERE id = %s",
                                   (Jsonb({"activity": "nấu ăn", "notable": True}), frame))
    if talk:
        audio = await insert_item(pool, device_id, kind="audio", started_at=NINE + dt.timedelta(minutes=20), seconds=120)
        raw = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (audio,)))["raw_path"]
        make_opus(settings.media_root / raw, "sine=frequency=440:sample_rate=48000", 120)
        for k in range(6):
            await insert_moment(pool, device_id, kind="speech", item_id=audio,
                                ts=NINE + dt.timedelta(minutes=20, seconds=20 * k), text="nói dài" if k == 1 else "ừ")
        return audio
    return None


def _tmp_empty(settings) -> bool:
    tmp = settings.media_root / vlogs.TMP_DIR
    return not tmp.exists() or not any(tmp.iterdir())


def test_plan_seconds_keep_a_vlog_between_20_and_90_seconds():
    assert vlogs.plan_seconds(240, 10) == (60.0, 3.0)
    assert vlogs.plan_seconds(400, 10) == (60.0, 3.0)
    assert vlogs.plan_seconds(80, 6) == (20.0, 3.0)
    assert vlogs.plan_seconds(4, 2) == (10.0, 5.0)
    assert vlogs.plan_seconds(4, 0) == (20.0, 3.0)
    assert vlogs.plan_seconds(0, 3) == (0.0, 5.0)


async def test_the_planner_asks_for_a_vlog_once_a_day_has_its_recap(pool, pki_settings, jobs_open):
    await _recap(pool, DAY)
    now = LATE + dt.timedelta(minutes=5)
    assert await vlogs.plan_vlogs(pool, pki_settings.zone, now) == [DAY]
    [job] = await fetch_all(
        pool, "SELECT task_name, queueing_lock, lock, priority FROM procrastinate_jobs WHERE task_name = %s",
        (jobs.WRITE_VLOG_SCRIPT,),
    )
    assert job == {"task_name": "write_vlog_script", "queueing_lock": "vlog:2026-10-14", "lock": "llm", "priority": -10}
    assert await vlogs.plan_vlogs(pool, pki_settings.zone, now) == []  # its job is waiting


async def test_earlier_days_catch_up_one_per_run_newest_first_and_today_never_waits(pool, pki_settings, jobs_open):
    for d in (12, 13, 14):
        await _recap(pool, dt.date(2026, 10, d), at=NINE - dt.timedelta(days=14 - d))
    assert await vlogs.plan_vlogs(pool, pki_settings.zone, LATE) == [DAY, dt.date(2026, 10, 13)]
    assert await vlogs.plan_vlogs(pool, pki_settings.zone, LATE) == [dt.date(2026, 10, 12)]


async def test_a_vlog_is_made_again_once_its_recap_is_rewritten(pool, pki_settings, jobs_open):
    await _recap(pool, DAY, at=NINE)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO vlog (day, state, scripted_at) VALUES (%s, 'empty', %s)",
                           (DAY, NINE + dt.timedelta(hours=1)))
    assert await vlogs.plan_vlogs(pool, pki_settings.zone, LATE) == []  # scripted after its recap
    async with pool.connection() as conn:
        await conn.execute("UPDATE day_recap SET generated_at = %s", (NINE + dt.timedelta(hours=2),))
    assert await vlogs.plan_vlogs(pool, pki_settings.zone, LATE) == [DAY]


async def test_a_failed_vlog_is_not_asked_for_again_by_the_planner(pool, pki_settings, jobs_open):
    await _recap(pool, DAY)
    await _fail_job(pool, await vlogs.defer_script(pool, DAY))
    assert await vlogs.plan_vlogs(pool, pki_settings.zone, LATE) == []


async def test_a_script_whose_render_was_lost_gets_its_render(pool, pki_settings, jobs_open):
    await _recap(pool, DAY, at=NINE)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO vlog (day, state, script) VALUES (%s, 'scripted', '{}')", (DAY,))
    assert await vlogs.plan_vlogs(pool, pki_settings.zone, LATE) == [DAY]
    [job] = await fetch_all(pool, "SELECT task_name, lock FROM procrastinate_jobs WHERE queueing_lock = 'vlog:2026-10-14'")
    assert job == {"task_name": "render_vlog", "lock": "vlog"}


async def test_a_day_without_activity_is_stored_empty_and_nothing_renders(pool, pki_settings, jobs_open):
    assert await vlogs.write_vlog_script(make_ctx(pool, pki_settings), DAY) == "empty"
    row = await fetch_one(pool, "SELECT state, script, path FROM vlog WHERE day = %s", (DAY,))
    assert row == {"state": "empty", "script": None, "path": None}
    assert await fetch_all(pool, "SELECT id FROM procrastinate_jobs WHERE task_name = 'render_vlog'") == []


async def test_a_day_is_scripted_then_rendered_into_its_vlog(pool, pki_settings, dev, jobs_open):
    audio = await _day(pool, pki_settings, dev["device_id"])
    ollama = FakeOllama()
    ollama.reply = lambda body: {"title": "Một ngày ở nhà", "highlights": [
        {"ref": "H1", "line": "Nấu ăn", "audio": False}, {"ref": "H2", "line": "Trò chuyện", "audio": True}]}
    ctx = make_ctx(pool, pki_settings, ollama=ollama)
    assert await vlogs.write_vlog_script(ctx, DAY) == "scripted"
    row = await fetch_one(pool, "SELECT state, script, model_version FROM vlog WHERE day = %s", (DAY,))
    assert (row["state"], row["model_version"]) == ("scripted", "ollama:qwen3:8b")
    assert [h["line"] for h in row["script"]["highlights"]] == ["Nấu ăn", "Trò chuyện"]
    [job] = await fetch_all(
        pool, "SELECT task_name, lock, queueing_lock FROM procrastinate_jobs WHERE task_name = 'render_vlog'"
    )
    assert job == {"task_name": "render_vlog", "lock": "vlog", "queueing_lock": "vlog:2026-10-14"}

    assert await vlogs.render_vlog(ctx, DAY) is True
    row = await fetch_one(pool, "SELECT state, path, source, duration_s, error FROM vlog WHERE day = %s", (DAY,))
    assert (row["state"], row["path"], row["source"], row["error"]) == (
        "rendered", "vlog/2026/10/14/2026-10-14.mp4", "video", None)
    mp4 = pki_settings.media_root / row["path"]
    assert sorted(t.handler for t in render.probe_mp4(mp4)) == ["sbtl", "soun", "vide"]
    assert row["duration_s"] == pytest.approx(render.video_track(mp4).seconds)
    vtt = mp4.with_suffix(".vtt").read_text(encoding="utf-8")
    assert vtt.count("-->") == 3  # the title and two pictures
    assert "Trò chuyện\n“nói dài”" in vtt  # a line the model wrote differs from the sentence: both show
    raw = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (audio,)))["raw_path"]
    assert (pki_settings.media_root / media.audio_view_path(PurePosixPath(raw))).is_file()  # the sound's copy (D19)
    assert _tmp_empty(pki_settings)


async def test_a_day_whose_raw_video_is_gone_renders_a_slideshow(pool, pki_settings, dev, jobs_open):
    await _day(pool, pki_settings, dev["device_id"], talk=False)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE kind = 'video'")
    ctx = make_ctx(pool, pki_settings)  # FakeOllama's default answer is no script: the rule script is used
    assert await vlogs.write_vlog_script(ctx, DAY) == "scripted"
    assert await vlogs.render_vlog(ctx, DAY) is True
    row = await fetch_one(pool, "SELECT source, model_version FROM vlog WHERE day = %s", (DAY,))
    assert row == {"source": "keyframes", "model_version": "rule"}


async def test_a_day_whose_mp4_files_are_missing_or_broken_still_renders_a_slideshow(
        pool, pki_settings, dev, jobs_open, monkeypatch):
    """The keyframes always go to the renderer, which uses them only when no segment is usable (ruling 1)."""
    await _day(pool, pki_settings, dev["device_id"], talk=False)
    ctx = make_ctx(pool, pki_settings)
    assert await vlogs.write_vlog_script(ctx, DAY) == "scripted"
    views = sorted((pki_settings.media_root / "view").rglob("*.mp4"))
    assert len(views) == 3
    views[0].unlink()  # gone after the script was written ...
    views[1].write_bytes(b"not an mp4")  # ... or unreadable ...
    views[2].unlink()
    real, modes = render.render, []

    async def spy(**kwargs):
        result = await real(**kwargs)
        modes.append(result.mode)
        return result

    monkeypatch.setattr(vlogs.render, "render", spy)
    assert await vlogs.render_vlog(ctx, DAY) is True
    assert modes == ["slideshow"]
    row = await fetch_one(pool, "SELECT state, path, source, duration_s FROM vlog WHERE day = %s", (DAY,))
    assert (row["state"], row["source"]) == ("rendered", "keyframes")
    # the day's three keyframes (0.25 s each), and the notable picture's 5 s that fills the 20-s minimum
    assert row["duration_s"] == pytest.approx(0.75 + 5.0)
    assert _tmp_empty(pki_settings)


async def test_a_rule_script_talk_highlight_shows_its_sentence_once(pool, pki_settings, dev, jobs_open):
    """The rule script's line *is* the conversation's sentence: the subtitle must not repeat it (ruling 2)."""
    await _day(pool, pki_settings, dev["device_id"])
    ctx = make_ctx(pool, pki_settings)  # no script from the model: the rule script
    assert await vlogs.write_vlog_script(ctx, DAY) == "scripted"
    row = await fetch_one(pool, "SELECT script, model_version FROM vlog WHERE day = %s", (DAY,))
    assert row["model_version"] == "rule"
    [talk] = [h for h in row["script"]["highlights"] if h["kind"] == "talk"]
    assert talk["line"] == talk["speech"] == "nói dài"
    assert await vlogs.render_vlog(ctx, DAY) is True
    vtt = (pki_settings.media_root / vlogs.vlog_rel(DAY)).with_suffix(".vtt").read_text(encoding="utf-8")
    assert vtt.count("nói dài") == 1
    assert vtt.count("-->") == 3  # the title, the notable picture, the conversation


async def test_a_script_overtaken_by_a_forget_is_not_stored(pool, pki_settings, dev, jobs_open, monkeypatch):
    await _day(pool, pki_settings, dev["device_id"], talk=False)
    real = vlogs.write_script

    async def write_then_forget(*args):
        written = await real(*args)
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO forget_log (id, range_start, range_end, counts) VALUES ('F1', %s, %s, '{}')",
                               (NINE, NINE + dt.timedelta(hours=1)))
        return written

    monkeypatch.setattr(vlogs, "write_script", write_then_forget)
    assert await vlogs.write_vlog_script(make_ctx(pool, pki_settings), DAY) == "skipped"
    assert await fetch_all(pool, "SELECT day FROM vlog") == []


async def test_a_render_overtaken_by_a_forget_is_not_stored(pool, pki_settings, dev, jobs_open, monkeypatch):
    await _day(pool, pki_settings, dev["device_id"], talk=False)
    ctx = make_ctx(pool, pki_settings)
    await vlogs.write_vlog_script(ctx, DAY)
    real = render.render

    async def render_then_forget(**kwargs):
        result = await real(**kwargs)
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO forget_log (id, range_start, range_end, counts) VALUES ('F1', %s, %s, '{}')",
                               (NINE, NINE + dt.timedelta(hours=1)))
        return result

    monkeypatch.setattr(vlogs.render, "render", render_then_forget)
    assert await vlogs.render_vlog(ctx, DAY) is False
    assert (await fetch_one(pool, "SELECT state FROM vlog WHERE day = %s", (DAY,)))["state"] == "scripted"
    assert not (pki_settings.media_root / vlogs.vlog_rel(DAY)).exists() and _tmp_empty(pki_settings)


async def test_a_failed_render_stores_a_fixed_error_and_leaves_no_file(pool, pki_settings, dev, jobs_open, monkeypatch):
    await _day(pool, pki_settings, dev["device_id"], talk=False)
    ctx = make_ctx(pool, pki_settings)
    await vlogs.write_vlog_script(ctx, DAY)

    async def broken(**_kwargs):
        raise render.RenderError("assemble", 234, "Error in subtitle: Bữa SECRET")

    monkeypatch.setattr(vlogs.render, "render", broken)
    with pytest.raises(vlogs.VlogFailed):
        await vlogs.render_vlog(ctx, DAY)
    row = await fetch_one(pool, "SELECT state, error FROM vlog WHERE day = %s", (DAY,))
    assert row == {"state": "scripted", "error": "render failed at assemble (ffmpeg exit 234)"}
    [stored] = await fetch_all(pool, "SELECT error FROM day_job_error WHERE task = 'render_vlog'")
    assert "SECRET" not in stored["error"] and _tmp_empty(pki_settings)


async def test_rebuild_drops_failed_runs_marks_stale_and_asks_for_a_script(pool, pki_settings, jobs_open):
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO vlog (day, state, path) VALUES (%s, 'rendered', 'vlog/x.mp4')", (DAY,))
    await _fail_job(pool, await vlogs.defer_render(pool, DAY))
    assert await vlogs.request_rebuild(pool, DAY) is not None
    row = await fetch_one(pool, "SELECT stale, path FROM vlog WHERE day = %s", (DAY,))
    assert row == {"stale": True, "path": "vlog/x.mp4"}  # the vlog stays until its replacement is stored (R5)
    assert await fetch_all(pool, "SELECT task_name, status FROM procrastinate_jobs") == [
        {"task_name": "write_vlog_script", "status": "todo"}]


async def test_forget_deletes_the_touched_days_vlogs_and_their_files(pool, pki_settings, dev, jobs_open):
    rel = vlogs.vlog_rel(DAY)
    for suffix in (".mp4", ".vtt"):
        path = (pki_settings.media_root / rel).with_suffix(suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO vlog (day, state, path) VALUES (%s, 'rendered', %s)", (DAY, rel))
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, text="x")
    end = NINE + dt.timedelta(minutes=1)
    assert (await forget.preview(pool, NINE, end, pki_settings.zone)).vlogs == 1
    counts = await forget.forget_range(pool, pki_settings, NINE, end)
    assert counts.vlogs == 1 and counts.files_failed == 0
    assert await fetch_all(pool, "SELECT day FROM vlog") == []
    assert not (pki_settings.media_root / rel).exists()
    assert not (pki_settings.media_root / rel).with_suffix(".vtt").exists()


async def test_retention_never_deletes_a_vlog(pool, pki_settings):
    path = pki_settings.media_root / vlogs.vlog_rel(DAY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    await store.set_retention(pool, store.Retention(enabled=True))
    await retention.apply_retention(pool, pki_settings, now=NINE + dt.timedelta(days=30))
    assert path.exists()


async def _scripted(pool, settings, device_id, *, talk=False):
    """A day with its rule script stored (the render job not run)."""
    await _day(pool, settings, device_id, talk=talk)
    ctx = make_ctx(pool, settings)
    assert await vlogs.write_vlog_script(ctx, DAY) == "scripted"
    return ctx


async def _row(pool):
    return await fetch_one(pool, "SELECT state, path, source, error FROM vlog WHERE day = %s", (DAY,))


def _vtt(settings) -> str:
    return (settings.media_root / vlogs.vlog_rel(DAY)).with_suffix(".vtt").read_text(encoding="utf-8")


def test_the_planner_runs_every_15_minutes_and_is_never_retried_by_hand():
    periodic = {(p.task.name, p.cron) for p in jobs.job_app.periodic_registry.periodic_tasks.values()}
    assert (jobs.PLAN_VLOGS, "*/15 * * * *") in periodic
    assert jobs.PLAN_VLOGS in jobs.AUTO_RERUN_TASKS
    assert jobs.WRITE_VLOG_SCRIPT not in jobs.AUTO_RERUN_TASKS and jobs.RENDER_VLOG not in jobs.AUTO_RERUN_TASKS


def test_a_render_retries_after_a_fixed_minute_and_runs_three_times_at_most():
    # A render waiting for its retry stays `todo` and holds the `vlog` lock, so every other day's render waits too:
    # the wait must stay short (the exponential 60 s reached an hour on the second retry).
    strategy = jobs.job_app.tasks[jobs.RENDER_VLOG].retry_strategy
    assert strategy is vlogs.RENDER_RETRY
    for attempts in (0, 1):
        before = dt.datetime.now(dt.UTC)
        decision = strategy.get_retry_decision(exception=RuntimeError(), job=SimpleNamespace(attempts=attempts))
        after = dt.datetime.now(dt.UTC)
        assert before + dt.timedelta(seconds=60) <= decision.retry_at <= after + dt.timedelta(seconds=60)
    assert strategy.get_retry_decision(exception=RuntimeError(), job=SimpleNamespace(attempts=2)) is None


async def test_vlog_jobs_never_run_ahead_of_live_work_even_when_deferred_directly(pool, jobs_open):
    await jobs.job_app.tasks[jobs.WRITE_VLOG_SCRIPT].defer_async(day="2026-10-14")
    await jobs.job_app.tasks[jobs.RENDER_VLOG].defer_async(day="2026-10-15")
    assert await fetch_all(pool, "SELECT task_name, queue_name, lock, priority FROM procrastinate_jobs ORDER BY id") == [
        {"task_name": "write_vlog_script", "queue_name": "understand", "lock": "llm", "priority": -10},
        {"task_name": "render_vlog", "queue_name": "understand", "lock": "vlog", "priority": -10},
    ]


async def test_the_planner_job_asks_for_the_days_of_the_current_context(pool, pki_settings, jobs_open):
    await _recap(pool, DAY)
    set_context(make_ctx(pool, pki_settings))
    try:
        await vlogs.plan_vlogs_job(0)
    finally:
        set_context(None)
    assert [j["task_name"] for j in (await jobs_by_lock(pool))["vlog:2026-10-14"]] == ["write_vlog_script"]


async def test_the_worker_scripts_then_renders_a_day(pool, pki_settings, dev, jobs_open):
    await _day(pool, pki_settings, dev["device_id"], talk=False)
    await vlogs.defer_script(pool, DAY)
    await run_jobs(make_ctx(pool, pki_settings))
    assert await _row(pool) == {"state": "rendered", "path": vlogs.vlog_rel(DAY), "source": "video", "error": None}
    assert {r["task_name"]: r["status"] for r in await fetch_all(pool, "SELECT task_name, status FROM procrastinate_jobs")} == {
        "write_vlog_script": "succeeded", "render_vlog": "succeeded"}


async def test_nothing_is_rendered_without_a_scripted_vlog(pool, pki_settings, jobs_open):
    ctx = make_ctx(pool, pki_settings)
    assert await vlogs.render_vlog(ctx, DAY) is False  # no row
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO vlog (day, state, path) VALUES (%s, 'rendered', 'vlog/x.mp4')", (DAY,))
    assert await vlogs.render_vlog(ctx, DAY) is False  # already rendered
    assert _tmp_empty(pki_settings)


async def test_a_script_that_fails_is_recorded_for_the_dead_letter_list(pool, pki_settings, dev, jobs_open, monkeypatch):
    await _day(pool, pki_settings, dev["device_id"], talk=False)

    async def broken(*_args):
        raise RuntimeError("model server down")

    monkeypatch.setattr(vlogs, "write_script", broken)
    with pytest.raises(RuntimeError):
        await vlogs.write_vlog_script(make_ctx(pool, pki_settings), DAY)
    assert await fetch_all(pool, "SELECT day FROM vlog") == []
    [stored] = await fetch_all(pool, "SELECT error FROM day_job_error WHERE task = 'write_vlog_script'")
    assert stored["error"].startswith("RuntimeError")


async def test_a_day_left_without_activity_loses_its_earlier_vlog(pool, pki_settings, jobs_open):
    rel = vlogs.vlog_rel(DAY)
    for suffix in (".mp4", ".vtt"):
        path = (pki_settings.media_root / rel).with_suffix(suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO vlog (day, state, script, model_version, source, path, duration_s, generated_at)"
            " VALUES (%s, 'rendered', '{}', 'rule', 'video', %s, 30, now())", (DAY, rel))
    assert await vlogs.write_vlog_script(make_ctx(pool, pki_settings), DAY) == "empty"
    row = await fetch_one(pool, "SELECT state, script, model_version, source, path, duration_s, generated_at FROM vlog")
    assert row == {"state": "empty", "script": None, "model_version": None, "source": None, "path": None,
                   "duration_s": None, "generated_at": None}
    assert not (pki_settings.media_root / rel).exists() and not (pki_settings.media_root / rel).with_suffix(".vtt").exists()


async def test_a_script_changed_while_the_vlog_rendered_is_not_overwritten(pool, pki_settings, dev, jobs_open, monkeypatch):
    ctx = await _scripted(pool, pki_settings, dev["device_id"])
    real = render.render

    async def render_then_rescript(**kwargs):
        result = await real(**kwargs)
        async with pool.connection() as conn:
            await conn.execute("UPDATE vlog SET scripted_at = scripted_at + interval '1 second' WHERE day = %s", (DAY,))
        return result

    monkeypatch.setattr(vlogs.render, "render", render_then_rescript)
    assert await vlogs.render_vlog(ctx, DAY) is False
    assert (await _row(pool))["state"] == "scripted"
    assert not (pki_settings.media_root / vlogs.vlog_rel(DAY)).exists() and _tmp_empty(pki_settings)


async def test_a_render_that_takes_too_long_fails_with_a_fixed_message(pool, pki_settings, dev, jobs_open, monkeypatch):
    ctx = await _scripted(pool, pki_settings, dev["device_id"])

    async def slow(**_kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(vlogs.render, "render", slow)
    monkeypatch.setattr(vlogs, "RENDER_TIMEOUT_S", 0.05)
    with pytest.raises(vlogs.VlogFailed, match="render took longer than"):
        await vlogs.render_vlog(ctx, DAY)
    assert (await _row(pool))["error"].startswith("render took longer than") and _tmp_empty(pki_settings)


async def test_unreadable_media_fails_the_render_with_a_fixed_message(pool, pki_settings, dev, jobs_open, monkeypatch):
    ctx = await _scripted(pool, pki_settings, dev["device_id"])

    async def unreadable(**_kwargs):
        raise render.InvalidMedia("/media/secret-name.jpg: not a JPEG")

    monkeypatch.setattr(vlogs.render, "render", unreadable)
    with pytest.raises(vlogs.VlogFailed, match="a picture or recording could not be read"):
        await vlogs.render_vlog(ctx, DAY)
    row = await _row(pool)
    assert (row["state"], row["error"]) == ("scripted", "a picture or recording could not be read")
    assert _tmp_empty(pki_settings)


async def test_an_unexpected_failure_is_stored_without_its_message_and_leaves_no_file(
        pool, pki_settings, dev, jobs_open, monkeypatch):
    ctx = await _scripted(pool, pki_settings, dev["device_id"])

    async def broken(**_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(vlogs.render, "render", broken)
    with pytest.raises(RuntimeError):
        await vlogs.render_vlog(ctx, DAY)
    assert (await _row(pool))["error"] == "render failed" and _tmp_empty(pki_settings)


async def test_a_highlight_whose_picture_is_gone_is_left_out(pool, pki_settings, dev, jobs_open):
    ctx = await _scripted(pool, pki_settings, dev["device_id"])
    script = (await fetch_one(pool, "SELECT script FROM vlog"))["script"]
    [highlight] = script["highlights"]  # the one notable picture
    (pki_settings.media_root / highlight["jpeg"]).unlink()  # forgotten after the script was written
    assert await vlogs.render_vlog(ctx, DAY) is True
    assert _vtt(pki_settings).count("-->") == 1  # the title only


async def test_a_path_leaving_the_media_root_is_not_an_input(pool, pki_settings, caplog):
    root = pki_settings.media_root
    with caplog.at_level(logging.WARNING, logger="loopi_brain.pipeline.vlogs"):
        assert vlogs._inside(root, "../outside.jpg") is None
        assert vlogs._inside(root, None) is None
        stills = await vlogs._stills(pool, root, {"highlights": [{"jpeg": "../outside.jpg", "line": "x", "at": NINE.isoformat()}]})
    assert stills == [] and "leaves the media root" in caplog.text


async def test_a_sound_is_cut_only_from_kept_readable_audio(pool, pki_settings, dev):
    root, device = pki_settings.media_root, dev["device_id"]
    good = await insert_item(pool, device, kind="audio", started_at=NINE, seconds=5)
    raw = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (good,)))["raw_path"]
    view = root / media.audio_view_path(PurePosixPath(raw))
    assert await vlogs._sound(pool, root, "01NOSUCHITEM000000000000") is None  # not an audio item
    assert await vlogs._sound(pool, root, good) is None  # its raw file is missing
    make_opus(root / raw, "sine=frequency=440:sample_rate=48000", 5)
    assert await vlogs._sound(pool, root, good) == view.resolve() and view.is_file()  # made now ...
    stamp = view.stat().st_mtime_ns
    assert await vlogs._sound(pool, root, good) == view.resolve() and view.stat().st_mtime_ns == stamp  # ... then reused
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE id = %s", (good,))
    assert await vlogs._sound(pool, root, good) is None  # raw audio expired


async def test_a_sound_that_cannot_be_made_is_skipped(pool, pki_settings, dev, caplog):
    root, device = pki_settings.media_root, dev["device_id"]
    bad = await insert_item(pool, device, kind="audio", started_at=NINE, seconds=5)
    raw = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (bad,)))["raw_path"]
    (root / raw).parent.mkdir(parents=True, exist_ok=True)
    (root / raw).write_bytes(b"this is not Opus")
    with caplog.at_level(logging.WARNING, logger="loopi_brain.pipeline.vlogs"):
        assert await vlogs._sound(pool, root, bad) is None
    assert "vlog sound of" in caplog.text and "this is not Opus" not in caplog.text
    assert not (root / media.audio_view_path(PurePosixPath(raw))).exists()
    outside = await insert_item(pool, device, kind="audio", started_at=NINE, seconds=5, raw_path="../escape.opus")
    assert await vlogs._sound(pool, root, outside) is None  # its path leaves the media root


async def test_a_sound_copy_made_after_its_audio_was_forgotten_is_removed(pool, pki_settings, dev, monkeypatch):
    root, device = pki_settings.media_root, dev["device_id"]
    audio = await insert_item(pool, device, kind="audio", started_at=NINE, seconds=5)
    raw = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (audio,)))["raw_path"]
    make_opus(root / raw, "sine=frequency=440:sample_rate=48000", 5)
    real = media.normalise_audio

    async def made_then_forgotten(src, dst):
        gain = await real(src, dst)
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM item WHERE id = %s", (audio,))
        return gain

    monkeypatch.setattr(media, "normalise_audio", made_then_forgotten)
    assert await vlogs._sound(pool, root, audio) is None
    assert not (root / media.audio_view_path(PurePosixPath(raw))).exists()


async def test_a_rebuild_that_cannot_be_queued_is_logged_and_the_vlog_stays_marked(pool, pki_settings, jobs_open,
                                                                                    monkeypatch, caplog):
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO vlog (day, state, path) VALUES (%s, 'rendered', 'vlog/x.mp4')", (DAY,))

    async def queue_down(pool, day):
        raise RuntimeError("queue down")

    monkeypatch.setattr(vlogs, "defer_script", queue_down)
    with caplog.at_level(logging.ERROR, logger="loopi_brain.pipeline.vlogs"):
        assert await vlogs.request_rebuild(pool, DAY) is None
    assert "could not ask for the vlog of 2026-10-14" in caplog.text
    assert (await fetch_one(pool, "SELECT stale FROM vlog"))["stale"] is True  # the planner asks again


async def test_a_render_failure_still_surfaces_when_its_error_cannot_be_stored_on_the_row(
        pool, pki_settings, dev, jobs_open, monkeypatch, caplog):
    ctx = await _scripted(pool, pki_settings, dev["device_id"])

    async def broken(**_kwargs):
        raise render.RenderError("assemble", 1, "ffmpeg's last words")

    monkeypatch.setattr(vlogs.render, "render", broken)
    async with pool.connection() as conn:  # the vlog row refuses every update
        await conn.execute("CREATE FUNCTION vlog_refuses() RETURNS trigger LANGUAGE plpgsql"
                           " AS $$ BEGIN RAISE EXCEPTION 'refused'; END $$")
        await conn.execute("CREATE TRIGGER vlog_refuses BEFORE UPDATE ON vlog FOR EACH ROW EXECUTE FUNCTION vlog_refuses()")
    try:
        with caplog.at_level(logging.ERROR, logger="loopi_brain.pipeline.vlogs"), pytest.raises(vlogs.VlogFailed):
            await vlogs.render_vlog(ctx, DAY)
    finally:
        async with pool.connection() as conn:
            await conn.execute("DROP TRIGGER vlog_refuses ON vlog")
            await conn.execute("DROP FUNCTION vlog_refuses()")
    assert "could not record the vlog error of 2026-10-14" in caplog.text
    assert (await _row(pool))["error"] is None
    assert len(await fetch_all(pool, "SELECT error FROM day_job_error WHERE task = 'render_vlog'")) == 1
    assert _tmp_empty(pki_settings)


# ---- fix round 1


def _final_files(settings):
    mp4 = settings.media_root / vlogs.vlog_rel(DAY)
    return mp4, mp4.with_suffix(".vtt")


def _write_final_files(settings):
    for path in _final_files(settings):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")


def _leftover_tmp(settings, name):
    leftover = settings.media_root / vlogs.TMP_DIR / name
    leftover.mkdir(parents=True)
    (leftover / "subs.srt").write_text("secret subtitle", encoding="utf-8")
    return leftover


async def test_forget_removes_a_vlog_file_the_row_does_not_name(pool, pki_settings, dev, jobs_open):
    """I1: a render that renamed its files and then failed to commit leaves files with ``path`` still NULL."""
    _write_final_files(pki_settings)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO vlog (day, state, script) VALUES (%s, 'scripted', '{}')", (DAY,))
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, text="x")
    counts = await forget.forget_range(pool, pki_settings, NINE, NINE + dt.timedelta(minutes=1))
    assert counts.vlogs == 1 and counts.files == 2 and counts.files_failed == 0
    assert not any(path.exists() for path in _final_files(pki_settings))


async def test_forget_removes_vlog_files_even_when_the_day_has_no_vlog_row(pool, pki_settings, dev, jobs_open):
    _write_final_files(pki_settings)
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, text="x")
    other = pki_settings.media_root / vlogs.vlog_rel(dt.date(2026, 10, 20))  # a day the forget does not touch
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"x")
    await forget.forget_range(pool, pki_settings, NINE, NINE + dt.timedelta(minutes=1))
    assert not any(path.exists() for path in _final_files(pki_settings)) and other.exists()


async def test_a_day_stored_empty_removes_a_vlog_file_its_row_did_not_name(pool, pki_settings, jobs_open):
    _write_final_files(pki_settings)  # no row at all
    assert await vlogs.write_vlog_script(make_ctx(pool, pki_settings), DAY) == "empty"
    assert not any(path.exists() for path in _final_files(pki_settings))


async def test_the_audit_row_of_a_forget_lists_no_vlog_file(pool, pki_settings, dev, jobs_open):
    """Minor 3: a vlog path is fixed and the planner remakes the file, so the 03:00 retry must never get it."""
    if os.geteuid() == 0:
        pytest.skip("a read-only folder does not stop root")
    _write_final_files(pki_settings)
    folder = _final_files(pki_settings)[0].parent
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO vlog (day, state, path) VALUES (%s, 'rendered', %s)", (DAY, vlogs.vlog_rel(DAY)))
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, text="x")
    folder.chmod(0o500)  # the unlink fails
    try:
        counts = await forget.forget_range(pool, pki_settings, NINE, NINE + dt.timedelta(minutes=1))
    finally:
        folder.chmod(0o700)
    assert counts.files_failed == 2 and all(path.exists() for path in _final_files(pki_settings))
    [audit] = await fetch_all(pool, "SELECT pending_files FROM forget_log")
    assert audit["pending_files"] == []


async def test_a_render_clears_the_temp_folders_a_killed_render_left(pool, pki_settings, dev, jobs_open):
    """I2: a SIGKILL skips ``finally``; the ``vlog`` lock lets one render run at a time, so all of it is dead."""
    ctx = await _scripted(pool, pki_settings, dev["device_id"])
    old_one, old_two = _leftover_tmp(pki_settings, "2026-10-14-aaaa"), _leftover_tmp(pki_settings, "2026-10-13-bbbb")
    assert await vlogs.render_vlog(ctx, DAY) is True
    assert not old_one.exists() and not old_two.exists() and _tmp_empty(pki_settings)


async def test_forget_clears_the_temp_folders_of_the_days_it_touches(pool, pki_settings, dev, jobs_open):
    touched, other = _leftover_tmp(pki_settings, "2026-10-14-aaaa"), _leftover_tmp(pki_settings, "2026-10-15-bbbb")
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, text="x")
    await forget.forget_range(pool, pki_settings, NINE, NINE + dt.timedelta(minutes=1))
    assert not touched.exists() and other.exists()


def _nul_script(body):
    return {"title": "Một ngày\x00 ở nhà", "highlights": [{"ref": "H1", "line": "Nấu ăn\x00 SECRETLINE\ud800", "audio": False}]}


async def test_a_script_with_nul_or_a_lone_surrogate_is_stored_clean_and_records_no_error(
        pool, pki_settings, dev, jobs_open):
    """I3: PostgreSQL refuses NUL in JSONB and quotes the text around it in its error."""
    await _day(pool, pki_settings, dev["device_id"], talk=False)
    ollama = FakeOllama()
    ollama.reply = _nul_script
    assert await vlogs.write_vlog_script(make_ctx(pool, pki_settings, ollama=ollama), DAY) == "scripted"
    script = (await fetch_one(pool, "SELECT script FROM vlog"))["script"]
    assert script["title"] == "Một ngày ở nhà"
    assert [h["line"] for h in script["highlights"]] == ["Nấu ăn SECRETLINE"]
    assert await fetch_all(pool, "SELECT task FROM day_job_error") == []


async def test_a_rebuild_replaces_a_render_that_is_waiting(pool, pki_settings, jobs_open):
    """Minor 1: the day's queueing lock is shared; a waiting render would swallow the new script request."""
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO vlog (day, state, script) VALUES (%s, 'scripted', '{}')", (DAY,))
    render_id = await vlogs.defer_render(pool, DAY)
    other = await vlogs.defer_render(pool, dt.date(2026, 10, 15))
    script_id = await vlogs.request_rebuild(pool, DAY)
    assert script_id not in (None, render_id)
    assert {r["id"]: r["task_name"] for r in await fetch_all(pool, "SELECT id, task_name FROM procrastinate_jobs")} == {
        script_id: "write_vlog_script", other: "render_vlog"}  # another day's render is not touched


async def test_a_rebuild_leaves_a_render_that_is_running(pool, pki_settings, jobs_open):
    render_id = await vlogs.defer_render(pool, DAY)
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'doing' WHERE id = %s", (render_id,))
    script_id = await vlogs.request_rebuild(pool, DAY)
    assert {r["id"]: r["status"] for r in await fetch_all(pool, "SELECT id, status FROM procrastinate_jobs")} == {
        render_id: "doing", script_id: "todo"}


async def test_the_forget_count_is_read_before_the_row_when_a_vlog_renders(pool, pki_settings, dev, jobs_open, monkeypatch):
    """Minor 7: what the render goes on is the row read *after* the count (as ``write_vlog_script`` does), so a change
    between the two reads is seen by both or caught at the store, never half of each."""
    ctx = await _scripted(pool, pki_settings, dev["device_id"])
    real, calls = vlogs.forgotten, []

    async def forgotten_then_rescripted(conn, start, end):
        count = await real(conn, start, end)
        if not calls:  # the first read, i.e. the one before the row
            async with pool.connection() as other:
                await other.execute("UPDATE vlog SET scripted_at = scripted_at + interval '1 second'")
        calls.append(count)
        return count

    monkeypatch.setattr(vlogs, "forgotten", forgotten_then_rescripted)
    assert await vlogs.render_vlog(ctx, DAY) is True  # it read the new ``scripted_at`` and stored against it


async def test_a_forget_whose_vlog_files_cannot_be_removed_still_reports_and_keeps_the_audit_row_clean(
        pool, pki_settings, dev, jobs_open, monkeypatch, caplog):
    def crash(root, days):
        raise RuntimeError("the disk fell off")

    monkeypatch.setattr(vlogs, "remove_forgotten", crash)
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, text="x")
    with caplog.at_level(logging.ERROR, logger="loopi_brain.forget"):
        counts = await forget.forget_range(pool, pki_settings, NINE, NINE + dt.timedelta(minutes=1))
    assert (counts.moments, counts.files_failed) == (1, 1)
    assert "deleting the vlog files of the forgotten days failed" in caplog.text
    assert (await fetch_one(pool, "SELECT pending_files FROM forget_log"))["pending_files"] == []

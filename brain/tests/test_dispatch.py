import asyncio
import datetime as dt
import json
import types

import pytest

from helpers import (
    T0,
    FakeOllama,
    fetch_all,
    fetch_one,
    insert_item,
    insert_moment,
    item_headers,
    jobs_by_lock,
    new_ulid,
    paired_device,
    stall_jobs,
)
from loopi_brain.pipeline import dispatch, jobs
from loopi_brain.providers.config import DEFAULT_CONFIG
from loopi_brain.providers.router import build_router


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def ollama():
    return FakeOllama()


@pytest.fixture
def router(pki_settings, ollama):
    return build_router(DEFAULT_CONFIG, pki_settings, ollama.client())


async def _insert_keyframe(pool, item_id, device_id, ts, caption=None) -> str:
    kf_id = new_ulid()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO keyframe (id, item_id, device_id, ts, frame_index, path, dhash, luma, caption)"
            " VALUES (%s, %s, %s, %s, %s, %s, 0, 100, %s)",
            (kf_id, item_id, device_id, ts, int(ts.timestamp()) % 1000, f"keyframes/{kf_id}.jpg",
             json.dumps(caption) if caption else None),
        )
    return kf_id


async def test_defer_item_creates_one_job_per_kind_and_records_it(pool, dev, jobs_open):
    device_id = dev["device_id"]
    video = await insert_item(pool, device_id, kind="video")
    audio = await insert_item(pool, device_id, kind="audio")
    marker = await insert_item(pool, device_id, kind="marker")
    for item_id in (video, audio, marker):
        assert await dispatch.defer_item(pool, item_id) is not None
        assert await dispatch.defer_item(pool, item_id) is None  # already has a job
    by_lock = await jobs_by_lock(pool)
    expected = {
        video: (jobs.ANALYZE_VIDEO, "video", f"video:{device_id}"),
        audio: (jobs.TRANSCRIBE_AUDIO, "audio", "asr"),
        marker: (jobs.INDEX_MARKER, "light", None),
    }
    for item_id, (task, queue, lock) in expected.items():
        [job] = by_lock[f"item:{item_id}"]
        assert (job["task_name"], job["queue_name"], job["lock"], job["args"]) == (task, queue, lock, {"item_id": item_id})
        row = await fetch_one(pool, "SELECT analysis_job_id FROM item WHERE id = %s", (item_id,))
        assert row["analysis_job_id"] == job["id"]


async def test_todo_job_holding_the_queueing_lock_is_adopted(pool, dev, jobs_open):
    item_id = await insert_item(pool, dev["device_id"], kind="marker")
    async with pool.connection() as conn, conn.transaction():  # e.g. deferred, then the row update was lost
        orphan = await jobs.enqueue(conn, jobs.INDEX_MARKER, queue="light", queueing_lock=f"item:{item_id}", item_id=item_id)
    assert await dispatch.defer_item(pool, item_id) == orphan
    assert len((await jobs_by_lock(pool))[f"item:{item_id}"]) == 1


async def test_backlog_and_fast_path_race_give_each_item_exactly_one_job(pool, pki_settings, router, jobs_open):
    devices = [(await paired_device(pool, pki_settings))["device_id"] for _ in range(3)]
    ids = [
        await insert_item(pool, devices[n % 3], kind=("video", "audio", "marker")[n % 3],
                          started_at=T0 + dt.timedelta(minutes=n))
        for n in range(300)
    ]
    fast_path = [dispatch.defer_item(pool, i) for i in reversed(ids[::7])]
    tick = dispatch.dispatch_pending(pool, router, max_queued=300)  # no backlog cap: this tests exactly-once
    await asyncio.gather(tick, dispatch.dispatch_pending(pool, router, max_queued=300), *fast_path)
    by_lock = await jobs_by_lock(pool)
    assert sorted(by_lock) == sorted(f"item:{i}" for i in ids)
    assert all(len(v) == 1 for v in by_lock.values())
    rows = await fetch_all(pool, "SELECT id, analysis_job_id FROM item")
    assert {r["id"]: r["analysis_job_id"] for r in rows} == {i: by_lock[f"item:{i}"][0]["id"] for i in ids}


async def _run_all_waiting(pool):
    """Pretend the worker ran every waiting job."""
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'succeeded' WHERE status = 'todo'")


async def test_dispatcher_defers_oldest_first_so_device_order_follows_started_at(pool, dev, router, jobs_open):
    started = [T0 + dt.timedelta(minutes=m) for m in (5, 1, 3, 2, 4)]
    ids = {await insert_item(pool, dev["device_id"], started_at=s): s for s in started}
    for expected in (2, 2, 1):
        assert await dispatch.dispatch_pending(pool, router, max_queued=2) == (expected, 0)
        await _run_all_waiting(pool)
    rows = await fetch_all(
        pool, "SELECT j.id, j.args->>'item_id' AS item_id FROM procrastinate_jobs j ORDER BY j.id"
    )
    assert [ids[r["item_id"]] for r in rows] == sorted(started)


async def test_backlog_is_fed_a_few_jobs_at_a_time_and_uploads_skip_the_line(pool, dev, router, jobs_open):
    for n in range(100):
        await insert_item(pool, dev["device_id"], kind="audio", started_at=T0 + dt.timedelta(minutes=n))
    for n in range(3):
        await insert_item(pool, dev["device_id"], kind="marker", started_at=T0 + dt.timedelta(minutes=n), seconds=0)
    assert await dispatch.dispatch_pending(pool, router) == (23, 0)  # 20 of the audio backlog, every marker
    assert await dispatch.dispatch_pending(pool, router) == (0, 0)  # 20 transcriptions already wait
    fresh = await insert_item(pool, dev["device_id"], kind="audio", started_at=T0 + dt.timedelta(days=1))
    assert await dispatch.defer_item(pool, fresh) is not None  # the upload fast path is never capped
    async with pool.connection() as conn:  # the worker finishes 15 of the 21 waiting transcriptions
        await conn.execute(
            "UPDATE procrastinate_jobs SET status = 'succeeded' WHERE id IN (SELECT id FROM procrastinate_jobs"
            " WHERE status = 'todo' AND task_name = 'transcribe_audio' ORDER BY id LIMIT 15)"
        )
    assert await dispatch.dispatch_pending(pool, router) == (14, 0)  # topped up to 20 waiting again
    waiting = await fetch_one(
        pool, "SELECT count(*) AS n FROM procrastinate_jobs WHERE status = 'todo' AND task_name = 'transcribe_audio'"
    )
    assert waiting["n"] == 20


async def test_done_skipped_and_dead_lettered_items_are_not_redeferred(pool, dev, router, jobs_open):
    done = await insert_item(pool, dev["device_id"])
    skipped = await insert_item(pool, dev["device_id"], kind="audio")
    failed = await insert_item(pool, dev["device_id"], kind="marker")
    job_id = await dispatch.defer_item(pool, failed)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (done,))
        await conn.execute("UPDATE item SET analysis_state = 'skipped' WHERE id = %s", (skipped,))
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed' WHERE id = %s", (job_id,))
    assert await dispatch.dispatch_pending(pool, router) == (0, 0)
    assert [r["id"] for r in await fetch_all(pool, "SELECT id FROM procrastinate_jobs")] == [job_id]


async def _done_video_with_keyframes(pool, device_id) -> tuple[str, str]:
    """A done video item with one uncaptioned and one captioned keyframe; returns (item, waiting keyframe)."""
    item_id = await insert_item(pool, device_id)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (item_id,))
    waiting = await _insert_keyframe(pool, item_id, device_id, T0)
    await _insert_keyframe(pool, item_id, device_id, T0 + dt.timedelta(minutes=1), caption={"scene": "x"})
    return item_id, waiting


async def test_healthy_tick_gives_uncaptioned_keyframes_one_caption_job(pool, dev, router, jobs_open):
    _item, waiting = await _done_video_with_keyframes(pool, dev["device_id"])
    assert await dispatch.dispatch_pending(pool, router) == (0, 1)
    assert await dispatch.dispatch_pending(pool, router) == (0, 0)
    [job] = (await jobs_by_lock(pool))[f"kf:{waiting}"]
    assert (job["task_name"], job["queue_name"], job["args"]) == (jobs.CAPTION_KEYFRAME, "caption", {"keyframe_id": waiting})
    row = await fetch_one(pool, "SELECT caption_job_id FROM keyframe WHERE id = %s", (waiting,))
    assert row["caption_job_id"] == job["id"]


async def test_outage_tick_defers_no_caption_job(pool, dev, router, ollama, jobs_open, caplog):
    _item, waiting = await _done_video_with_keyframes(pool, dev["device_id"])
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption_error = 'ProviderUnavailable: down' WHERE id = %s", (waiting,))
    ollama.down = True
    assert await dispatch.dispatch_pending(pool, router) == (0, 0)
    assert await jobs_by_lock(pool) == {}
    row = await fetch_one(pool, "SELECT caption_job_id, caption_error FROM keyframe WHERE id = %s", (waiting,))
    assert row == {"caption_job_id": None, "caption_error": "ProviderUnavailable: down"}
    assert "model server is unreachable: captions and embeddings wait for the next tick" in caplog.text
    ollama.down = False
    assert await dispatch.dispatch_pending(pool, router) == (0, 1)


async def test_tick_without_the_caption_model_defers_no_caption_job(pool, dev, ollama, router, jobs_open, caplog):
    await _done_video_with_keyframes(pool, dev["device_id"])
    ollama.models.remove("minicpm-v4.6:1b")
    assert await dispatch.dispatch_pending(pool, router) == (0, 0)
    assert await jobs_by_lock(pool) == {}
    assert "model server lacks minicpm-v4.6:1b: captions wait for the next tick" in caplog.text
    assert [p for p in ollama.paths() if p != "/api/tags"] == []  # one health check per tick, nothing else


async def test_tick_asks_for_embeddings_only_when_moments_wait_and_the_model_is_there(
    pool, dev, ollama, router, jobs_open
):
    assert await dispatch.dispatch_pending(pool, router) == (0, 0)
    assert "embed" not in await jobs_by_lock(pool)  # nothing waits
    await insert_moment(pool, dev["device_id"])
    ollama.models.remove("qwen3-embedding:0.6b")
    await dispatch.dispatch_pending(pool, router)
    assert "embed" not in await jobs_by_lock(pool)  # model missing
    ollama.models.append("qwen3-embedding:0.6b")
    await dispatch.dispatch_pending(pool, router)
    await dispatch.dispatch_pending(pool, router)
    [job] = (await jobs_by_lock(pool))["embed"]
    assert job["task_name"] == jobs.EMBED_MOMENTS


async def test_embedding_runs_are_coalesced(pool, jobs_open):
    await dispatch.defer_embed(pool)
    await dispatch.defer_embed(pool)
    [job] = (await jobs_by_lock(pool))["embed"]
    assert (job["task_name"], job["queue_name"], job["status"]) == (jobs.EMBED_MOMENTS, "light", "todo")


async def test_failed_embedding_runs_do_not_pile_up_on_the_dead_letter_list(pool, dev, router, jobs_open):
    for _ in range(2):  # the queueing lock covers only 'todo' runs, so a persistent error adds a failed run per tick
        await dispatch.defer_embed(pool)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE procrastinate_jobs SET status = 'failed' WHERE task_name = %s AND status = 'todo'",
                (jobs.EMBED_MOMENTS,),
            )
    newest_failed = (await fetch_one(pool, "SELECT max(id) AS id FROM procrastinate_jobs"))["id"]
    await insert_moment(pool, dev["device_id"])
    assert await dispatch.dispatch_pending(pool, router) == (0, 0)
    runs = await fetch_all(pool, "SELECT id, status FROM procrastinate_jobs WHERE task_name = %s ORDER BY id",
                           (jobs.EMBED_MOMENTS,))
    assert [r["status"] for r in runs] == ["failed", "todo"]
    assert runs[0]["id"] == newest_failed < runs[1]["id"]


async def test_a_tick_keeps_only_the_newest_failed_run_of_each_automatic_task(pool, dev, router, jobs_open):
    automatic = (jobs.EMBED_MOMENTS, jobs.DISPATCH_PENDING, jobs.RETRY_STALLED, jobs.REMOVE_OLD_JOBS)
    runs = {task: [] for task in automatic}
    for task in automatic:
        for _ in range(2):  # e.g. two scheduler ticks that failed
            async with pool.connection() as conn:
                job_id = await jobs.enqueue(conn, task, queue="light", queueing_lock=f"test-{task}")
                await conn.execute("UPDATE procrastinate_jobs SET status = 'failed' WHERE id = %s", (job_id,))
            runs[task].append(job_id)
    pipeline_failures = []
    for minute in range(2):  # dead letters with a retry button are never pruned
        item_id = await insert_item(pool, dev["device_id"], kind="marker", seconds=0,
                                    started_at=T0 + dt.timedelta(minutes=minute))
        pipeline_failures.append(await dispatch.defer_item(pool, item_id))
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed' WHERE id = ANY(%s)", (pipeline_failures,))
        # the scheduler's record of a periodic defer may still point at a failed tick
        await conn.execute(
            "INSERT INTO procrastinate_periodic_defers (task_name, defer_timestamp, job_id) VALUES (%s, 0, %s)",
            (jobs.DISPATCH_PENDING, runs[jobs.DISPATCH_PENDING][0]),
        )
    assert await dispatch.dispatch_pending(pool, router) == (0, 0)
    failed = await fetch_all(pool, "SELECT id FROM procrastinate_jobs WHERE status = 'failed' ORDER BY id")
    assert [r["id"] for r in failed] == sorted([newest for _, newest in runs.values()] + pipeline_failures)


async def test_upload_defers_analysis_on_the_fast_path(client, pool, dev):
    body = json.dumps({"note": "chìa khoá"}).encode()
    item_id = new_ulid()
    r = await client.put(f"/v1/items/{item_id}", content=body,
                         headers=item_headers(dev["fingerprint"], body, kind="marker", end="2026-10-14T01:00:00Z"))
    assert r.status_code == 201
    row = await fetch_one(pool, "SELECT analysis_state, analysis_job_id FROM item WHERE id = %s", (item_id,))
    [job] = (await jobs_by_lock(pool))[f"item:{item_id}"]
    assert row == {"analysis_state": "pending", "analysis_job_id": job["id"]}


async def test_fast_path_failure_never_affects_the_upload(client, pool, dev, router, monkeypatch, caplog):
    async def broken(*_a, **_k):
        raise RuntimeError("queue down")

    monkeypatch.setattr(jobs, "enqueue", broken)
    body = b'{"note": "x"}'
    item_id = new_ulid()
    r = await client.put(f"/v1/items/{item_id}", content=body,
                         headers=item_headers(dev["fingerprint"], body, kind="marker", end="2026-10-14T01:00:00Z"))
    assert r.status_code == 201
    assert f"could not defer analysis of item {item_id}" in caplog.text
    monkeypatch.undo()
    assert await dispatch.dispatch_pending(pool, router) == (1, 0)


async def test_stalled_jobs_are_retried_until_their_attempts_are_used_up(pool, dev, jobs_open):
    first = await insert_item(pool, dev["device_id"], kind="marker", seconds=0)
    killer = await insert_item(pool, dev["device_id"], kind="audio")
    running = await insert_item(pool, dev["device_id"], kind="marker", seconds=0, started_at=T0 + dt.timedelta(hours=1))
    jobs_by_item = {i: await dispatch.defer_item(pool, i) for i in (first, killer, running)}
    async with pool.connection() as conn:
        dead = (await (await conn.execute(
            "INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now() - interval '5 minutes') RETURNING id"
        )).fetchone())["id"]
        alive = (await (await conn.execute(
            "INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now()) RETURNING id"
        )).fetchone())["id"]
        for item_id, worker_id, attempts in ((first, dead, 1), (killer, dead, 3), (running, alive, 0)):
            await conn.execute(
                "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s, attempts = %s WHERE id = %s",
                (worker_id, attempts, jobs_by_item[item_id]),
            )
    assert await dispatch.recover_stalled(pool) == (1, 1)
    states = {
        r["id"]: (r["status"], r["attempts"])
        for r in await fetch_all(pool, "SELECT id, status, attempts FROM procrastinate_jobs")
    }
    assert states[jobs_by_item[first]] == ("todo", 2)
    assert states[jobs_by_item[killer]] == ("failed", 4)  # on the dead-letter list
    assert states[jobs_by_item[running]] == ("doing", 0)  # its worker is alive
    row = await fetch_one(pool, "SELECT analysis_error FROM item WHERE id = %s", (killer,))
    assert row["analysis_error"] == dispatch.STALLED_ERROR


async def test_a_caption_job_that_keeps_killing_the_worker_is_dead_lettered_with_the_reason(pool, dev, jobs_open):
    item_id = await insert_item(pool, dev["device_id"], kind="video")
    kf = await _insert_keyframe(pool, item_id, dev["device_id"], T0)
    job_id = await dispatch.defer_keyframe(pool, kf)
    await stall_jobs(pool, job_id, attempts=3)
    assert await dispatch.recover_stalled(pool) == (0, 1)
    job = await fetch_one(pool, "SELECT status, attempts FROM procrastinate_jobs WHERE id = %s", (job_id,))
    assert job == {"status": "failed", "attempts": 4}
    row = await fetch_one(pool, "SELECT caption_error, caption_job_id FROM keyframe WHERE id = %s", (kf,))
    assert row == {"caption_error": dispatch.STALLED_ERROR, "caption_job_id": job_id}


@pytest.mark.parametrize(("task", "lock"), [(jobs.EMBED_MOMENTS, "embed"), (jobs.DISPATCH_PENDING, "dispatch")])
async def test_a_stalled_run_next_to_a_waiting_one_is_dropped_not_dead_lettered(
    pool, dev, jobs_open, caplog, task, lock
):
    async with pool.connection() as conn:
        stalled = await jobs.enqueue(conn, task, queue="light", queueing_lock=lock)
    marker = await insert_item(pool, dev["device_id"], kind="marker", seconds=0)
    other = await dispatch.defer_item(pool, marker)
    await stall_jobs(pool, stalled, other)  # the worker died while running both
    async with pool.connection() as conn:  # deferred while the stalled run was running (the lock covers 'todo' only)
        waiting = await jobs.enqueue(conn, task, queue="light", queueing_lock=lock)
    assert await dispatch.recover_stalled(pool) == (2, 0)  # both pieces of work run again
    states = {r["id"]: r["status"] for r in await fetch_all(pool, "SELECT id, status FROM procrastinate_jobs")}
    assert states == {waiting: "todo", other: "todo"}  # the stalled run is gone, nothing is dead-lettered
    assert f"stalled job {stalled} ({task}) dropped: job {waiting} waits to do the same work" in caplog.text


async def test_a_dropped_stalled_job_hands_its_row_to_the_waiting_job(pool, dev, jobs_open):
    device_id = dev["device_id"]
    marker = await insert_item(pool, device_id, kind="marker", seconds=0)
    video = await insert_item(pool, device_id, kind="video")
    kf = await _insert_keyframe(pool, video, device_id, T0)
    stalled_item, stalled_kf = await dispatch.defer_item(pool, marker), await dispatch.defer_keyframe(pool, kf)
    await stall_jobs(pool, stalled_item, stalled_kf)
    async with pool.connection() as conn:
        # The caption job gave its keyframe back (model server outage) just before the worker died, and the
        # dispatcher deferred a new one; the marker was deferred again by hand.
        await conn.execute("UPDATE keyframe SET caption_job_id = NULL WHERE id = %s", (kf,))
        waiting_item = await jobs.enqueue(
            conn, jobs.INDEX_MARKER, queue="light", queueing_lock=f"item:{marker}", item_id=marker
        )
    waiting_kf = await dispatch.defer_keyframe(pool, kf)
    assert await dispatch.recover_stalled(pool) == (2, 0)
    item = await fetch_one(pool, "SELECT analysis_job_id FROM item WHERE id = %s", (marker,))
    keyframe = await fetch_one(pool, "SELECT caption_job_id FROM keyframe WHERE id = %s", (kf,))
    assert (item["analysis_job_id"], keyframe["caption_job_id"]) == (waiting_item, waiting_kf)
    assert [r["id"] for r in await fetch_all(pool, "SELECT id FROM procrastinate_jobs ORDER BY id")] == [
        waiting_item, waiting_kf,
    ]


async def test_enqueue_reraises_when_the_lock_holder_left_the_queue(pool, monkeypatch, jobs_open):
    from procrastinate.exceptions import AlreadyEnqueued

    class Taken:
        async def defer_async(self, **_kwargs):
            raise AlreadyEnqueued("taken")

    monkeypatch.setattr(jobs.job_app, "configure_task", lambda *_a, **_k: Taken())
    async with pool.connection() as conn, conn.transaction():
        with pytest.raises(AlreadyEnqueued):  # no 'todo' holder to adopt: the dispatcher retries next tick
            await jobs.enqueue(conn, jobs.INDEX_MARKER, queue="light", queueing_lock="item:gone", item_id="gone")


async def test_remove_old_jobs_keeps_failed_jobs(pool, dev, jobs_open):
    ok_item = await insert_item(pool, dev["device_id"], kind="marker")
    bad_item = await insert_item(pool, dev["device_id"], kind="marker")
    ok_job = await dispatch.defer_item(pool, ok_item)
    bad_job = await dispatch.defer_item(pool, bad_item)
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'succeeded' WHERE id = %s", (ok_job,))
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed' WHERE id = %s", (bad_job,))
        await conn.execute("UPDATE procrastinate_events SET at = now() - interval '8 days'")
    await dispatch.remove_old_jobs(types.SimpleNamespace(app=jobs.job_app))
    assert [r["id"] for r in await fetch_all(pool, "SELECT id FROM procrastinate_jobs")] == [bad_job]


async def test_periodic_tasks_are_registered():
    periodic = {(p.task.name, p.cron) for p in jobs.job_app.periodic_registry.periodic_tasks.values()}
    assert {
        (jobs.DISPATCH_PENDING, "* * * * *"),
        (jobs.RETRY_STALLED, "*/10 * * * *"),
        (jobs.REMOVE_OLD_JOBS, "15 4 * * *"),
    } <= periodic


async def test_api_lifespan_opens_the_job_app_on_its_pool(pool, pki_settings):
    from procrastinate.exceptions import AppNotOpen

    from loopi_brain.app import create_app

    app = create_app(pki_settings, pool=pool)
    async with app.router.lifespan_context(app):
        assert jobs.job_app.connector.pool is pool  # the retry button needs it
    with pytest.raises(AppNotOpen):
        _ = jobs.job_app.connector.pool

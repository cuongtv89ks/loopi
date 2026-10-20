import datetime as dt
import os
import subprocess
import sys

import pytest
from procrastinate.periodic import PeriodicRegistry

from helpers import (
    T0,
    TWO_SCENES,
    FakeOllama,
    FakeTranscriber,
    fetch_all,
    fetch_one,
    insert_item,
    make_ctx,
    make_h264,
    make_opus,
    paired_device,
    run_jobs,
    stall_jobs,
)
from loopi_brain import cli, health
from loopi_brain.config import Settings
from loopi_brain.pipeline import asr, context, dispatch, jobs, worker
from loopi_brain.pipeline.asr import Region, RegionResult, SegmentResult
from loopi_brain.providers.config import DEFAULT_CONFIG


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


async def _with_file(pool, settings, item_id, write):
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    write(settings.media_root / row["raw_path"])


def test_every_pipeline_task_is_registered():
    jobs.job_app.perform_import_paths()
    assert set(jobs.PIPELINE_TASKS) | {jobs.DISPATCH_PENDING, jobs.RETRY_STALLED, jobs.REMOVE_OLD_JOBS} <= set(
        jobs.job_app.tasks
    )
    assert jobs.DETECT_SPEECH in jobs.job_app.tasks  # the audio item's first job (VAD + SNR); Whisper runs after it


async def test_a_real_worker_runs_every_stage(pool, pki_settings, dev, jobs_open, monkeypatch):
    device_id = dev["device_id"]
    video = await insert_item(pool, device_id, kind="video", seconds=80)
    await _with_file(pool, pki_settings, video, lambda p: make_h264(p, TWO_SCENES, 80))
    audio = await insert_item(pool, device_id, kind="audio", started_at=T0 + dt.timedelta(minutes=2), seconds=5)
    await _with_file(pool, pki_settings, audio, lambda p: make_opus(p, seconds=5))
    marker = await insert_item(pool, device_id, kind="marker", started_at=T0 + dt.timedelta(minutes=3), seconds=0,
                               meta={"note": "chìa khoá"})
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _threshold: [Region(1.0, 3.0)])
    speech = RegionResult("vi", 0.9, [SegmentResult(0.0, 2.0, "Chìa khoá ở trên bàn", -0.2, 0.02, 1.1)])
    ctx = make_ctx(pool, pki_settings, ollama=FakeOllama(), transcriber=FakeTranscriber(speech))

    assert await dispatch.dispatch_pending(pool, ctx.router) == (3, 0)
    await run_jobs(ctx)

    states = await fetch_all(pool, "SELECT id, analysis_state FROM item")
    assert {r["id"]: r["analysis_state"] for r in states} == {video: "done", audio: "done", marker: "done"}
    regions = await fetch_all(pool, "SELECT item_id, state FROM speech_region")
    assert [(r["item_id"], r["state"]) for r in regions] == [(audio, "chosen")]  # decided at the end of the detection
    assert [r["caption"] is not None for r in await fetch_all(pool, "SELECT caption FROM keyframe")] == [True, True]
    found = await fetch_all(pool, "SELECT kind, text, embedding FROM moment ORDER BY ts_start")
    assert [m["kind"] for m in found] == ["visual", "visual", "speech", "marker"]
    assert all(m["embedding"] is not None for m in found)
    statuses = await fetch_all(pool, "SELECT task_name, status FROM procrastinate_jobs")
    assert {r["status"] for r in statuses} == {"succeeded"}
    assert {r["task_name"] for r in statuses} == set(jobs.PIPELINE_TASKS)


async def test_run_worker_builds_its_context_and_recovers_stalled_jobs(pool, pki_settings, dev, jobs_open, monkeypatch):
    marker = await insert_item(pool, dev["device_id"], kind="marker", seconds=0)
    job_id = await dispatch.defer_item(pool, marker)
    async with pool.connection() as conn:  # the previous worker died while running it
        cur = await conn.execute(
            "INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now() - interval '5 minutes') RETURNING id"
        )
        await conn.execute(
            "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
            ((await cur.fetchone())["id"], job_id),
        )
    monkeypatch.setattr(jobs.job_app, "periodic_registry", PeriodicRegistry())
    settings = pki_settings.model_copy(update={"ollama_url": "http://127.0.0.1:9", "worker_concurrency": 1})
    await worker.run_worker(settings, wait=False, install_signal_handlers=False, listen_notify=False)
    moment = await fetch_one(pool, "SELECT kind, embedding FROM moment")
    assert moment == {"kind": "marker", "embedding": None}  # no model server: embedding waits
    job = await fetch_one(pool, "SELECT status, attempts FROM procrastinate_jobs WHERE id = %s", (job_id,))
    assert job == {"status": "succeeded", "attempts": 2}  # one lost run, one that finished
    with pytest.raises(RuntimeError):
        context.get_context()


@pytest.mark.parametrize(("task", "lock"), [(jobs.EMBED_MOMENTS, "embed"), (jobs.DISPATCH_PENDING, "dispatch")])
async def test_run_worker_starts_when_a_stalled_run_waits_next_to_a_newer_one(
    pool, pki_settings, jobs_open, monkeypatch, caplog, task, lock
):
    async with pool.connection() as conn:
        stalled = await jobs.enqueue(conn, task, queue="light", queueing_lock=lock)
    await stall_jobs(pool, stalled)  # killed mid-run (power cut, OOM) ...
    async with pool.connection() as conn:  # ... after the next run was deferred
        waiting = await jobs.enqueue(conn, task, queue="light", queueing_lock=lock)
    monkeypatch.setattr(jobs.job_app, "periodic_registry", PeriodicRegistry())
    settings = pki_settings.model_copy(update={"ollama_url": "http://127.0.0.1:9", "worker_concurrency": 1})
    await worker.run_worker(settings, wait=False, install_signal_handlers=False, listen_notify=False)
    jobs_left = await fetch_all(pool, "SELECT id, status FROM procrastinate_jobs")
    assert jobs_left == [{"id": waiting, "status": "succeeded"}]  # the stalled run is gone, the waiting one ran
    assert "could not recover stalled jobs" not in caplog.text


async def test_run_worker_starts_even_if_recovering_stalled_jobs_fails(
    pool, pki_settings, dev, jobs_open, monkeypatch, caplog
):
    async def broken(_pool):
        raise RuntimeError("recovery bug")

    await dispatch.defer_item(pool, await insert_item(pool, dev["device_id"], kind="marker", seconds=0))
    monkeypatch.setattr(worker, "recover_stalled", broken)
    monkeypatch.setattr(jobs.job_app, "periodic_registry", PeriodicRegistry())
    settings = pki_settings.model_copy(update={"ollama_url": "http://127.0.0.1:9", "worker_concurrency": 1})
    await worker.run_worker(settings, wait=False, install_signal_handlers=False, listen_notify=False)
    assert "could not recover stalled jobs" in caplog.text and "recovery bug" in caplog.text
    job = await fetch_one(pool, "SELECT status FROM procrastinate_jobs")
    assert job == {"status": "succeeded"}  # the worker ran anyway


async def test_worker_health_follows_the_heartbeat(pool, database_url, monkeypatch, capsys):
    monkeypatch.setenv("LOOPI_DATABASE_URL", database_url)
    assert health.worker_alive(database_url) is False
    assert cli.main(["worker-health"]) == 1
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now() - interval '5 minutes')")
    assert health.worker_alive(database_url) is False
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now())")
    assert health.worker_alive(database_url) is True
    assert cli.main(["worker-health"]) == 0
    assert "worker alive" in capsys.readouterr().out


def test_worker_health_command_imports_only_psycopg(pool, database_url):  # pool: empty tables
    code = (
        "import sys; from loopi_brain import cli; rc = cli.main(['worker-health']); heavy = sorted(m for m in"
        " ('numpy', 'procrastinate', 'cryptography', 'pgvector', 'pydantic', 'PIL', 'faster_whisper')"
        " if m in sys.modules); print(rc, heavy)"
    )
    out = subprocess.run([sys.executable, "-c", code], env={**os.environ, "LOOPI_DATABASE_URL": database_url},
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip().endswith("1 []")  # no worker in the test database, nothing heavy imported


async def test_models_pull_fetches_every_configured_model(pki_settings):
    fake = FakeOllama(models=[])
    downloads, lines = [], []
    pulled = await worker.pull_models(pki_settings, fake.client(), download=downloads.append, report=lines.append)
    configured = DEFAULT_CONFIG.ollama_models()
    assert [body["model"] for _, body in fake.requests] == configured
    assert pulled == [*configured, "faster-whisper/large-v3-turbo"]
    assert downloads == [pki_settings]
    assert len(lines) == len(configured) + 1


def test_models_pull_command(monkeypatch, capsys):
    async def fake_pull(settings):
        return ["qwen3:8b", "faster-whisper/large-v3-turbo"]

    monkeypatch.setenv("LOOPI_DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(worker, "pull_models", fake_pull)
    assert cli.main(["models", "pull"]) == 0
    assert "ready: qwen3:8b, faster-whisper/large-v3-turbo" in capsys.readouterr().out


def test_download_whisper_uses_the_model_dir(monkeypatch, tmp_path):
    import faster_whisper

    calls = []
    monkeypatch.setattr(faster_whisper, "download_model", lambda size, cache_dir: calls.append((size, cache_dir)))
    worker.download_whisper(Settings(database_url="postgresql://x", model_dir=tmp_path))
    assert calls == [("large-v3-turbo", str(tmp_path / "whisper"))]

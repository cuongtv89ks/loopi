import datetime as dt

import httpx
import pytest
from psycopg.types.json import Jsonb

from helpers import (
    DEFAULT_CAPTION,
    FakeOllama,
    fetch_all,
    fetch_one,
    insert_chapter,
    insert_item,
    insert_keyframe,
    insert_moment,
    make_ctx,
    paired_device,
)
from loopi_brain.app import create_app
from loopi_brain.pipeline import dispatch, jobs, moments, recap
from loopi_brain.pipeline.status import fmt_duration

HX = {"HX-Request": "true"}
DAY = "2026-10-14"
UTC_9AM_SEOUL = dt.datetime(2026, 10, 14, 0, 30, tzinfo=dt.UTC)  # 09:30 in Seoul


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def ollama():
    return FakeOllama()


@pytest.fixture
async def web(pool, pki_settings, jobs_open, ollama):
    app = create_app(pki_settings, pool=pool, http_client=ollama.client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


async def _fail_job(pool, job_id: int) -> None:
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 4 WHERE id = %s", (job_id,))


def test_durations_read_in_vietnamese():
    assert [fmt_duration(s) for s in (None, 4.6, 200, 7500)] == ["—", "5 giây", "3 phút 20 giây", "2 giờ 5 phút"]


async def test_timeline_shows_keyframes_speech_and_markers_by_local_hour(web, pool, pki_settings, dev):
    device_id = dev["device_id"]
    item_id = await insert_item(pool, device_id, kind="video", started_at=UTC_9AM_SEOUL)
    kf = await insert_keyframe(pool, pki_settings, item_id, device_id, UTC_9AM_SEOUL + dt.timedelta(seconds=10))
    waiting = await insert_keyframe(pool, pki_settings, item_id, device_id, UTC_9AM_SEOUL + dt.timedelta(seconds=50))
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption = %s WHERE id = %s", (Jsonb(DEFAULT_CAPTION), kf))
    await insert_moment(pool, device_id, kind="speech", ts=UTC_9AM_SEOUL + dt.timedelta(minutes=5),
                        text="Mai mình đi chợ <b>nhé</b>", lang="vi")
    marker = await insert_item(pool, device_id, kind="marker", started_at=UTC_9AM_SEOUL + dt.timedelta(minutes=7),
                               seconds=0, meta={"note": "chìa khoá"})
    assert await moments.index_marker(make_ctx(pool, pki_settings), marker)
    text = (await web.get("/hours", params={"day": DAY})).text
    assert "<h2>09:00</h2>" in text
    assert f'id="kf-{kf}"' in text and 'loading="lazy"' in text
    row = await fetch_one(pool, "SELECT path FROM keyframe WHERE id = %s", (kf,))
    assert f'href="/media/{row["path"]}"><img src="/thumb/{kf}.jpg"' in text  # a thumbnail, linked to the keyframe
    assert f"{DEFAULT_CAPTION['scene']} — {DEFAULT_CAPTION['activity']}" in text
    assert f'id="kf-{waiting}"' in text and "Đang chờ mô tả…" in text
    assert '<span class="lang">VI</span> Mai mình đi chợ &lt;b&gt;nhé&lt;/b&gt;' in text
    assert "09:37:00</time> ★ chìa khoá" in text
    assert "<details>" in text and f'id="item-{item_id}"' in text and "Đang phân tích…" in text


async def test_a_marker_without_a_note_reads_danh_dau_on_the_timeline(web, pool, pki_settings, dev):
    # The device uploads every button press as an empty JSON object.
    marker = await insert_item(pool, dev["device_id"], kind="marker", started_at=UTC_9AM_SEOUL, seconds=0, meta={})
    assert await moments.index_marker(make_ctx(pool, pki_settings), marker)
    text = (await web.get("/hours", params={"day": DAY})).text
    assert '<p class="marker"><time>09:30:00</time> ★ Đánh dấu</p>' in text
    assert moments.MARKER_TEXT not in text  # the English text is only what gets embedded


async def test_analysed_items_are_not_marked_pending(web, pool, dev):
    item_id = await insert_item(pool, dev["device_id"], kind="audio", started_at=UTC_9AM_SEOUL)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (item_id,))
    text = (await web.get("/hours", params={"day": DAY})).text
    assert f'id="item-{item_id}"' in text and "Đang phân tích…" not in text


async def test_a_keyframe_whose_caption_job_failed_is_not_waiting(web, pool, pki_settings, dev):
    device_id = dev["device_id"]
    item_id = await insert_item(pool, device_id, kind="video", started_at=UTC_9AM_SEOUL)
    broken = await insert_keyframe(pool, pki_settings, item_id, device_id, UTC_9AM_SEOUL + dt.timedelta(seconds=10))
    queued = await insert_keyframe(pool, pki_settings, item_id, device_id, UTC_9AM_SEOUL + dt.timedelta(seconds=50))
    await _fail_job(pool, await dispatch.defer_keyframe(pool, broken))
    assert await dispatch.defer_keyframe(pool, queued) is not None  # a caption job that is still queued
    pipeline = (await web.get("/pipeline")).text
    assert "Khung hình chờ mô tả: 1" in pipeline  # only the queued one
    timeline = (await web.get("/hours", params={"day": DAY})).text
    assert timeline.count("Lỗi mô tả") == 1 and timeline.count("Đang chờ mô tả…") == 1
    failed_figure = timeline.split(f'id="kf-{broken}"')[1].split("</figure>")[0]
    assert "Lỗi mô tả" in failed_figure and "Đang chờ mô tả…" not in failed_figure


async def test_pipeline_page_shows_backlog_latency_models_and_dead_letters(web, pool, pki_settings, dev, ollama):
    ollama.models.remove("qwen3:8b")
    device_id = dev["device_id"]
    now = dt.datetime.now(dt.UTC)
    for kind in ("video", "video", "audio"):
        await insert_item(pool, device_id, kind=kind, started_at=now - dt.timedelta(hours=2))
    done = await insert_item(pool, device_id, kind="video", started_at=now - dt.timedelta(minutes=11), seconds=60)
    kf = await insert_keyframe(pool, pki_settings, done, device_id, now - dt.timedelta(minutes=10, seconds=30))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (done,))
        await conn.execute("UPDATE keyframe SET caption = '{}', captioned_at = now() WHERE id = %s", (kf,))
    broken = await insert_item(pool, device_id, kind="marker", started_at=now - dt.timedelta(hours=3), seconds=0)
    job_id = await dispatch.defer_item(pool, broken)
    await _fail_job(pool, job_id)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_error = 'RuntimeError: bad marker' WHERE id = %s", (broken,))
        tick_id = await jobs.enqueue(conn, jobs.DISPATCH_PENDING, queue="light", queueing_lock="dispatch")
    await _fail_job(pool, tick_id)
    text = (await web.get("/pipeline")).text
    assert '<a href="/pipeline">Xử lý</a>' in text
    assert "Video chờ phân tích: 2" in text and "Âm thanh chờ nhận dạng: 1" in text
    assert "Đánh dấu chờ ghi nhận: 0" in text and "Khung hình chờ mô tả: 0" in text  # the failed one is not waiting
    assert "Ghi hình → mô tả: trung bình 10 phút" in text and "(1 mẫu)" in text
    assert "Ghi âm → transcript: chưa có dữ liệu" in text
    assert "Thiếu model: qwen3:8b" in text and "loopi-brain models pull" in text
    assert "Worker: <span class=\"err\">không thấy tín hiệu</span>" in text
    assert f'id="job-{job_id}"' in text and "Ghi nhận đánh dấu" in text and "RuntimeError: bad marker" in text
    assert f'hx-post="/pipeline/jobs/{job_id}/retry"' in text and "Xử lý lại" in text
    assert "Job lỗi (2)" in text and 'hx-post="/pipeline/jobs/retry-failed"' in text
    # a failed scheduler tick is listed, but without a retry button: the scheduler starts the next tick itself
    tick_row = text.split(f'id="job-{tick_id}"')[1].split("</tr>")[0]
    assert "Điều phối xử lý" in tick_row and "hx-post" not in tick_row
    broken_day = (now - dt.timedelta(hours=3)).astimezone(pki_settings.zone).date().isoformat()
    timeline = (await web.get("/hours", params={"day": broken_day})).text
    assert f'id="item-{broken}"' in timeline and "Lỗi phân tích" in timeline


async def test_pipeline_page_reports_a_stopped_model_server_and_a_live_worker(web, pool, ollama):
    ollama.down = True
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now())")
    text = (await web.get("/pipeline")).text
    assert "Không kết nối được Ollama (http://models:11434)" in text
    assert '<span class="online">đang chạy</span>' in text and "Không có job lỗi." in text


async def test_pipeline_page_reports_a_broken_providers_file(pool, pki_settings, jobs_open, tmp_path):
    bad = tmp_path / "providers.yaml"
    bad.write_text("tasks: {}\n")
    app = create_app(pki_settings.model_copy(update={"providers_file": bad}), pool=pool,
                     http_client=FakeOllama().client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        text = (await c.get("/pipeline")).text
    assert "Cấu hình provider lỗi" in text


async def test_retry_button_requeues_a_dead_lettered_job_once(web, pool, dev):
    item_id = await insert_item(pool, dev["device_id"], kind="marker", seconds=0)
    job_id = await dispatch.defer_item(pool, item_id)
    assert (await web.post(f"/pipeline/jobs/{job_id}/retry", headers=HX)).status_code == 409  # still todo
    await _fail_job(pool, job_id)
    assert (await web.post(f"/pipeline/jobs/{job_id}/retry")).status_code == 403
    r = await web.post(f"/pipeline/jobs/{job_id}/retry", headers=HX)
    assert r.status_code == 200 and f"Đã đưa job {job_id} vào hàng đợi xử lý lại." in r.text
    job = await fetch_one(pool, "SELECT status FROM procrastinate_jobs WHERE id = %s", (job_id,))
    assert job["status"] == "todo"
    assert (await web.post(f"/pipeline/jobs/{job_id}/retry", headers=HX)).status_code == 409
    assert (await web.post("/pipeline/jobs/999999/retry", headers=HX)).status_code == 409
    # the page carries the Vietnamese explanation the retry button shows for a 409
    assert "409: \"Job không còn ở trạng thái lỗi" in (await web.get("/pipeline")).text


@pytest.mark.parametrize(("task", "label", "note"), [
    (jobs.EMBED_MOMENTS, "Tạo embedding", "tự chạy lại mỗi phút"),
    (jobs.DISPATCH_PENDING, "Điều phối xử lý", "tự chạy lại mỗi phút"),
    (jobs.RETRY_STALLED, "Khôi phục job bị treo", "tự chạy lại theo lịch"),
    (jobs.REMOVE_OLD_JOBS, "Dọn job cũ", "tự chạy lại theo lịch"),
])
async def test_a_failed_automatic_job_says_when_it_runs_again_and_has_no_retry_button(web, pool, task, label, note):
    async with pool.connection() as conn:
        job_id = await jobs.enqueue(conn, task, queue="light", queueing_lock=f"test-{task}")
    await _fail_job(pool, job_id)
    text = (await web.get("/pipeline")).text
    row = text.split(f'id="job-{job_id}"')[1].split("</tr>")[0]
    assert label in row and note in row and "hx-post" not in row
    other = ({"tự chạy lại mỗi phút", "tự chạy lại theo lịch"} - {note}).pop()
    assert other not in row
    assert (await web.post(f"/pipeline/jobs/{job_id}/retry", headers=HX)).status_code == 409  # not by hand either


async def test_retry_all_requeues_every_dead_letter_but_the_embedding_run(web, pool, dev):
    job_ids = []
    for minute in range(2):
        item_id = await insert_item(pool, dev["device_id"], kind="marker", seconds=0,
                                    started_at=UTC_9AM_SEOUL + dt.timedelta(minutes=minute))
        job_ids.append(await dispatch.defer_item(pool, item_id))
    await dispatch.defer_embed(pool)
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 4")
    text = (await web.get("/pipeline")).text
    assert "Job lỗi (3)" in text and "tự chạy lại mỗi phút" in text
    assert (await web.post("/pipeline/jobs/retry-failed")).status_code == 403
    r = await web.post("/pipeline/jobs/retry-failed", headers=HX)
    assert r.status_code == 200 and "Đã đưa 2 job vào hàng đợi xử lý lại." in r.text
    statuses = {r["task_name"]: r["status"] for r in await fetch_all(pool, "SELECT task_name, status FROM procrastinate_jobs")}
    assert statuses == {"index_marker": "todo", "embed_moments": "failed"}


async def test_the_pipeline_page_shows_the_understanding_schedule(web, pool, pki_settings):
    text = (await web.get("/pipeline")).text
    card = " ".join(text.split('<h2>Hiểu ngày</h2>')[1].split("</div>")[0].split())  # whitespace-insensitive
    assert "Chia chương: mỗi giờ (phút thứ 5) · chương chờ tóm tắt: 0" in card
    assert "gần nhất: chưa có" in card and "<strong>đang tắt</strong>, không xoá gì" in card
    day = dt.date(2026, 10, 14)
    await insert_chapter(pool, day, UTC_9AM_SEOUL, UTC_9AM_SEOUL)  # no summary yet
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version, generated_at)"
                           " VALUES (%s, 'x', 'x', '2026-10-14T14:30:00Z')", (day,))
        await conn.execute("INSERT INTO setting (key, value) VALUES ('retention',"
                           " '{\"video_days\": 7, \"audio_days\": 7, \"enabled\": true}')")
    job_id = await recap.defer_recap(pool, dt.date(2026, 10, 13))  # deferred like the planner does (lock, priority)
    await _fail_job(pool, job_id)
    text = (await web.get("/pipeline")).text
    card = " ".join(text.split('<h2>Hiểu ngày</h2>')[1].split("</div>")[0].split())
    assert "chương chờ tóm tắt: 1" in card
    assert '<a href="/?day=2026-10-14">2026-10-14</a> (viết lúc 14/10 23:30)' in card
    assert "1 recap lỗi (xem Job lỗi)" in card and "đang bật, giữ video thô 7 ngày, audio thô 7 ngày" in card

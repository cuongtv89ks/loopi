import asyncio
import datetime as dt
import logging

import httpx
import pydantic
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
    jobs_by_lock,
    new_ulid,
    paired_device,
)
from loopi_brain import reprocess, store
from loopi_brain.app import create_app
from loopi_brain.pipeline import recap as recap_rules
from loopi_brain.pipeline.state import lock_day

HX = {"HX-Request": "true"}
DAY = dt.date(2026, 10, 14)
NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def ollama():
    return FakeOllama()


@pytest.fixture
async def web(pool, pki_settings, jobs_open, ollama):
    app = create_app(pki_settings, pool=pool, http_client=ollama.client(), llm_client=ollama.client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


async def test_retention_defaults_to_seven_days_and_is_stored(pool):
    assert await store.get_retention(pool) == store.Retention(video_days=7, audio_days=7, enabled=True)  # on (D32)
    await store.set_retention(pool, store.Retention(video_days=14, audio_days=3, enabled=True))
    assert await store.get_retention(pool) == store.Retention(video_days=14, audio_days=3, enabled=True)
    for bad in ({"video_days": 0}, {"audio_days": 3651}, {"video_days": "a week"}, {"keep": 1}):
        with pytest.raises(pydantic.ValidationError):
            store.Retention(**bad)


async def test_the_settings_page_shows_providers_privacy_health_and_retention(web, ollama, pool):
    ollama.models.remove("qwen3:8b")
    text = (await web.get("/settings")).text
    assert '<a href="/settings">Cài đặt</a>' in text
    assert "<td>Recap cuối ngày</td><td><code>ollama:qwen3:8b</code></td>" in text
    assert "<td>Nhận dạng giọng nói</td><td><code>local:faster-whisper/large-v3-turbo-int8</code></td>" in text
    assert "Ảnh gửi lên cloud: không" in text and "âm thanh gửi lên cloud: không" in text
    assert "Thiếu model: qwen3:8b." in text and "Asia/Seoul" in text
    assert '<label>Giữ video <input type="number" name="video_days" min="1" max="3650" required value="7"> ngày</label>' in text
    assert '<label>Giữ audio thô <input type="number" name="audio_days"' in text and "Giữ video thô" not in text
    assert ("Bản H.264 thô bị xoá ngay khi đã có MP4 và keyframe (D32); MP4 và audio giữ theo số ngày ở đây."
            ) in text.split('id="retention"')[1].split("</section>")[0]
    assert 'name="audio_days" min="1" max="3650" required value="7"' in text
    assert '<input type="checkbox" name="enabled" checked> Bật dọn dữ liệu cũ' in text  # on by default (D32)
    assert "Dọn dữ liệu cũ: <strong>đang bật</strong>." in text
    assert 'href="/pipeline"' in text and 'hx-post="/settings/reprocess"' in text
    await store.set_retention(pool, store.Retention(enabled=False))  # the owner turned it off
    text = (await web.get("/settings")).text
    assert '<input type="checkbox" name="enabled"> Bật dọn dữ liệu cũ' in text  # not checked
    assert "Dọn dữ liệu cũ: <strong>đang tắt</strong> (chưa xoá gì)" in text


async def test_a_broken_providers_file_is_shown(pool, pki_settings, jobs_open, tmp_path):
    bad = tmp_path / "providers.yaml"
    bad.write_text("tasks: {}\n")
    app = create_app(pki_settings.model_copy(update={"providers_file": bad}), pool=pool,
                     http_client=FakeOllama().client(), llm_client=FakeOllama().client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        assert "Cấu hình provider lỗi" in (await c.get("/settings")).text


async def test_the_retention_form_saves_validated_days(web, pool):
    assert (await web.post("/settings/retention", data={"video_days": "5", "audio_days": "9"})).status_code == 403
    r = await web.post("/settings/retention", data={"video_days": "5", "audio_days": "9"}, headers=HX)
    assert r.status_code == 200 and "giữ video thô 5 ngày, audio thô 9 ngày" in r.text and "đang tắt" in r.text
    assert await store.get_retention(pool) == store.Retention(video_days=5, audio_days=9, enabled=False)
    for bad in ("0", "3651", "abc", "-2"):
        r = await web.post("/settings/retention", data={"video_days": bad, "audio_days": "9"}, headers=HX)
        assert r.status_code == 422
    assert await store.get_retention(pool) == store.Retention(video_days=5, audio_days=9, enabled=False)
    assert "value=\"5\"" in (await web.get("/settings")).text
    r = await web.post("/settings/retention", data={"video_days": "5", "audio_days": "9", "enabled": "on"}, headers=HX)
    assert "Dọn dữ liệu cũ đang bật: chạy lúc 03:00" in r.text and (await store.get_retention(pool)).enabled
    assert 'name="enabled" checked' in (await web.get("/settings")).text


async def _captioned_keyframe(pool, settings, device_id, ts) -> tuple[str, str]:
    item = await insert_item(pool, device_id, kind="video", started_at=ts)
    kf = await insert_keyframe(pool, settings, item, device_id, ts)
    moment = await insert_moment(pool, device_id, kind="visual", ts=ts, text="old caption", lang="en")
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE keyframe SET caption = %s, caption_model = 'ollama:old', captioned_at = now(), caption_job_id = 7"
            " WHERE id = %s", (Jsonb(DEFAULT_CAPTION), kf))
        await conn.execute("UPDATE moment SET keyframe_id = %s, item_id = %s WHERE id = %s", (kf, item, moment))
    return kf, moment


async def test_reprocessing_captions_resets_the_days_keyframes(web, pool, pki_settings, dev):
    device_id = dev["device_id"]
    kf, moment = await _captioned_keyframe(pool, pki_settings, device_id, NINE)
    other, other_moment = await _captioned_keyframe(pool, pki_settings, device_id, NINE + dt.timedelta(days=1))
    await insert_chapter(pool, DAY, NINE, NINE)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, 'x', 'x')", (DAY,))
    r = await web.post("/settings/reprocess", data={"day": "2026-10-14", "what": ["captions"]}, headers=HX)
    assert r.status_code == 200
    assert "Ngày 14/10/2026 sẽ được xử lý lại (mô tả khung hình)" in r.text and "1 khung hình mô tả lại" in r.text
    assert "1 chương chia lại, recap sẽ được viết lại" in r.text
    row = await fetch_one(pool, "SELECT caption, caption_model, caption_job_id FROM keyframe WHERE id = %s", (kf,))
    assert row == {"caption": None, "caption_model": None, "caption_job_id": None}  # the dispatcher captions it again
    assert [m["id"] for m in await fetch_all(pool, "SELECT id FROM moment")] == [other_moment]
    assert (await fetch_one(pool, "SELECT caption_model FROM keyframe WHERE id = %s", (other,)))["caption_model"]
    assert (await fetch_one(pool, "SELECT closed FROM chapter"))["closed"] is False
    assert (await fetch_one(pool, "SELECT stale FROM day_recap"))["stale"] is True


async def test_reprocessing_transcripts_only_where_the_raw_audio_is_kept(pool, pki_settings, dev):
    device_id = dev["device_id"]
    kept = await insert_item(pool, device_id, kind="audio", started_at=NINE)
    gone = await insert_item(pool, device_id, kind="audio", started_at=NINE + dt.timedelta(minutes=1))
    for item in (kept, gone):
        segment = new_ulid()
        async with pool.connection() as conn:
            await conn.execute("UPDATE item SET analysis_state = 'done', analysis_job_id = 3 WHERE id = %s", (item,))
            await conn.execute(
                "INSERT INTO transcript_segment (id, item_id, device_id, start_ts, end_ts, lang, text, model_version)"
                " VALUES (%s, %s, %s, %s, %s, 'vi', 'xin chào', 'old')", (segment, item, device_id, NINE, NINE))
            await conn.execute(
                "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, item_id, transcript_segment_id,"
                " model_version) VALUES (%s, %s, %s, %s, 'speech', 'xin chào', %s, %s, 'old')",
                (new_ulid(), device_id, NINE, NINE, item, segment))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE id = %s", (gone,))
    result = await reprocess.reprocess_day(pool, DAY, pki_settings.zone, {"transcripts"})
    assert result == reprocess.ReprocessResult(audio_items=1, expired_audio=1)
    rows = await fetch_all(pool, "SELECT id, analysis_state, analysis_job_id FROM item ORDER BY started_at")
    assert rows == [{"id": kept, "analysis_state": "pending", "analysis_job_id": None},
                    {"id": gone, "analysis_state": "done", "analysis_job_id": 3}]
    assert [r["item_id"] for r in await fetch_all(pool, "SELECT item_id FROM moment")] == [gone]


async def test_reprocessing_chapters_forces_new_summaries_now(pool, pki_settings, dev, jobs_open):
    await insert_moment(pool, dev["device_id"], ts=NINE)
    await insert_chapter(pool, DAY, NINE, NINE, title="Cũ", summary="Tóm tắt cũ.")
    result = await reprocess.reprocess_day(pool, DAY, pki_settings.zone, {"chapters"})
    assert result.chapters == 1
    row = await fetch_one(pool, "SELECT closed, summary_signature, title FROM chapter")
    assert row == {"closed": False, "summary_signature": None, "title": "Cũ"}  # shown until the new one is written
    assert [j["task_name"] for j in (await jobs_by_lock(pool))["chapters:2026-10-14"]] == ["build_chapters"]


async def test_reprocessing_the_recap_marks_it_stale_and_asks_now(pool, pki_settings, jobs_open):
    await insert_chapter(pool, DAY, NINE, NINE)
    failed = await recap_rules.defer_recap(pool, DAY)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, 'x', 'x')", (DAY,))
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 4 WHERE id = %s", (failed,))
        await conn.execute("INSERT INTO day_job_error (task, day, error) VALUES ('write_recap', %s, 'boom')", (DAY,))
    result = await reprocess.reprocess_day(pool, DAY, pki_settings.zone, {"recap"})
    assert result == reprocess.ReprocessResult(recap=True)  # chapters are not touched
    assert (await fetch_one(pool, "SELECT closed FROM chapter"))["closed"] is True
    # Like "Tạo lại recap": the failed run and its error go (no stale banner, the planner is not kept away).
    [job] = (await jobs_by_lock(pool))["recap:2026-10-14"]
    assert job["id"] != failed and job["status"] == "todo"
    assert await fetch_all(pool, "SELECT * FROM day_job_error") == []


async def test_reprocessing_needs_a_day_and_a_choice(web, pool, pki_settings):
    assert (await web.post("/settings/reprocess", data={"day": "2026-10-14"}, headers=HX)).status_code == 422
    assert (await web.post("/settings/reprocess", data={"day": "2026-10-14", "what": ["vlog"]},
                           headers=HX)).status_code == 422
    assert (await web.post("/settings/reprocess", data={"day": "hôm qua", "what": ["recap"]},
                           headers=HX)).status_code == 422
    assert (await web.post("/settings/reprocess", data={"day": "2026-10-14", "what": ["recap"]})).status_code == 403
    with pytest.raises(ValueError):
        await reprocess.reprocess_day(pool, DAY, pki_settings.zone, set())


async def test_reprocessing_waits_for_the_days_lock_like_every_other_writer(pool, pki_settings, dev):
    kf, _ = await _captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
    zone = pki_settings.zone
    async with pool.connection() as other, other.transaction():
        await lock_day(other, DAY + dt.timedelta(days=1))  # another day's lock does not matter
        assert (await asyncio.wait_for(reprocess.reprocess_day(pool, DAY, zone, {"chapters"}), 10)).chapters == 0
    async with pool.connection() as holder, holder.transaction():
        await lock_day(holder, DAY)  # a chapter build, a recap write or a forget is working on the day
        task = asyncio.create_task(reprocess.reprocess_day(pool, DAY, zone, {"captions"}))
        await asyncio.sleep(0.5)
        assert not task.done()
        assert (await fetch_one(pool, "SELECT caption_model FROM keyframe WHERE id = %s", (kf,)))["caption_model"]
    assert (await asyncio.wait_for(task, 10)).keyframes == 1
    assert (await fetch_one(pool, "SELECT caption_model FROM keyframe WHERE id = %s", (kf,)))["caption_model"] is None


@pytest.mark.parametrize("day", ["0001-01-01", "9999-12-31"])
async def test_a_day_the_calendar_cannot_hold_is_a_422(web, pool, pki_settings, day):
    r = await web.post("/settings/reprocess", data={"day": day, "what": ["recap"]}, headers=HX)
    assert r.status_code == 422
    with pytest.raises(ValueError):
        await reprocess.reprocess_day(pool, dt.date.fromisoformat(day), pki_settings.zone, {"recap"})


async def test_a_failing_recap_request_is_logged_and_the_reset_stands(web, pool, monkeypatch, caplog):
    await insert_chapter(pool, DAY, NINE, NINE)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, 'x', 'x')", (DAY,))

    async def queue_down(pool, day):
        raise RuntimeError("queue down")

    monkeypatch.setattr(recap_rules, "defer_recap", queue_down)
    with caplog.at_level(logging.ERROR, logger="loopi_brain.pipeline.recap"):
        r = await web.post("/settings/reprocess", data={"day": "2026-10-14", "what": ["recap"]}, headers=HX)
    assert r.status_code == 200 and "recap sẽ được viết lại" in r.text
    assert (await fetch_one(pool, "SELECT stale FROM day_recap"))["stale"] is True  # the planner writes it anyway
    assert "could not ask for the recap of 2026-10-14" in caplog.text and "queue down" in caplog.text


TASK_LINES = (
    "  caption: {provider: ollama, model: m1%s}\n  asr: {provider: local, model: faster-whisper/large-v3-turbo-int8}\n"
    "  embed: {provider: ollama, model: m3}\n  chapter_summary: {provider: ollama, model: m4}\n"
    "  recap: {provider: ollama, model: m5}\n  chat: {provider: ollama, model: m6}\n"
)


async def _providers_page(pool, settings, tmp_path, yaml_text: str) -> str:
    path = tmp_path / "providers.yaml"
    path.write_text(yaml_text)
    app = create_app(settings.model_copy(update={"providers_file": path}), pool=pool,
                     http_client=FakeOllama().client(), llm_client=FakeOllama().client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        return (await c.get("/settings")).text


async def test_a_broken_providers_file_shows_where_and_why_but_never_its_values(
    pool, pki_settings, jobs_open, tmp_path
):
    secret = "sk-ant-SECRET"
    unknown_key = "tasks:\n" + TASK_LINES % f", api_key: {secret}"
    text = await _providers_page(pool, pki_settings, tmp_path, unknown_key)
    assert "Cấu hình provider lỗi" in text and "tasks.caption.api_key" in text
    assert "Extra inputs are not permitted" in text
    assert secret not in text and "input_value" not in text
    wrong_type = f"privacy: {secret}\ntasks:\n" + TASK_LINES % ""
    text = await _providers_page(pool, pki_settings, tmp_path, wrong_type)
    assert "Cấu hình provider lỗi" in text and "privacy" in text and secret not in text
    syntax = f"api_key: {secret}\ntasks: [unclosed\n"
    text = await _providers_page(pool, pki_settings, tmp_path, syntax)
    assert "Cấu hình provider lỗi" in text and "line" in text and secret not in text

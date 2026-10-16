import datetime as dt

import httpx
import pytest
from PIL import Image

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
    paired_device,
    write_jpeg,
)
from loopi_brain.app import create_app
from loopi_brain.days import local_now

HX = {"HX-Request": "true"}
DAY = dt.date(2026, 10, 14)
NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
RECAP = "## Diễn biến chính\n- Sáng **đọc sách**\n- <script>alert(1)</script>\n\n## Khoảnh khắc đáng nhớ\nKhông có."


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
async def web(pool, pki_settings, jobs_open):
    app = create_app(pki_settings, pool=pool, http_client=FakeOllama().client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


async def _day(pool, settings, device_id) -> dict:
    video = await insert_item(pool, device_id, kind="video", started_at=NINE)
    frames = []
    for n in range(8):
        kf = await insert_keyframe(pool, settings, video, device_id, NINE + dt.timedelta(minutes=n))
        frames.append(await _visual(pool, device_id, kf, NINE + dt.timedelta(minutes=n), f"Scene {n} <b>"))
    audio = await insert_item(pool, device_id, kind="audio", started_at=NINE + dt.timedelta(minutes=10))
    speech = await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=10, seconds=42),
                                 text="Mai mình đi chợ <nhé>", lang="vi", item_id=audio)
    note = await insert_item(pool, device_id, kind="marker", started_at=NINE + dt.timedelta(minutes=20), seconds=0,
                             meta={"note": "chìa khoá"})
    marker = await insert_moment(pool, device_id, kind="marker", ts=NINE + dt.timedelta(minutes=20), item_id=note)
    first = await insert_chapter(pool, DAY, NINE, NINE + dt.timedelta(minutes=12), title="Đọc sách buổi sáng",
                                 summary="Một người đọc sách. Sau đó hẹn đi chợ.")
    second = await insert_chapter(pool, DAY, NINE + dt.timedelta(minutes=20), NINE + dt.timedelta(minutes=20))
    late = await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(hours=2), text="Mới nói xong",
                               lang="vi")
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, %s, 'ollama:qwen3:8b')",
                           (DAY, RECAP))
        await conn.execute(
            "INSERT INTO commitment (id, day, text, source_moment_id, model_version)"
            " VALUES ('C1', %s, 'Đi chợ', %s, 'ollama:qwen3:8b')", (DAY, speech))
    return {"frames": frames, "speech": speech, "audio": audio, "marker": marker, "first": first,
            "second": second, "late": late}


async def _visual(pool, device_id, keyframe_id, ts, text) -> str:
    moment_id = await insert_moment(pool, device_id, kind="visual", ts=ts, text=text, lang="en")
    async with pool.connection() as conn:
        await conn.execute("UPDATE moment SET keyframe_id = %s WHERE id = %s", (keyframe_id, moment_id))
    return moment_id


async def test_a_day_shows_its_recap_commitments_and_chapters_in_order(web, pool, pki_settings, dev):
    ids = await _day(pool, pki_settings, dev["device_id"])
    text = (await web.get("/", params={"day": "2026-10-14"})).text
    assert "<h1>Thứ Tư, 14/10/2026</h1>" in text and 'href="/hours?day=2026-10-14"' in text
    assert 'href="/?day=2026-10-13"' in text and 'href="/?day=2026-10-15"' in text
    # The recap: rendered markdown, with the model's text escaped.
    assert "<h3>Diễn biến chính</h3>" in text and "<li>Sáng <strong>đọc sách</strong></li>" in text
    assert "&lt;script&gt;" in text and "<script>alert" not in text
    assert 'hx-post="/day/2026-10-14/recap"' in text
    # The commitment, linked to the line it came from, with its buttons.
    assert f'href="/?day=2026-10-14#m-{ids["speech"]}">09:10</a>' in text
    assert 'hx-post="/commitments/C1/done"' in text and "Chưa xong" in text
    # Chapters in order: time range, title, summary, six thumbnails then the rest folded, speech with its audio.
    assert text.index("Đọc sách buổi sáng") < text.index("Đang tóm tắt…") < text.index("Chưa chia chương (1 khoảnh khắc)")
    assert "<time>09:00–09:12</time> Đọc sách buổi sáng" in text and "Một người đọc sách. Sau đó hẹn đi chợ." in text
    first = text.split(f'id="ch-{ids["first"]}"')[1].split("</section>")[0]
    assert first.count('src="/thumb/') == 8 and "Tất cả khung hình (8)" in first
    assert f'id="m-{ids["frames"][7]}"' in first and "Scene 7 &lt;b&gt;" in first
    assert f'<audio controls preload="none" src="/audio/{ids["audio"]}.opus#t=42"></audio>' in first
    assert "Mai mình đi chợ &lt;nhé&gt;" in first
    second = text.split(f'id="ch-{ids["second"]}"')[1].split("</section>")[0]
    assert f'<p class="marker" id="m-{ids["marker"]}"><time>09:20:00</time> ★ chìa khoá</p>' in second
    assert f'id="m-{ids["late"]}"' in text.split(f'id="loose-{ids["late"]}"')[1]


async def test_moments_outside_chapters_are_shown_in_time_order(web, pool, dev):
    early = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, text="Sớm", lang="vi")
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE + dt.timedelta(hours=1), text="Giữa", lang="vi")
    late = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE + dt.timedelta(hours=3), text="Muộn",
                               lang="vi")
    chapter = await insert_chapter(pool, DAY, NINE + dt.timedelta(hours=1), NINE + dt.timedelta(hours=1), title="Trưa")
    text = (await web.get("/", params={"day": "2026-10-14"})).text
    assert text.index(f'id="loose-{early}"') < text.index(f'id="ch-{chapter}"') < text.index(f'id="loose-{late}"')
    assert "<time>09:00</time> Chưa chia chương (1 khoảnh khắc)" in text


async def test_today_also_lists_the_commitments_left_open_on_earlier_days(web, pool, pki_settings):
    today = local_now(pki_settings.zone).date()
    async with pool.connection() as conn:
        for cid, days_ago, status in (("C1", 2, "open"), ("C2", 3, "done"), ("C3", 8, "open"), ("C4", 0, "open")):
            await conn.execute("INSERT INTO commitment (id, day, text, status, model_version)"
                               " VALUES (%s, %s, %s, %s, 'x')", (cid, today - dt.timedelta(days=days_ago),
                                                                 f"việc {cid}", status))
    text = (await web.get("/")).text
    earlier = text.split('id="earlier-commitments"')[1].split("</section>")[0]
    assert 'id="c-C1"' in earlier and f"{today - dt.timedelta(days=2):%d/%m}" in earlier
    assert 'id="c-C2"' not in text and 'id="c-C3"' not in text  # done, or more than 7 days old
    assert 'id="c-C4"' in text.split('id="commitments"')[1].split("</section>")[0]  # today's own list
    other_day = (await web.get("/", params={"day": (today - dt.timedelta(days=1)).isoformat()})).text
    assert "earlier-commitments" not in other_day


async def test_a_failed_recap_is_shown_on_its_day(web, pool, pki_settings, dev, jobs_open):
    from loopi_brain.pipeline import recap

    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, text="x", lang="vi")
    job_id = await recap.defer_recap(pool, DAY)
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 4 WHERE id = %s", (job_id,))
        await conn.execute("INSERT INTO day_job_error (task, day, error) VALUES ('write_recap', %s, "
                           "'ProviderError: ollama /api/chat: ReadTimeout')", (DAY,))
    text = (await web.get("/", params={"day": "2026-10-14"})).text
    assert "Viết recap lỗi: ProviderError: ollama /api/chat: ReadTimeout." in text and 'href="/pipeline"' in text


async def test_a_day_without_data_says_so(web):
    text = (await web.get("/", params={"day": "2026-10-01"})).text
    assert "Không có dữ liệu cho ngày này." in text and "Tạo lại recap" not in text


async def test_a_day_with_moments_but_nothing_written_yet(web, pool, dev):
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, text="Chào buổi sáng", lang="vi")
    text = (await web.get("/", params={"day": "2026-10-14"})).text
    assert "Chưa có recap." in text and "Tạo lại recap" in text and "Chưa chia chương (1 khoảnh khắc)" in text


async def test_the_landing_page_is_today(web, pki_settings):
    today = local_now(pki_settings.zone).date()
    text = (await web.get("/")).text
    assert "<h1>Hôm nay · " in text and f"{today:%d/%m/%Y}</h1>" in text
    assert (await web.get("/", params={"day": "yesterday"})).status_code == 422


async def test_audio_that_retention_removed_says_so(web, pool, pki_settings, dev):
    ids = await _day(pool, pki_settings, dev["device_id"])
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE id = %s", (ids["audio"],))
    day = (await web.get("/", params={"day": "2026-10-14"})).text
    assert f'/audio/{ids["audio"]}.opus' not in day and "(âm thanh đã hết hạn)" in day
    hours = (await web.get("/hours", params={"day": "2026-10-14"})).text
    row = hours.split(f'id="item-{ids["audio"]}"')[1].split("</li>")[0]
    assert "Đã hết hạn" in row and "<audio" not in row


async def test_rewriting_the_recap_marks_it_stale_and_asks_for_a_new_one(web, pool, pki_settings, dev):
    from loopi_brain.pipeline import recap

    await _day(pool, pki_settings, dev["device_id"])
    # A failed run of the day (dead letter plus its error text) blocks the planner and shows a banner.
    failed = await recap.defer_recap(pool, DAY)
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 4 WHERE id = %s", (failed,))
        await conn.execute("INSERT INTO day_job_error (task, day, error) VALUES ('write_recap', %s, 'ProviderError: x')",
                           (DAY,))
    router = make_ctx(pool, pki_settings).router
    morning = dt.datetime(2026, 10, 15, 8, 0, tzinfo=pki_settings.zone)
    async with pool.connection() as conn:
        await conn.execute("UPDATE day_recap SET stale = true")  # e.g. reprocessing changed the day
    assert await recap.plan_recaps(pool, router, pki_settings.zone, morning) == []  # blocked by the failed job
    assert "Viết recap lỗi: ProviderError: x." in (await web.get("/", params={"day": "2026-10-14"})).text

    assert (await web.post("/day/2026-10-14/recap")).status_code == 403
    assert len(await fetch_all(pool, "SELECT id FROM procrastinate_jobs WHERE status = 'failed'")) == 1  # untouched
    r = await web.post("/day/2026-10-14/recap", headers=HX)
    assert r.status_code == 200 and "Đã yêu cầu viết lại recap" in r.text
    assert (await fetch_one(pool, "SELECT stale FROM day_recap"))["stale"] is True
    # The failed job and its error are gone; one new request waits behind live work (defer_recap's priority/lock).
    assert await fetch_all(pool, "SELECT id FROM procrastinate_jobs WHERE status = 'failed'") == []
    assert await fetch_all(pool, "SELECT * FROM day_job_error") == []
    [job] = (await jobs_by_lock(pool))["recap:2026-10-14"]
    assert job["id"] != failed and job["task_name"] == "write_recap" and job["status"] == "todo"
    assert job["priority"] == -10 and job["lock"] == "llm"
    page = (await web.get("/", params={"day": "2026-10-14"})).text
    assert "Recap này đã cũ" in page and "Viết recap lỗi" not in page
    # Once that request has run, the planner can ask for the day again (a failed job no longer blocks it).
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'succeeded' WHERE id = %s", (job["id"],))
    assert await recap.plan_recaps(pool, router, pki_settings.zone, morning) == [DAY]


async def test_commitment_buttons_change_its_status(web, pool, pki_settings, dev):
    await _day(pool, pki_settings, dev["device_id"])
    assert (await web.post("/commitments/C1/done")).status_code == 403
    r = await web.post("/commitments/C1/done", headers=HX)
    assert r.status_code == 200 and 'class="commitment done"' in r.text and "Đã xong" in r.text
    assert 'hx-post="/commitments/C1/open"' in r.text and 'hx-post="/commitments/C1/done"' not in r.text
    assert (await fetch_one(pool, "SELECT status FROM commitment"))["status"] == "done"
    assert (await web.post("/commitments/C1/later", headers=HX)).status_code == 404
    assert (await web.post("/commitments/NOPE/done", headers=HX)).status_code == 404


async def test_thumbnails_are_small_cached_and_never_leave_the_media_root(web, pool, pki_settings, dev):
    device_id = dev["device_id"]
    video = await insert_item(pool, device_id, kind="video", started_at=NINE)
    kf = await insert_keyframe(pool, pki_settings, video, device_id, NINE)
    r = await web.get(f"/thumb/{kf}.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    row = await fetch_one(pool, "SELECT path FROM keyframe WHERE id = %s", (kf,))
    thumb = pki_settings.media_root / row["path"].replace("keyframes/", "thumbs/", 1)
    with Image.open(thumb) as img:
        assert img.size == (320, 180)
    made = thumb.stat().st_mtime_ns
    assert (await web.get(f"/thumb/{kf}.jpg")).status_code == 200 and thumb.stat().st_mtime_ns == made
    assert (await web.get("/thumb/NOPE.jpg")).status_code == 404
    (pki_settings.media_root / row["path"]).unlink()
    thumb.unlink()
    assert (await web.get(f"/thumb/{kf}.jpg")).status_code == 404  # the keyframe file is gone
    write_jpeg(pki_settings.media_root.parent / "secret.jpg")
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET path = '../secret.jpg' WHERE id = %s", (kf,))
    assert (await web.get(f"/thumb/{kf}.jpg")).status_code == 404
    assert not (pki_settings.media_root.parent / "thumbs").exists()


async def test_a_thumbnail_made_after_its_keyframe_was_forgotten_is_removed_and_not_served(
    web, pool, pki_settings, dev, database_url, monkeypatch
):
    import psycopg

    from loopi_brain import thumbs

    device_id = dev["device_id"]
    video = await insert_item(pool, device_id, kind="video", started_at=NINE)
    kf = await insert_keyframe(pool, pki_settings, video, device_id, NINE)
    row = await fetch_one(pool, "SELECT path FROM keyframe WHERE id = %s", (kf,))
    thumb = pki_settings.media_root / row["path"].replace("keyframes/", "thumbs/", 1)
    real = thumbs.make_thumb

    def made_then_forgotten(src, dst):  # runs in a worker thread
        real(src, dst)
        with psycopg.connect(database_url, autocommit=True) as conn:  # the forget commits meanwhile
            conn.execute("DELETE FROM keyframe WHERE id = %s", (kf,))

    monkeypatch.setattr(thumbs, "make_thumb", made_then_forgotten)
    r = await web.get(f"/thumb/{kf}.jpg")
    assert r.status_code == 404 and r.headers["content-type"] != "image/jpeg"
    assert not thumb.exists()

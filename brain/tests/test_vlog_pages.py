import datetime as dt

import httpx
import pytest
from psycopg.types.json import Jsonb

from helpers import FakeOllama, fetch_all
from loopi_brain.app import create_app
from loopi_brain.pipeline import vlogs

DAY = dt.date(2026, 10, 14)
HX = {"HX-Request": "true"}


@pytest.fixture
async def web(pool, pki_settings, jobs_open):
    app = create_app(pki_settings, pool=pool, http_client=FakeOllama().client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


async def _rendered(pool, settings, *, title="Một ngày <b>ở nhà</b>", path=None):
    rel = path or vlogs.vlog_rel(DAY)
    if path is None:
        mp4 = settings.media_root / rel
        mp4.parent.mkdir(parents=True, exist_ok=True)
        mp4.write_bytes(bytes(range(256)) * 4)
        mp4.with_suffix(".vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nBữa sáng\n\n", encoding="utf-8")
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO vlog (day, state, script, model_version, source, path, duration_s, generated_at)"
            " VALUES (%s, 'rendered', %s, 'ollama:qwen3:8b', 'video', %s, 84.0, now())",
            (DAY, Jsonb({"title": title, "highlights": []}), rel),
        )


async def test_the_list_shows_each_vlog_with_its_escaped_title_and_length(web, pool, pki_settings):
    await _rendered(pool, pki_settings)
    text = (await web.get("/vlog")).text
    assert '<a href="/vlog/2026-10-14">' in text and "Một ngày &lt;b&gt;ở nhà&lt;/b&gt;" in text and "1:24" in text
    assert '<a href="/vlog">Vlog</a>' in text  # the menu


async def test_the_day_page_plays_the_vlog_with_its_subtitles_and_offers_the_download(web, pool, pki_settings):
    await _rendered(pool, pki_settings)
    text = (await web.get("/vlog/2026-10-14")).text
    assert '<video controls preload="metadata" src="/vlog/2026-10-14.mp4"' in text
    assert '<track kind="subtitles" src="/vlog/2026-10-14.vtt" srclang="vi" label="Phụ đề" default>' in text
    assert 'href="/vlog/2026-10-14.mp4?download=1"' in text and "Dựng lại vlog" in text


async def test_the_file_seeks_downloads_and_has_web_subtitles(web, pool, pki_settings):
    await _rendered(pool, pki_settings)
    whole = await web.get("/vlog/2026-10-14.mp4")
    assert whole.status_code == 200 and whole.headers["content-type"] == "video/mp4" and len(whole.content) == 1024
    part = await web.get("/vlog/2026-10-14.mp4", headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and part.content == bytes(range(10))
    download = await web.get("/vlog/2026-10-14.mp4", params={"download": "1"})
    assert download.headers["content-disposition"] == 'attachment; filename="loopi-vlog-2026-10-14.mp4"'
    vtt = await web.get("/vlog/2026-10-14.vtt")
    assert vtt.headers["content-type"].startswith("text/vtt") and "Bữa sáng" in vtt.text


async def test_missing_unknown_and_escaping_files_answer_404(web, pool, pki_settings):
    assert (await web.get("/vlog/2026-10-14.mp4")).status_code == 404
    assert (await web.get("/vlog/2026-10-14.vtt")).status_code == 404
    await _rendered(pool, pki_settings, path="../../etc/passwd")
    assert (await web.get("/vlog/2026-10-14.mp4")).status_code == 404


async def test_a_day_without_activity_says_so(web, pool):
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO vlog (day, state) VALUES (%s, 'empty')", (DAY,))
    assert "Không có hoạt động trong ngày này." in (await web.get("/vlog/2026-10-14")).text


async def test_a_failed_vlog_shows_its_error_and_a_working_one_says_so(web, pool):
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO vlog (day, state, error) VALUES (%s, 'scripted', 'render failed at assemble"
                           " (ffmpeg exit 234)')", (DAY,))
    text = (await web.get("/vlog/2026-10-14")).text
    assert "Dựng vlog lỗi: render failed at assemble (ffmpeg exit 234)" in text and "Đang dựng vlog" in text


async def test_rebuild_needs_htmx_and_asks_for_a_script(web, pool):
    assert (await web.post("/vlog/2026-10-14/rebuild")).status_code == 403
    r = await web.post("/vlog/2026-10-14/rebuild", headers=HX)
    assert r.status_code == 200 and "Đã yêu cầu dựng lại vlog" in r.text
    [job] = await fetch_all(pool, "SELECT task_name, queueing_lock FROM procrastinate_jobs")
    assert job == {"task_name": "write_vlog_script", "queueing_lock": "vlog:2026-10-14"}
    page = (await web.get("/vlog/2026-10-14")).text
    assert '<form method="post" hx-post="/vlog/2026-10-14/rebuild"' in page


async def test_the_day_page_has_the_vlog_card(web, pool, pki_settings):
    await _rendered(pool, pki_settings)
    text = (await web.get("/", params={"day": "2026-10-14"})).text
    assert 'id="vlog"' in text and 'src="/vlog/2026-10-14.mp4"' in text


async def test_a_day_without_a_vlog_invites_one(web):
    text = (await web.get("/vlog/2026-10-14")).text
    assert "Chưa có vlog. Vlog được dựng sau recap mỗi tối." in text

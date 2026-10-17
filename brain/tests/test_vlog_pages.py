import datetime as dt
from contextlib import asynccontextmanager

import httpx
import pytest
from psycopg.types.json import Jsonb

from helpers import FakeOllama, fetch_all
from loopi_brain.app import create_app
from loopi_brain.days import local_now
from loopi_brain.pipeline import vlogs
from loopi_brain.web.vlog_routes import vlog_view, vlog_views

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


async def test_a_failed_vlog_shows_its_error_and_does_not_say_it_is_building(web, pool):
    # The last attempt failed: no job is alive and the planner leaves the day alone, so "Đang dựng" would be false.
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO vlog (day, state, error) VALUES (%s, 'scripted', 'render failed at assemble"
                           " (ffmpeg exit 234)')", (DAY,))
    text = (await web.get("/vlog/2026-10-14")).text
    assert "Dựng vlog lỗi: render failed at assemble (ffmpeg exit 234)" in text and "Đang dựng" not in text
    row = [r.split("</tr>")[0] for r in (await web.get("/vlog")).text.split("<tr>")[2:]][0]
    assert "lỗi" in row and "đang dựng" not in row


async def test_a_vlog_says_it_is_building_only_while_a_job_of_its_day_is_alive(web, pool, jobs_open):
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO vlog (day, state) VALUES (%s, 'scripted')", (DAY,))
    assert "Đang dựng" not in (await web.get("/vlog/2026-10-14")).text  # a scripted row alone is not work
    job = await vlogs.defer_render(pool, DAY)
    assert "Đang dựng vlog…" in (await web.get("/vlog/2026-10-14")).text  # todo
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'doing' WHERE id = %s", (job,))
    assert "Đang dựng vlog…" in (await web.get("/vlog/2026-10-14")).text  # doing
    await _fail(pool, job)
    text = (await web.get("/vlog/2026-10-14")).text
    assert "Đang dựng" not in text and "Dựng vlog lỗi" in text  # failed: the failure, not the work


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


# --- Fix round 1 ---------------------------------------------------------------------------------------------------


async def _fail(pool, job_id, task=None, day=None, error=None):
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 4 WHERE id = %s", (job_id,))
        if error:
            await conn.execute("INSERT INTO day_job_error (task, day, error) VALUES (%s, %s, %s)", (task, day, error))


class _Counted:
    """A pool that records the statements run through it."""

    def __init__(self, pool):
        self._pool, self.statements = pool, []

    @asynccontextmanager
    async def connection(self):
        async with self._pool.connection() as conn:
            yield _CountedConnection(conn, self.statements)


class _CountedConnection:
    def __init__(self, conn, statements):
        self._conn, self._statements = conn, statements

    async def execute(self, query, params=None):
        self._statements.append(query)
        return await self._conn.execute(query, params)


async def test_the_files_are_revalidated_because_a_rebuild_replaces_them_under_the_same_url(web, pool, pki_settings):
    await _rendered(pool, pki_settings)
    for url, kwargs in (
        ("/vlog/2026-10-14.mp4", {}),
        ("/vlog/2026-10-14.mp4", {"headers": {"Range": "bytes=0-9"}}),
        ("/vlog/2026-10-14.mp4", {"params": {"download": "1"}}),
        ("/vlog/2026-10-14.vtt", {}),
    ):
        response = await web.get(url, **kwargs)
        assert response.status_code in (200, 206)
        assert response.headers["cache-control"] == "private, no-cache", (url, kwargs)


async def test_views_of_many_days_take_three_queries_and_tell_each_state(web, pool, pki_settings, jobs_open):
    rendered, empty, failed, waiting, failed_without_error, nothing = (DAY - dt.timedelta(days=n) for n in range(6))
    await _rendered(pool, pki_settings)  # DAY
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO vlog (day, state) VALUES (%s, 'empty')", (empty,))
    await _fail(
        pool, await vlogs.defer_render(pool, failed), "render_vlog", failed, "render failed at mix (ffmpeg exit 1)"
    )
    await vlogs.defer_script(pool, waiting)
    await _fail(pool, await vlogs.defer_script(pool, failed_without_error))
    days = [rendered, empty, failed, waiting, failed_without_error, nothing]

    counted = _Counted(pool)
    views = await vlog_views(counted, days, pki_settings.zone)

    assert len(counted.statements) == 3  # the rows, the failed jobs, the waiting jobs: however many days
    assert set(views) == {rendered, empty, failed, waiting, failed_without_error}  # no row and no job: no view
    assert views[rendered] == {
        "day": rendered, "state": "rendered", "title": "Một ngày <b>ở nhà</b>", "duration": "1:24",
        "at": views[rendered]["at"], "has_file": True, "working": False, "error": None, "source": "video",
        "model": "ollama:qwen3:8b",
    }
    assert views[rendered]["at"] is not None
    assert views[empty]["state"] == "empty" and not views[empty]["has_file"] and not views[empty]["working"]
    assert views[failed]["state"] is None and views[failed]["error"] == "render failed at mix (ffmpeg exit 1)"
    assert views[failed]["working"] is False and views[failed]["has_file"] is False
    assert views[waiting]["working"] is True and views[waiting]["error"] is None
    assert views[failed_without_error]["error"] == "lần dựng gần nhất bị lỗi"
    for day in days:  # one day on its own tells the same, and a day without anything is None
        assert await vlog_view(pool, day, pki_settings.zone) == views.get(day)
    assert await vlog_views(counted, [], pki_settings.zone) == {} and len(counted.statements) == 3  # asks nothing


async def test_the_list_shows_every_state_newest_first(web, pool, pki_settings, jobs_open):
    await _rendered(pool, pki_settings)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO vlog (day, state) VALUES (%s, 'empty')", (DAY - dt.timedelta(days=1),))
        await conn.execute("INSERT INTO vlog (day, state, error) VALUES (%s, 'scripted', 'render failed at mix"
                           " (ffmpeg exit 1)')", (DAY - dt.timedelta(days=2),))
        await conn.execute("INSERT INTO vlog (day, state) VALUES (%s, 'scripted')", (DAY - dt.timedelta(days=3),))
    await vlogs.defer_render(pool, DAY - dt.timedelta(days=3))
    text = (await web.get("/vlog")).text
    order = [text.index(f"/vlog/2026-10-{n}") for n in (14, 13, 12, 11)]
    assert order == sorted(order)
    rows = [row.split("</tr>")[0] for row in text.split("<tr>")[2:]]
    assert [("xong" in r, "không có hoạt động" in r, "lỗi" in r, "đang dựng" in r) for r in rows] == [
        (True, False, False, False), (False, True, False, False), (False, False, True, False),
        (False, False, False, True),
    ]


async def test_the_list_without_vlogs_invites_one(web):
    assert "Chưa có vlog nào. Vlog được dựng sau recap mỗi tối." in (await web.get("/vlog")).text


async def test_a_failed_job_shows_its_stored_error_escaped_or_says_the_last_run_failed(web, pool, jobs_open):
    error = "render failed at <b>mix</b> (ffmpeg exit 1)"
    await _fail(pool, await vlogs.defer_render(pool, DAY), "render_vlog", DAY, error)
    text = (await web.get("/vlog/2026-10-14")).text
    assert "Dựng vlog lỗi: render failed at &lt;b&gt;mix&lt;/b&gt; (ffmpeg exit 1)." in text
    assert "Chưa có vlog" in text and "Đang dựng" not in text  # nothing is building: only the failure
    other = DAY + dt.timedelta(days=1)
    await _fail(pool, await vlogs.defer_script(pool, other))
    assert "Dựng vlog lỗi: lần dựng gần nhất bị lỗi." in (await web.get("/vlog/2026-10-15")).text


async def test_a_missing_title_duration_or_time_never_prints_none(web, pool, pki_settings):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO vlog (day, state, path) VALUES (%s, 'rendered', %s)", (DAY, vlogs.vlog_rel(DAY))
        )
    for url in ("/vlog/2026-10-14", "/vlog", "/?day=2026-10-14"):
        assert "None" not in (await web.get(url)).text, url
    assert '<p><a href="/vlog/2026-10-14.mp4?download=1">Tải về</a></p>' in (await web.get("/vlog/2026-10-14")).text
    async with pool.connection() as conn:
        await conn.execute("UPDATE vlog SET script = %s, duration_s = 84", (Jsonb({"title": "Một ngày"}),))
    text = (await web.get("/vlog/2026-10-14")).text
    assert "<p>Một ngày · 1:24 · <a" in text and "None" not in text


async def test_while_the_first_build_runs_only_that_is_said(web, pool, jobs_open):
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO vlog (day, state) VALUES (%s, 'scripted')", (DAY,))
    await vlogs.defer_render(pool, DAY)
    text = (await web.get("/vlog/2026-10-14")).text
    assert "Đang dựng vlog…" in text and "Chưa có vlog" not in text
    other = DAY + dt.timedelta(days=1)  # a waiting script job and no row yet
    assert (await web.post("/vlog/2026-10-15/rebuild", headers=HX)).status_code == 200
    text = (await web.get(f"/vlog/{other}")).text
    assert "Đang dựng vlog…" in text and "Chưa có vlog" not in text


async def test_a_rebuild_of_a_vlog_that_has_a_file_keeps_the_player_and_says_it_rebuilds(web, pool, pki_settings,
                                                                                         jobs_open):
    await _rendered(pool, pki_settings)
    async with pool.connection() as conn:
        await conn.execute("UPDATE vlog SET stale = true")
    assert "Đang dựng" not in (await web.get("/vlog/2026-10-14")).text  # stale and waiting for the planner: no job yet
    await vlogs.defer_script(pool, DAY)
    text = (await web.get("/vlog/2026-10-14")).text
    assert 'src="/vlog/2026-10-14.mp4"' in text and "Đang dựng lại vlog…" in text
    assert "Một ngày &lt;b&gt;ở nhà&lt;/b&gt; · 1:24 · dựng lúc " in text  # model text, escaped
    assert "<b>ở nhà" not in text


async def test_a_day_that_has_not_come_cannot_be_rebuilt_and_has_no_button(web, pool, pki_settings):
    today = local_now(pki_settings.zone).date()
    tomorrow = today + dt.timedelta(days=1)
    r = await web.post(f"/vlog/{tomorrow}/rebuild", headers=HX)
    assert r.status_code == 200 and r.text == '<span class="err">Ngày này chưa tới.</span>'
    assert await fetch_all(pool, "SELECT id FROM procrastinate_jobs") == []
    assert await fetch_all(pool, "SELECT day FROM vlog") == []
    for url in (f"/vlog/{tomorrow}", f"/?day={tomorrow}"):
        text = (await web.get(url)).text
        assert 'id="vlog"' in text and "Dựng lại vlog" not in text and "hx-post" not in text.split('id="vlog"')[1]
    for day in (today, today - dt.timedelta(days=1)):  # today, and any day gone by, can be rebuilt
        assert "Dựng lại vlog" in (await web.get(f"/vlog/{day}")).text
        assert "Đã yêu cầu dựng lại vlog" in (await web.post(f"/vlog/{day}/rebuild", headers=HX)).text

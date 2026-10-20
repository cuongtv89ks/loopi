import datetime as dt

import pytest

from helpers import FakeOllama, fetch_all, fetch_one, insert_moment, make_ctx, paired_device
from loopi_brain import places, store
from loopi_brain.pipeline import chapters, segment
from loopi_brain.pipeline.segment import Point

NOW = dt.datetime(2026, 10, 21, 3, 0, tzinfo=dt.UTC)
NINE = dt.datetime(2026, 10, 20, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul on 10-20
HX = {"HX-Request": "true"}


def test_a_chapter_from_one_device_keeps_its_old_signature():
    one = [Point("a", "visual", NINE, NINE, device_id="pi", place="Bếp"),
           Point("b", "speech", NINE, NINE, device_id="pi", place="Bếp")]
    assert segment.signature(one) == segment.signature([Point("a", "visual", NINE, NINE), Point("b", "speech", NINE, NINE)])


def test_a_chapter_from_two_devices_signs_its_places():
    two = [Point("a", "visual", NINE, NINE, device_id="pi", place="Bếp"),
           Point("b", "speech", NINE, NINE, device_id="cam", place="Phòng khách")]
    renamed = [two[0], Point("b", "speech", NINE, NINE, device_id="cam", place="Phòng ngủ")]
    assert segment.signature(two) != segment.signature(renamed)


async def _day(pool, pki_settings):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await places.set_location(pool, pi["device_id"], "Bếp")
    await places.set_location(pool, cam["device_id"], "Phòng khách")
    for k in range(4):
        await insert_moment(pool, pi["device_id"], kind="speech", ts=NINE + dt.timedelta(minutes=k), text=f"nấu {k}")
        await insert_moment(pool, cam["device_id"], kind="visual", ts=NINE + dt.timedelta(minutes=k, seconds=30),
                            text=f"phòng {k}")
    ollama = FakeOllama()  # the chapter summary is a JSON answer (the default reply is a picture caption)
    ollama.reply = lambda body: {"title": "Buổi sáng", "summary": "Ở nhà. Mọi người làm việc."}
    ctx = make_ctx(pool, pki_settings, ollama=ollama)
    await chapters.build_chapters(ctx, NINE.date(), now=NOW)
    return pi, cam


async def test_renaming_a_place_re_signs_that_days_chapters_and_stales_its_recap(pool, pki_settings, jobs_open):
    pi, cam = await _day(pool, pki_settings)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version, generated_at) VALUES (%s, 'x', 't', now())",
                           (NINE.date(),))
    before = await fetch_all(pool, "SELECT id, signature, summary_signature FROM chapter")
    assert all(r["signature"] == r["summary_signature"] for r in before)
    await places.set_location(pool, cam["device_id"], "Phòng ngủ")
    assert await chapters.refresh_places(pool, cam["device_id"], pki_settings.zone, now=NOW) == [NINE.date()]
    after = await fetch_all(pool, "SELECT signature, summary_signature FROM chapter")
    assert any(r["signature"] != r["summary_signature"] for r in after)  # out of date: summarised again
    assert (await fetch_one(pool, "SELECT stale FROM day_recap"))["stale"] is True
    job = await fetch_one(pool, "SELECT task_name FROM procrastinate_jobs WHERE task_name = 'build_chapters'")
    assert job is not None


async def test_days_older_than_two_weeks_or_with_one_device_are_left_alone(pool, pki_settings, jobs_open):
    pi, cam = await _day(pool, pki_settings)
    later = NOW + dt.timedelta(days=chapters.RENAME_REFRESH_DAYS + 1)
    assert await chapters.refresh_places(pool, cam["device_id"], pki_settings.zone, now=later) == []
    alone = await paired_device(pool, pki_settings)  # the only device on its day
    await insert_moment(pool, alone["device_id"], kind="visual", ts=NINE - dt.timedelta(days=2), text="một mình")
    assert await chapters.refresh_places(pool, alone["device_id"], pki_settings.zone, now=NOW) == []


async def _recap(pool, day):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO day_recap (day, markdown, model_version, generated_at) VALUES (%s, 'x', 't', now())", (day,)
        )


async def _stale(pool, day):
    return (await fetch_one(pool, "SELECT stale FROM day_recap WHERE day = %s", (day,)))["stale"]


async def _whole_history(pool, monkeypatch):
    """The route uses the real clock: let the refresh reach the fixed test days."""
    monkeypatch.setattr(chapters, "RENAME_REFRESH_DAYS", 100_000)
    await store.set_retention(pool, store.Retention(video_days=store.MAX_DAYS))


async def test_the_rename_form_refreshes_the_summaries(client, pool, pki_settings, jobs_open, monkeypatch):
    await _whole_history(pool, monkeypatch)
    pi, cam = await _day(pool, pki_settings)
    r = await client.post(f"/devices/{cam['device_id']}/location", data={"location": "Phòng ngủ"}, headers=HX)
    assert r.status_code == 200
    rows = await fetch_all(pool, "SELECT signature, summary_signature FROM chapter")
    assert any(r["signature"] != r["summary_signature"] for r in rows)


async def test_a_failing_refresh_never_fails_the_rename(client, pool, pki_settings, caplog, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(chapters, "refresh_places", broken)
    cam = await paired_device(pool, pki_settings)
    r = await client.post(f"/devices/{cam['device_id']}/location", data={"location": "Phòng ngủ"}, headers=HX)
    assert r.status_code == 200
    saved = await fetch_one(pool, "SELECT location FROM device WHERE id = %s", (cam["device_id"],))
    assert saved["location"] == "Phòng ngủ"
    assert "could not refresh the summaries" in caplog.text


async def test_saving_an_unchanged_name_refreshes_nothing(client, pool, pki_settings, jobs_open, monkeypatch):
    await _whole_history(pool, monkeypatch)
    pi, cam = await _day(pool, pki_settings)
    await _recap(pool, NINE.date())
    builds = await fetch_all(pool, "SELECT id FROM procrastinate_jobs WHERE task_name = 'build_chapters'")
    r = await client.post(f"/devices/{cam['device_id']}/location", data={"location": " Phòng  khách "}, headers=HX)
    assert r.status_code == 200 and "Phòng khách" in r.text
    assert await _stale(pool, NINE.date()) is False  # "Lưu" without a change: no recap, no vlog written again
    assert await fetch_all(pool, "SELECT id FROM procrastinate_jobs WHERE task_name = 'build_chapters'") == builds


async def _two_devices_on(pool, zone, pi, cam, ts):
    await insert_moment(pool, pi["device_id"], kind="speech", ts=ts, text="nấu")
    await insert_moment(pool, cam["device_id"], kind="visual", ts=ts + dt.timedelta(seconds=30), text="phòng")
    day = ts.astimezone(zone).date()
    await _recap(pool, day)
    return day


@pytest.mark.parametrize(("video_days", "kept", "gone"), [(None, 3, 10), (30, 10, 20)])
async def test_a_rename_refreshes_only_the_days_whose_video_is_kept_and_at_most_two_weeks(
        pool, pki_settings, jobs_open, video_days, kept, gone):
    if video_days is not None:
        await store.set_retention(pool, store.Retention(video_days=video_days))
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    recent = await _two_devices_on(pool, pki_settings.zone, pi, cam, NOW - dt.timedelta(days=kept))
    # its video is gone (7 days), or it is older than two weeks (30 days of video)
    old = await _two_devices_on(pool, pki_settings.zone, pi, cam, NOW - dt.timedelta(days=gone))
    await places.set_location(pool, cam["device_id"], "Phòng ngủ")
    assert await chapters.refresh_places(pool, cam["device_id"], pki_settings.zone, now=NOW) == [recent]
    assert await _stale(pool, recent) is True
    assert await _stale(pool, old) is False  # its timelapse vlog is never re-scripted into a slideshow


async def test_the_rename_refresh_signs_without_reading_embeddings(pool, pki_settings, jobs_open, monkeypatch):
    pi, cam = await _day(pool, pki_settings)
    start, end = NINE, NINE + dt.timedelta(hours=1)
    async with pool.connection() as conn:
        two = await chapters._points(conn, start, end, None)
        assert await chapters._place_signature(conn, start, end) == segment.signature(two)
        one = [p for p in two if p.device_id == pi["device_id"]]
        cur = await conn.execute("DELETE FROM moment WHERE device_id = %s", (cam["device_id"],))
        assert cur.rowcount > 0
        assert await chapters._place_signature(conn, start, end) == segment.signature(one)

    async def no_vectors(*args, **kwargs):
        raise AssertionError("the rename refresh read the embeddings")

    monkeypatch.setattr(chapters, "_points", no_vectors)
    await places.set_location(pool, pi["device_id"], "Phòng ăn")
    await chapters.refresh_places(pool, pi["device_id"], pki_settings.zone, now=NOW)

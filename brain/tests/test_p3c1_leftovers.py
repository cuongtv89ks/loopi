import datetime as dt
import json

import httpx
import pytest

from helpers import FakeOllama, insert_moment, paired_device
from loopi_brain import chat, devices, places, search
from loopi_brain.app import create_app

HX = {"HX-Request": "true"}
NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)


async def _active(pool, *device_ids):
    async with pool.connection() as conn:
        for d in device_ids:
            await conn.execute("UPDATE device SET last_heartbeat_at = now() WHERE id = %s", (d,))


async def test_with_two_active_cameras_every_result_names_its_place(pool, pki_settings):
    a, b = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await places.set_location(pool, a["device_id"], "Bếp")
    await _active(pool, a["device_id"], b["device_id"])
    assert await places.active_cameras(pool) == 2
    await insert_moment(pool, a["device_id"], kind="speech", ts=NINE, text="nấu cơm", lang="vi")
    hits = (await search.search_moments(pool, None, "nấu cơm")).hits
    assert chat.source_lines(hits, pki_settings.zone, named=True) == ["[1] 2026-10-14 09:00 at Bếp, speech (vi): nấu cơm"]


async def test_only_recently_heard_cameras_that_are_not_revoked_are_active(pool, pki_settings):
    fresh, stale, revoked, silent = [await paired_device(pool, pki_settings) for _ in range(4)]
    await _active(pool, fresh["device_id"], revoked["device_id"])
    async with pool.connection() as conn:
        await conn.execute("UPDATE device SET last_heartbeat_at = now() - interval '8 days' WHERE id = %s",
                           (stale["device_id"],))
    await devices.revoke_device(pool, revoked["device_id"])
    assert silent  # never sent a heartbeat
    assert await places.active_cameras(pool) == 1


async def test_a_device_is_active_until_it_is_revoked(pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    assert await places.is_active_device(pool, dev["device_id"])
    assert not await places.is_active_device(pool, "NOPE")
    await devices.revoke_device(pool, dev["device_id"])
    assert not await places.is_active_device(pool, dev["device_id"])


async def test_named_defaults_to_the_devices_of_the_hits(pki_settings):
    def hit(n, device):
        return search.Hit(f"M{n}", "speech", NINE, "đi chợ", "vi", None, None, None, None, None, 0.0,
                          device_id=device, location=f"Chỗ {device}")

    assert chat.source_lines([hit(1, "A"), hit(2, "A")], pki_settings.zone) == [
        "[1] 2026-10-14 09:00, speech (vi): đi chợ", "[2] 2026-10-14 09:00, speech (vi): đi chợ"]
    assert chat.source_lines([hit(1, "A")], pki_settings.zone, named=True) == [
        "[1] 2026-10-14 09:00 at Chỗ A, speech (vi): đi chợ"]
    assert chat.source_lines([hit(1, "A"), hit(2, "B")], pki_settings.zone, named=False) == [
        "[1] 2026-10-14 09:00, speech (vi): đi chợ", "[2] 2026-10-14 09:00, speech (vi): đi chợ"]


@pytest.fixture
async def web(pool, pki_settings, jobs_open):
    ollama = FakeOllama()
    app = create_app(pki_settings, pool=pool, http_client=ollama.client(), llm_client=ollama.client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        c.ollama = ollama
        yield c


async def test_search_and_chat_name_the_place_of_hits_from_one_camera_when_two_are_active(web, pool, pki_settings):
    a, b = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await places.set_location(pool, a["device_id"], "Bếp")
    await _active(pool, a["device_id"], b["device_id"])
    await insert_moment(pool, a["device_id"], kind="speech", ts=NINE, text="nấu cơm", lang="vi")
    page = (await web.get("/search", params={"q": "nấu cơm"})).text
    assert '<span class="place">Bếp</span>' in page
    web.ollama.reply = lambda body: "Bạn nấu cơm [1]."
    async with web.stream("POST", "/chat", data={"q": "nấu cơm"}, headers=HX) as r:
        body = (await r.aread()).decode()
    sources = next(json.loads(block.split("data: ", 1)[1]) for block in body.split("\n\n")
                   if block.startswith("event: sources"))
    assert '<span class="place">Bếp</span>' in sources["html"]
    [answer] = [x for path, x in web.ollama.requests if path == "/api/chat" and x["stream"]]
    assert "at Bếp, speech (vi): nấu cơm" in answer["messages"][-1]["content"]


async def test_with_one_active_camera_the_place_stays_unnamed(web, pool, pki_settings):
    a, b = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await places.set_location(pool, a["device_id"], "Bếp")
    await _active(pool, a["device_id"])  # the other pairing is old and silent
    await insert_moment(pool, a["device_id"], kind="speech", ts=NINE, text="nấu cơm", lang="vi")
    assert b
    assert 'class="place"' not in (await web.get("/search", params={"q": "nấu cơm"})).text


async def test_a_bad_name_for_an_unknown_device_is_a_404(client):
    r = await client.post("/devices/NOPE/location", data={"location": " "}, headers=HX)
    assert r.status_code == 404


async def test_a_bad_name_for_a_revoked_device_is_a_404(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    await devices.revoke_device(pool, dev["device_id"])
    assert (await client.post(f"/devices/{dev['device_id']}/location", data={"location": " "},
                              headers=HX)).status_code == 404


async def test_a_saved_name_also_updates_the_card_title(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    r = await client.post(f"/devices/{dev['device_id']}/location", data={"location": "Bếp"}, headers=HX)
    assert f'<strong id="device-title-{dev["device_id"]}" hx-swap-oob="true">Bếp</strong>' in r.text
    page = (await client.get("/devices")).text
    assert f'<strong id="device-title-{dev["device_id"]}">Bếp</strong>' in page


async def test_a_refused_name_does_not_touch_the_card_title(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    r = await client.post(f"/devices/{dev['device_id']}/location", data={"location": " "}, headers=HX)
    assert "hx-swap-oob" not in r.text


async def test_a_camera_filter_keeps_the_day_s_data_and_names_the_camera(client, pool, pki_settings):
    a, b = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await places.set_location(pool, b["device_id"], "Bếp")
    await insert_moment(pool, a["device_id"], kind="speech", ts=NINE, text="Một", lang="vi")
    text = (await client.get("/", params={"day": "2026-10-14", "device": b["device_id"]})).text
    assert "Không có dữ liệu cho ngày này." not in text and "Tạo lại recap" in text
    assert "<strong>Bếp</strong>" in text


async def test_a_day_with_no_moments_at_all_still_has_no_data_under_a_filter(client, pool, pki_settings):
    a = await paired_device(pool, pki_settings)
    text = (await client.get("/", params={"day": "2026-10-14", "device": a["device_id"]})).text
    assert "Không có dữ liệu cho ngày này." in text and "Tạo lại recap" not in text


async def test_the_retention_wording_no_longer_speaks_of_raw_video(web, pool):
    r = await web.post("/settings/retention", data={"video_days": "5", "audio_days": "9", "enabled": "on"}, headers=HX)
    assert "Đã lưu: giữ video 5 ngày, audio thô 9 ngày." in r.text
    text = (await web.get("/settings")).text
    assert ("Video (MP4) và âm thanh cũ hơn số ngày này bị xoá lúc 03:00; keyframe, transcript, chương, recap và vlog "
            "giữ vĩnh viễn.") in text
    assert "video thô" not in text
    card = (await web.get("/pipeline")).text
    assert "giữ video 5 ngày, audio thô 9 ngày" in card and "video thô" not in card


async def test_the_deletion_line_follows_the_retention_switch(web, pool):
    on = "Video (MP4) và âm thanh cũ hơn số ngày này bị xoá lúc 03:00; keyframe, transcript, chương, recap và vlog giữ vĩnh viễn."
    off = ("Xoá tự động đang tắt; khi bật, video (MP4) và âm thanh cũ hơn số ngày này bị xoá lúc 03:00; keyframe, "
           "transcript, chương, recap và vlog giữ vĩnh viễn.")
    await web.post("/settings/retention", data={"video_days": "7", "audio_days": "7"}, headers=HX)
    text = (await web.get("/settings")).text
    assert off in text and on not in text  # while off, the page never says files are deleted at 03:00
    await web.post("/settings/retention", data={"video_days": "7", "audio_days": "7", "enabled": "on"}, headers=HX)
    text = (await web.get("/settings")).text
    assert on in text and off not in text


async def test_a_vlog_made_from_keyframes_says_its_video_expired(web, pool):
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO vlog (day, state, source, path) VALUES (%s, 'rendered', 'keyframes', 'vlog/x.mp4')",
                           (dt.date(2026, 10, 14),))
    text = (await web.get("/", params={"day": "2026-10-14"})).text
    assert "từ khung hình (video đã hết hạn)" in text and "video thô" not in text

import datetime as dt
import re
from zoneinfo import ZoneInfo

from helpers import paired_device
from loopi_brain import devices, pki
from loopi_brain.web.routes import day_bounds_utc

HX = {"HX-Request": "true"}


async def _insert_item(pool, device_id, item_id, started_at, kind="audio"):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO item (id, device_id, kind, started_at, ended_at, sha256, size_bytes,"
            " raw_path, view_path, status) VALUES (%s, %s, %s, %s, %s, 'x', 1, %s, %s, 'ready')",
            (item_id, device_id, kind, started_at, started_at + dt.timedelta(minutes=1),
             f"raw/{item_id}.opus", f"raw/{item_id}.opus"),
        )


def test_day_bounds_utc_for_seoul():
    start, end = day_bounds_utc(dt.date(2026, 10, 15), ZoneInfo("Asia/Seoul"))
    assert start == dt.datetime(2026, 10, 14, 15, 0, tzinfo=dt.UTC)
    assert end == dt.datetime(2026, 10, 15, 15, 0, tzinfo=dt.UTC)


async def test_timeline_uses_local_day_boundaries(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    late = dt.datetime(2026, 10, 14, 23, 30, tzinfo=dt.UTC)  # 08:30 on Oct 15 in Seoul
    await _insert_item(pool, dev["device_id"], "01JA2B3C4D5E6F7G8H9J0KMNPS", late)
    on_15 = await client.get("/hours", params={"day": "2026-10-15"})
    on_14 = await client.get("/hours", params={"day": "2026-10-14"})
    assert on_15.status_code == 200
    assert "01JA2B3C4D5E6F7G8H9J0KMNPS" in on_15.text and "08:30:00" in on_15.text
    assert "01JA2B3C4D5E6F7G8H9J0KMNPS" not in on_14.text


async def test_timeline_rejects_bad_day(client):
    assert (await client.get("/hours", params={"day": "yesterday"})).status_code == 422


async def test_devices_page_shows_online_device(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    await devices.record_heartbeat(pool, dev["device_id"], {"paused": True, "queue_depth": 4}, [])
    r = await client.get("/devices")
    assert r.status_code == 200
    assert "trực tuyến" in r.text and "loopi-" in r.text


async def test_commands_require_htmx_header(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    assert (await client.post(f"/devices/{dev['device_id']}/commands/pause")).status_code == 403
    r = await client.post(f"/devices/{dev['device_id']}/commands/audio-off", headers=HX)
    assert r.status_code == 200
    assert "Tắt ghi âm" in r.text and "audio-off" not in r.text and "15" in r.text
    pending = await devices.record_heartbeat(pool, dev["device_id"], {}, [])
    assert [(c["type"], c["payload"]) for c in pending] == [("set_config", {"audio_enabled": False})]


async def test_unknown_action_or_device_is_404(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    assert (await client.post(f"/devices/{dev['device_id']}/commands/reboot", headers=HX)).status_code == 404
    assert (await client.post("/devices/NOPE/commands/pause", headers=HX)).status_code == 404


async def test_pair_token_partial_shows_token_and_ca(client, pki_settings):
    r = await client.post("/devices/pair-token", headers=HX)
    assert r.status_code == 200
    assert re.search(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}", r.text)
    assert pki.fingerprint(pki.ca_cert_pem(pki_settings.pki_dir)) in r.text
    assert (await client.post("/devices/pair-token")).status_code == 403


async def test_revoke_device(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    assert (await client.post(f"/devices/{dev['device_id']}/revoke")).status_code == 403
    r = await client.post(f"/devices/{dev['device_id']}/revoke", headers=HX)
    assert r.status_code == 200
    assert "thu hồi" in r.text and "revoked" not in r.text and "15" not in r.text
    assert await devices.get_active_device(pool, dev["fingerprint"]) is None
    assert (await client.post("/devices/NOPE/revoke", headers=HX)).status_code == 404


async def test_device_name_is_escaped(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    async with pool.connection() as conn:
        await conn.execute("UPDATE device SET name = %s WHERE id = %s", ("<script>x</script>", dev["device_id"]))
    r = await client.get("/devices")
    assert "<script>x</script>" not in r.text and "&lt;script&gt;" in r.text


async def test_devices_page_uses_vietnamese_for_flags_and_missing_values(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    status = {"paused": True, "audio_enabled": False, "recording_video": None,
              "soc_temp_c": None, "wifi_rssi_dbm": None}
    await devices.record_heartbeat(pool, dev["device_id"], status, [])
    text = (await client.get("/devices")).text
    assert "tạm dừng: có" in text and "audio bật: không" in text
    assert "Đang ghi hình: —" in text and "nhiệt độ: —" in text and "WiFi: —" in text
    for raw in ("None", "True", "False", "°C", " dBm"):
        assert raw not in text


async def test_page_has_vietnamese_htmx_error_handler(client):
    text = (await client.get("/devices")).text
    assert 'id="hx-error"' in text and "htmx:responseError" in text
    assert "Không tìm thấy thiết bị" in text and "Có lỗi, thử lại" in text


async def test_healthz_is_unauthenticated_and_ok(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

"""Thiết bị's update controls and the Phiên bản page (P4a design §8, §8.1; spec D27, D29)."""
import pytest

from helpers import fetch_all, paired_device, write_bundle
from loopi_brain import devices, ota

HX = {"HX-Request": "true"}
NAME = "loopi-dev-2026.10.21-a1b2c3d.raucb"
STEM = "loopi-dev-2026.10.21-a1b2c3d"
NOTES = [{"commit": "d" * 40, "subject": "feat(agent): resume bundle downloads"}]


@pytest.fixture
def ota_dir(pki_settings):
    write_bundle(pki_settings.ota_dir, notes=NOTES, notes_base="2026.10.20-aaaaaaa")
    return pki_settings.ota_dir


async def test_devices_page_shows_os_and_update_controls(client, pool, pki_settings, ota_dir):
    dev = await paired_device(pool, pki_settings)
    await devices.record_heartbeat(
        pool, dev["device_id"], {"os_version": "2026.10.20-aaaaaaa", "os_variant": "dev", "boot_slot": "A"}, [])
    r = await client.get("/devices")
    assert r.status_code == 200
    assert "2026.10.20-aaaaaaa" in r.text and "slot A" in r.text
    assert f'value="{NAME}"' in r.text
    assert f'hx-post="/devices/{dev["device_id"]}/update"' in r.text


async def test_update_requires_htmx_and_queues_the_command(client, pool, pki_settings, ota_dir):
    dev = await paired_device(pool, pki_settings)
    url = f"/devices/{dev['device_id']}/update"
    assert (await client.post(url, data={"bundle": NAME})).status_code == 403
    r = await client.post(url, data={"bundle": NAME}, headers=HX)
    assert r.status_code == 200
    assert "Đã gửi lệnh cập nhật" in r.text
    (row,) = await fetch_all(pool, "SELECT bundle, state FROM ota_update")
    assert row == {"bundle": NAME, "state": "requested"}


async def test_a_refused_update_says_why(client, pool, pki_settings, ota_dir):
    dev = await paired_device(pool, pki_settings)
    url = f"/devices/{dev['device_id']}/update"
    await client.post(url, data={"bundle": NAME}, headers=HX)
    r = await client.post(url, data={"bundle": NAME}, headers=HX)
    assert r.status_code == 200
    assert ota.BUSY in r.text
    r = await client.post(url, data={"bundle": "loopi-dev-nope.raucb"}, headers=HX)
    assert ota.NOT_FOUND in r.text


async def test_update_for_an_unknown_device_is_404(client, ota_dir):
    r = await client.post("/devices/NOPE/update", data={"bundle": NAME}, headers=HX)
    assert r.status_code == 404


async def test_a_busy_device_shows_a_disabled_button_and_its_history(client, pool, pki_settings, ota_dir):
    dev = await paired_device(pool, pki_settings)
    command_id = await ota.request_update(pool, ota_dir, dev["device_id"], NAME)
    await ota.record_report(pool, dev["device_id"], {"command_id": command_id, "state": "trying", "version": "v",
                                                     "error": None})
    r = await client.get("/devices")
    assert "Đang thử bản mới" in r.text
    assert "disabled" in r.text


async def test_failed_updates_show_their_error(client, pool, pki_settings, ota_dir):
    dev = await paired_device(pool, pki_settings)
    command_id = await ota.request_update(pool, ota_dir, dev["device_id"], NAME)
    await ota.record_report(pool, dev["device_id"], {"command_id": command_id, "state": "failed", "version": "v",
                                                     "error": "install failed: signature"})
    r = await client.get("/devices")
    assert "Lỗi" in r.text and "install failed: signature" in r.text


async def test_update_notes_fragment(client, ota_dir):
    r = await client.get("/devices/update-notes", params={"bundle": NAME})
    assert r.status_code == 200
    assert "resume bundle downloads" in r.text and "2026.10.20-aaaaaaa" in r.text
    assert (await client.get("/devices/update-notes", params={"bundle": "nope"})).text.strip() == ""
    assert (await client.get("/devices/update-notes")).text.strip() == ""


async def test_versions_page_lists_bundles_with_running_devices(client, pool, pki_settings, ota_dir):
    write_bundle(ota_dir, version="2026.10.22-bbbbbbb", variant="prod", built_at="2026-10-22T01:00:00Z")
    (ota_dir / "loopi-dev-orphan.raucb").write_bytes(b"x")
    dev = await paired_device(pool, pki_settings)
    await devices.record_heartbeat(pool, dev["device_id"], {"os_version": "2026.10.21-a1b2c3d", "os_variant": "dev"}, [])
    r = await client.get("/versions")
    assert r.status_code == 200
    assert r.text.index("2026.10.22-bbbbbbb") < r.text.index("2026.10.21-a1b2c3d")
    assert "loopi-" + dev["device_id"][-4:].lower() in r.text
    assert "resume bundle downloads" in r.text and "so với 2026.10.20-aaaaaaa" in r.text
    assert "Không dùng được" in r.text and "loopi-dev-orphan.raucb" in r.text


async def test_versions_page_without_bundles(client):
    r = await client.get("/versions")
    assert "Chưa có bản cập nhật nào" in r.text


async def test_release_notes_are_escaped(client, pki_settings):
    write_bundle(pki_settings.ota_dir, notes=[{"commit": "e" * 40, "subject": "<script>alert(1)</script>"}])
    r = await client.get("/versions")
    assert "<script>alert(1)" not in r.text
    assert "&lt;script&gt;alert(1)" in r.text


async def test_truncated_notes_say_so(client, pki_settings):
    write_bundle(pki_settings.ota_dir, notes=NOTES, notes_truncated=True)
    assert "50 thay đổi mới nhất" in (await client.get("/versions")).text


async def test_delete_from_the_versions_page(client, pool, pki_settings, ota_dir):
    url = f"/versions/{STEM}/delete"
    assert (await client.post(url)).status_code == 403
    r = await client.post(url, headers=HX)
    assert r.status_code == 200 and "Đã xoá" in r.text
    assert not (ota_dir / NAME).exists()


async def test_a_refused_delete_keeps_the_card_and_says_why(client, pool, pki_settings, ota_dir):
    dev = await paired_device(pool, pki_settings)
    await devices.record_heartbeat(pool, dev["device_id"], {"os_version": "2026.10.21-a1b2c3d", "os_variant": "dev"}, [])
    r = await client.post(f"/versions/{STEM}/delete", headers=HX)
    assert r.status_code == 200
    assert "đang chạy bản này" in r.text and f'id="bundle-{STEM}"' in r.text
    assert (ota_dir / NAME).exists()


async def test_delete_of_an_unknown_name_is_404(client, ota_dir):
    assert (await client.post("/versions/loopi-dev-nope/delete", headers=HX)).status_code == 404


async def test_nav_links_the_versions_page(client):
    assert 'href="/versions"' in (await client.get("/devices")).text


async def test_an_unusable_file_can_be_deleted_from_the_versions_page(client, pki_settings):
    pki_settings.ota_dir.mkdir(parents=True, exist_ok=True)
    (pki_settings.ota_dir / "loopi-dev-orphan.raucb").write_bytes(b"x")
    page = await client.get("/versions")
    assert 'hx-post="/versions/loopi-dev-orphan/delete"' in page.text
    r = await client.post("/versions/loopi-dev-orphan/delete", headers=HX)
    assert r.status_code == 200 and "Đã xoá loopi-dev-orphan" in r.text
    assert not (pki_settings.ota_dir / "loopi-dev-orphan.raucb").exists()


async def test_revoked_and_silent_devices_are_not_listed_as_running(client, pool, pki_settings, ota_dir):
    revoked = await paired_device(pool, pki_settings)
    await devices.record_heartbeat(
        pool, revoked["device_id"], {"os_version": "2026.10.21-a1b2c3d", "os_variant": "dev"}, [])
    await devices.revoke_device(pool, revoked["device_id"])
    await paired_device(pool, pki_settings)  # never sent a heartbeat
    r = await client.get("/versions")
    assert "Đang chạy trên: không thiết bị nào" in r.text
    assert "loopi-" + revoked["device_id"][-4:].lower() not in r.text

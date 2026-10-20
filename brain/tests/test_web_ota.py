"""Thiết bị's update controls and the Phiên bản page (P4a design §8, §8.1; spec D27, D29)."""
import re

import pytest

from helpers import fetch_all, paired_device, write_bundle
from loopi_brain import devices, ota

HX = {"HX-Request": "true"}
NAME = "loopi-dev-2026.10.21-a1b2c3d.raucb"
STEM = "loopi-dev-2026.10.21-a1b2c3d"
NOTES = [{"commit": "d" * 40, "subject": "feat(agent): resume bundle downloads"}]


NO_SLOT_REASON = "Thiết bị này chưa có slot A/B nên chưa cập nhật qua mạng được."
SLOT_HB = {"os_version": "2026.10.20-aaaaaaa", "os_variant": "dev", "boot_slot": "A"}


@pytest.fixture
def ota_dir(pki_settings):
    write_bundle(pki_settings.ota_dir, notes=NOTES, notes_base="2026.10.20-aaaaaaa")
    return pki_settings.ota_dir


def update_form(html):
    """The update form of the only device on the page."""
    start = html.index('/update"')
    return html[html.rindex("<form", 0, start):html.index("</form>", start)]


def tag(html, name):
    """The opening tag <name ...> (attributes included) of the first <name> in html."""
    return re.search(rf"<{name}\b[^>]*>", html).group(0)


def is_disabled(opening_tag):
    return re.search(r"\bdisabled\b", opening_tag) is not None


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
    await devices.record_heartbeat(pool, dev["device_id"], SLOT_HB, [])
    command_id = await ota.request_update(pool, ota_dir, dev["device_id"], NAME)
    await ota.record_report(pool, dev["device_id"], {"command_id": command_id, "state": "trying", "version": "v",
                                                     "error": None})
    r = await client.get("/devices")
    assert "Đang thử bản mới" in r.text
    form = update_form(r.text)
    assert is_disabled(tag(form, "select")) and is_disabled(tag(form, "button"))
    assert NO_SLOT_REASON not in form  # busy is the only reason here


async def test_a_device_with_a_slot_can_be_updated_when_idle(client, pool, pki_settings, ota_dir):
    dev = await paired_device(pool, pki_settings)
    await devices.record_heartbeat(pool, dev["device_id"], SLOT_HB, [])
    form = update_form((await client.get("/devices")).text)
    assert not is_disabled(tag(form, "select")) and not is_disabled(tag(form, "button"))
    assert NO_SLOT_REASON not in form


async def test_a_device_without_a_slot_cannot_be_updated_and_says_why(client, pool, pki_settings, ota_dir):
    # A pre-P4a agent reports no boot_slot: the device has no slot A/B, so an update could never install.
    dev = await paired_device(pool, pki_settings)
    await devices.record_heartbeat(pool, dev["device_id"], {"os_version": "2026.10.20-aaaaaaa", "os_variant": "dev"}, [])
    form = update_form((await client.get("/devices")).text)
    assert is_disabled(tag(form, "select")) and is_disabled(tag(form, "button"))
    assert NO_SLOT_REASON in form


async def test_a_device_that_never_sent_a_heartbeat_cannot_be_updated(client, pool, pki_settings, ota_dir):
    await paired_device(pool, pki_settings)
    form = update_form((await client.get("/devices")).text)
    assert is_disabled(tag(form, "select")) and is_disabled(tag(form, "button"))
    assert NO_SLOT_REASON in form


async def test_failed_updates_show_their_error(client, pool, pki_settings, ota_dir):
    dev = await paired_device(pool, pki_settings)
    command_id = await ota.request_update(pool, ota_dir, dev["device_id"], NAME)
    await ota.record_report(pool, dev["device_id"], {"command_id": command_id, "state": "failed", "version": "v",
                                                     "error": "install failed: signature"})
    r = await client.get("/devices")
    assert "Lỗi" in r.text and "install failed: signature" in r.text


def summary_line(html):
    """The "Hệ điều hành" line of the only device on the page."""
    start = html.index("<li>Hệ điều hành")
    return html[start:html.index("</li>", start)]


async def test_the_summary_line_shows_why_the_latest_update_failed(client, pool, pki_settings, ota_dir):
    dev = await paired_device(pool, pki_settings)
    await devices.record_heartbeat(pool, dev["device_id"], SLOT_HB, [])
    command_id = await ota.request_update(pool, ota_dir, dev["device_id"], NAME)
    await ota.record_report(pool, dev["device_id"], {"command_id": command_id, "state": "failed", "version": "v",
                                                     "error": "install failed: signature"})
    line = summary_line((await client.get("/devices")).text)
    assert "cập nhật gần nhất: Lỗi: install failed: signature" in line


async def test_the_error_in_the_summary_line_is_escaped(client, pool, pki_settings, ota_dir):
    dev = await paired_device(pool, pki_settings)
    command_id = await ota.request_update(pool, ota_dir, dev["device_id"], NAME)
    await ota.record_report(pool, dev["device_id"], {"command_id": command_id, "state": "failed", "version": "v",
                                                     "error": "<script>alert(1)</script>"})
    line = summary_line((await client.get("/devices")).text)
    assert "<script>" not in line and "&lt;script&gt;alert(1)&lt;/script&gt;" in line


async def test_the_summary_line_of_a_running_update_has_no_error_suffix(client, pool, pki_settings, ota_dir):
    dev = await paired_device(pool, pki_settings)
    await ota.request_update(pool, ota_dir, dev["device_id"], NAME)
    line = summary_line((await client.get("/devices")).text)
    assert line.rstrip().endswith("cập nhật gần nhất: Đã gửi lệnh, chờ thiết bị nhận")


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


async def test_previewing_notes_does_not_ask_for_the_install_confirmation(client, pool, pki_settings, ota_dir):
    # hx-confirm on the form would be inherited by the select's hx-get and ask "Cài bản đã chọn?" on a preview.
    await paired_device(pool, pki_settings)
    html = (await client.get("/devices")).text
    form = update_form(html)
    assert 'hx-disinherit="hx-confirm"' in form and "hx-confirm=" in form
    assert '<select name="bundle" hx-get="/devices/update-notes"' in form


async def test_delete_buttons_target_their_card_not_an_id_with_dots(client, pki_settings, ota_dir):
    # "#bundle-loopi-dev-2026.10.21-a1b2c3d" is not a valid CSS selector (the dots), so htmx could not find it.
    (ota_dir / "loopi-dev-orphan.raucb").write_bytes(b"x")
    html = (await client.get("/versions")).text
    assert html.count('hx-target="closest .card"') == 2  # the bundle card and the unusable card
    assert 'hx-target="#bundle-' not in html
    assert f'id="bundle-{STEM}"' in html and 'id="bundle-loopi-dev-orphan"' in html

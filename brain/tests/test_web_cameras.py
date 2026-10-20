
import asyncio
import time

from helpers import fetch_one, make_csr_pem, paired_device
from loopi_brain import camera_probe, cameras, devices, pki

HX = {"HX-Request": "true"}
FORM = dict(kind="ip", location="Phòng khách", video_url="rtsp://192.0.2.10:554/cam/realmonitor?channel=1&subtype=0",
            audio_url="", username="admin", password="S3cr3t", has_audio="on")


async def test_the_devices_page_offers_the_camera_form_and_the_estimate(client):
    page = (await client.get("/devices")).text
    assert "Thêm camera mạng" in page and 'hx-post="/cameras"' in page and 'hx-post="/cameras/probe"' in page
    assert "Dung lượng ước tính cho" in page
    assert "Camera IP (RTSP)" in page and "rtsp://&lt;IP&gt;:554/cam/realmonitor?channel=1&amp;subtype=0" in page


async def test_switching_the_kind_fills_its_template(client):
    form = (await client.get("/cameras/form", params={"kind": "esp32"})).text
    assert 'value="http://&lt;IP&gt;:81/stream"' in form and "Firmware Loopi" in form
    assert 'name="has_audio"' in form and "checked" not in form.split('name="has_audio"')[1].split(">")[0]


async def test_a_phone_records_no_sound_by_default_and_its_hint_names_the_sound_address(client):
    # Its template is MJPEG over HTTP, which has no sound: "Ghi âm" on would only report a missing microphone.
    phone = cameras.KINDS["phone"]
    assert phone.has_audio is False and phone.video_template == "http://<IP>:8080/video"
    assert phone.hint == ('Android "IP Webcam": hình http://<IP>:8080/video, tiếng http://<IP>:8080/audio.wav (điền vào ô '
                          'âm thanh riêng và chọn "Ghi âm"). iPhone: dùng địa chỉ RTSP mà app hiện ra (có cả tiếng).')
    form = (await client.get("/cameras/form", params={"kind": "phone"})).text
    assert "tiếng http://&lt;IP&gt;:8080/audio.wav" in form
    assert "checked" not in form.split('name="has_audio"')[1].split(">")[0]


async def test_adding_a_camera(client, pool, pki_settings):
    assert (await client.post("/cameras", data=FORM)).status_code == 403
    r = await client.post("/cameras", data=FORM, headers=HX)
    assert r.status_code == 200 and "Đã thêm camera Phòng khách" in r.text and r.headers.get("HX-Refresh") == "true"
    row = await fetch_one(pool, "SELECT id, location FROM network_camera")
    assert row["location"] == "Phòng khách"
    assert (pki_settings.cameras_dir / row["id"] / "loopi.toml").exists()
    page = (await client.get("/devices")).text
    assert "Camera chờ ghép cặp" in page and "S3cr3t" not in page


async def test_a_refused_camera_keeps_the_form_without_the_password(client, pool):
    r = await client.post("/cameras", data=FORM | {"video_url": "rtsp://<IP>/cam"}, headers=HX)
    assert r.status_code == 200 and "Thay &lt;IP&gt; bằng địa chỉ của camera." in r.text
    assert "S3cr3t" not in r.text and 'value="Phòng khách"' in r.text
    assert await fetch_one(pool, "SELECT count(*) AS n FROM network_camera") == {"n": 0}


async def test_probing_shows_what_the_stream_carries(client, monkeypatch):
    async def fake_probe(url, username, password, **_):
        assert (url, username, password) == (FORM["video_url"], "admin", "S3cr3t")
        return camera_probe.ProbeResult(True, "Kết nối được: h264 1920×1080, 25 khung hình/giây, có âm thanh.")
    monkeypatch.setattr(camera_probe, "probe", fake_probe)
    r = await client.post("/cameras/probe", data=FORM, headers=HX)
    assert r.status_code == 200 and "Kết nối được: h264 1920×1080" in r.text


def _spy_probe(monkeypatch):
    calls = []

    async def spy(url, username, password, **_):
        calls.append(url)
        return camera_probe.ProbeResult(True, "đã thử")
    monkeypatch.setattr(camera_probe, "probe", spy)
    return calls


async def test_probing_refuses_an_address_that_a_save_would_refuse_and_never_runs_ffprobe(client, monkeypatch):
    calls = _spy_probe(monkeypatch)
    r = await client.post("/cameras/probe", data=FORM | {"video_url": "rtsp://<IP>/cam"}, headers=HX)
    assert r.status_code == 200 and "Thay &lt;IP&gt; bằng địa chỉ của camera." in r.text and "S3cr3t" not in r.text
    r = await client.post("/cameras/probe", data=FORM | {"video_url": "file:///etc/passwd"}, headers=HX)
    assert r.status_code == 200 and "Địa chỉ phải bắt đầu bằng rtsp://, http:// hoặc https://." in r.text
    r = await client.post("/cameras/probe", data=FORM | {"video_url": ""}, headers=HX)
    assert r.status_code == 200 and "Địa chỉ phải bắt đầu bằng" in r.text
    r = await client.post("/cameras/probe", data=FORM | {"audio_url": "ftp://192.0.2.10/audio"}, headers=HX)
    assert r.status_code == 200 and "Địa chỉ âm thanh phải bắt đầu bằng http://, https:// hoặc rtsp://." in r.text
    r = await client.post("/cameras/probe", data=FORM | {"audio_url": "http://<IP>/audio"}, headers=HX)
    assert r.status_code == 200 and "Thay &lt;IP&gt; bằng địa chỉ của camera." in r.text
    r = await client.post("/cameras/probe", data=FORM | {"video_url": "http://192.0.2.10/v.cgi?user=admin&pwd=x"}, headers=HX)
    assert r.status_code == 200 and "Đừng ghi tên đăng nhập hay mật khẩu vào địa chỉ" in r.text
    assert calls == []  # nothing, not even the good video address next to the bad audio address, reached ffprobe


async def test_probing_refuses_credentials_a_save_would_refuse_and_never_runs_ffprobe(client, monkeypatch):
    calls = _spy_probe(monkeypatch)
    r = await client.post("/cameras/probe", data=FORM | {"password": "p" * 129}, headers=HX)
    assert r.status_code == 200 and "Mật khẩu dài quá 128 ký tự." in r.text and "p" * 129 not in r.text
    r = await client.post("/cameras/probe", data=FORM | {"username": "u" * 65}, headers=HX)
    assert r.status_code == 200 and "Tên đăng nhập dài quá 64 ký tự." in r.text
    r = await client.post("/cameras/probe", data=FORM | {"password": "pw\x00"}, headers=HX)
    assert r.status_code == 200 and "Mật khẩu có ký tự không hợp lệ." in r.text
    assert calls == []
    r = await client.post("/cameras/probe", data=FORM | {"username": "u" * 64, "password": "p" * 128}, headers=HX)
    assert "đã thử" in r.text and len(calls) == 1  # the limits themselves are accepted


async def test_a_separate_sound_address_is_probed_for_sound_only(client, monkeypatch):
    async def fake_probe(url, username, password, **_):
        if url.endswith("/stream"):
            return camera_probe.ProbeResult(True, "Kết nối được: mjpeg 640×480, 10 khung hình/giây, không có âm thanh.",
                                            has_audio=False)
        if url.endswith("/audio"):  # a sound-only stream: ffprobe finds no picture
            return camera_probe.ProbeResult(False, "Luồng này không có hình.", has_audio=True)
        return camera_probe.ProbeResult(False, "Không kết nối được tới camera.")
    monkeypatch.setattr(camera_probe, "probe", fake_probe)
    data = FORM | {"video_url": "http://192.0.2.20/stream", "audio_url": "http://192.0.2.20/audio"}
    page = (await client.post("/cameras/probe", data=data, headers=HX)).text
    assert "Kết nối được: mjpeg 640×480" in page and "Âm thanh: có." in page and 'class="online"' in page
    page = (await client.post("/cameras/probe", data=data | {"audio_url": "http://192.0.2.20/mic"}, headers=HX)).text
    assert "Âm thanh: Không kết nối được tới camera." in page and 'class="err"' in page
    assert "S3cr3t" not in page


async def test_the_video_and_the_sound_are_probed_together_so_the_button_waits_at_most_one_probe(client, monkeypatch):
    started = []

    async def slow_probe(url, username, password, **_):
        started.append(url)
        await asyncio.sleep(0.3)
        if url.endswith("/audio"):
            return camera_probe.ProbeResult(False, "Luồng này không có hình.", has_audio=True)
        return camera_probe.ProbeResult(True, "Kết nối được: mjpeg 640×480, 10 khung hình/giây, không có âm thanh.",
                                        has_audio=False)
    monkeypatch.setattr(camera_probe, "probe", slow_probe)
    data = FORM | {"video_url": "http://192.0.2.20/stream", "audio_url": "http://192.0.2.20/audio"}
    begun = time.perf_counter()
    page = (await client.post("/cameras/probe", data=data, headers=HX)).text
    took = time.perf_counter() - begun
    assert took < 0.5, f"the probes ran one after the other: {took:.2f} s"
    assert sorted(started) == ["http://192.0.2.20/audio", "http://192.0.2.20/stream"]
    # Both answers, the video's first.
    assert "Kết nối được: mjpeg 640×480" in page and "Âm thanh: có." in page
    assert page.index("Kết nối được: mjpeg") < page.index("Âm thanh: có.")


async def test_repairing_and_removing_a_waiting_camera(client, pool, pki_settings):
    await client.post("/cameras", data=FORM, headers=HX)
    camera_id = (await fetch_one(pool, "SELECT id FROM network_camera"))["id"]
    r = await client.post(f"/cameras/{camera_id}/repair", headers=HX)
    assert r.status_code == 200 and "Đã tạo token ghép cặp mới" in r.text
    r = await client.post(f"/cameras/{camera_id}/remove", headers=HX)
    assert r.status_code == 200 and "Đã gỡ camera Phòng khách" in r.text and r.headers.get("HX-Refresh") == "true"
    assert (pki_settings.cameras_dir / camera_id / "disabled").exists()
    assert (await client.post(f"/cameras/{camera_id}/remove", headers=HX)).status_code == 404


async def test_a_waiting_camera_card_keeps_its_buttons_when_a_message_comes_back(client, pool):
    await client.post("/cameras", data=FORM, headers=HX)
    camera_id = (await fetch_one(pool, "SELECT id FROM network_camera"))["id"]
    page = (await client.get("/devices")).text
    assert f'id="camera-msg-{camera_id}"' in page
    assert f'hx-post="/cameras/{camera_id}/repair" hx-target="#camera-msg-{camera_id}"' in page
    assert f'hx-post="/cameras/{camera_id}/remove" hx-target="#camera-msg-{camera_id}"' in page
    assert f'hx-target="#camera-{camera_id}"' not in page  # a swap into the card would replace its buttons


async def test_repairing_a_camera_whose_password_cannot_be_read_says_so(client, pool, pki_settings):
    await client.post("/cameras", data=FORM, headers=HX)
    camera_id = (await fetch_one(pool, "SELECT id FROM network_camera"))["id"]
    (pki_settings.cameras_dir / camera_id / "loopi.toml").unlink()
    r = await client.post(f"/cameras/{camera_id}/repair", headers=HX)
    assert r.status_code == 200 and "Không đọc được mật khẩu đã lưu" in r.text and "S3cr3t" not in r.text
    assert not (pki_settings.cameras_dir / camera_id / "loopi.toml").exists()  # nothing was written over the loss


async def test_repairing_or_removing_an_unknown_camera_is_a_404(client):
    assert (await client.post("/cameras/nope/repair", headers=HX)).status_code == 404
    assert (await client.post("/cameras/nope/remove", headers=HX)).status_code == 404
    assert (await client.post("/cameras/nope/repair")).status_code == 403


async def _pair(client, pool, pki_settings, **over) -> tuple[str, str]:
    """Add a camera through the form and pair it as the cameras container's agent would; (camera id, device id)."""
    await client.post("/cameras", data=FORM | over, headers=HX)
    row = await fetch_one(pool, "SELECT id FROM network_camera")
    token = (await fetch_one(pool, "SELECT token FROM pair_token WHERE camera_id = %s", (row["id"],)))["token"]
    reg = await devices.register_device(
        pool, token, {"model": "x", "serial": "1", "agent_version": "0.1.0"},
        lambda device_id: pki.sign_csr(pki_settings.pki_dir, make_csr_pem(), device_id))
    return row["id"], reg["device_id"]


async def test_a_paired_camera_shows_on_its_device_card_without_a_revoke_button(client, pool, pki_settings):
    camera_id, device_id = await _pair(client, pool, pki_settings, has_audio="")
    page = (await client.get("/devices")).text
    assert "Camera chờ ghép cặp" not in page
    assert "Camera mạng: Camera IP (RTSP)" in page and "không ghi âm" in page and "S3cr3t" not in page
    assert f'hx-post="/cameras/{camera_id}/remove"' in page and f'hx-target="#result-{device_id}"' in page
    assert f"/devices/{device_id}/revoke" not in page  # "Gỡ camera" revokes the device and scrubs the password
    assert "commands/audio-on" not in page and "commands/audio-off" not in page  # a camera without sound
    assert "commands/pause" in page


async def test_a_camera_with_sound_keeps_its_audio_buttons_and_an_ordinary_device_keeps_revoke(client, pool, pki_settings):
    camera_id, device_id = await _pair(client, pool, pki_settings)
    pi = await paired_device(pool, pki_settings)
    page = (await client.get("/devices")).text
    assert "commands/audio-on" in page and "commands/audio-off" in page
    assert f"/devices/{pi['device_id']}/revoke" in page and f"/devices/{device_id}/revoke" not in page


async def test_removing_a_camera_from_its_paired_devices_card(client, pool, pki_settings):
    camera_id, device_id = await _pair(client, pool, pki_settings)
    r = await client.post(f"/cameras/{camera_id}/remove", headers=HX)
    assert r.status_code == 200 and "Đã gỡ camera Phòng khách" in r.text and r.headers.get("HX-Refresh") == "true"
    assert (pki_settings.cameras_dir / camera_id / "disabled").exists()
    assert (await fetch_one(pool, "SELECT revoked FROM device WHERE id = %s", (device_id,)))["revoked"] is True
    assert (await fetch_one(pool, "SELECT removed_at IS NOT NULL AS gone FROM network_camera"))["gone"] is True
    page = (await client.get("/devices")).text
    assert "Camera mạng:" not in page and f"/cameras/{camera_id}/remove" not in page


async def test_a_camera_whose_device_was_revoked_can_still_be_removed(client, pool, pki_settings):
    camera_id, device_id = await _pair(client, pool, pki_settings)
    # A camera stuck by an earlier "Thu hồi": the device is revoked, the camera row is not removed.
    assert (await client.post(f"/devices/{device_id}/revoke", headers=HX)).status_code == 200
    page = (await client.get("/devices")).text
    assert "đã thu hồi" in page and f'hx-post="/cameras/{camera_id}/remove"' in page
    assert f'id="camera-result-{camera_id}"' in page and f'hx-target="#camera-result-{camera_id}"' in page
    assert f'id="result-{device_id}"' not in page  # the target of a live card does not exist here
    r = await client.post(f"/cameras/{camera_id}/remove", headers=HX)
    assert r.status_code == 200 and "Đã gỡ camera Phòng khách" in r.text and r.headers.get("HX-Refresh") == "true"
    assert (pki_settings.cameras_dir / camera_id / "disabled").exists()
    page = (await client.get("/devices")).text
    assert "đã thu hồi" in page and f"/cameras/{camera_id}/remove" not in page


async def test_the_form_warns_when_the_disk_would_fill(client, monkeypatch):
    from loopi_brain import capacity
    monkeypatch.setattr(capacity, "_free_bytes", lambda settings: 10 * 10**9)
    page = (await client.get("/devices")).text
    assert "vượt 85 % chỗ trống" in page


async def test_a_full_disk_says_full_instead_of_zero_percent_and_still_warns(client, monkeypatch):
    from loopi_brain import capacity
    monkeypatch.setattr(capacity, "_free_bytes", lambda settings: 0)
    page = (await client.get("/devices")).text
    assert "còn trống 0,0 GB (đầy)" in page and "(0 %)" not in page
    assert "vượt 85 % chỗ trống" in page


async def test_a_disk_with_room_shows_the_percent_of_it_the_estimate_takes(client, monkeypatch):
    from loopi_brain import capacity
    monkeypatch.setattr(capacity, "_free_bytes", lambda settings: 100 * 10**9)
    page = (await client.get("/devices")).text
    assert "còn trống 100,0 GB" in page and "(0 %)" in page and "đầy" not in page


from helpers import fetch_one
from loopi_brain import camera_probe

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
    assert calls == []  # nothing, not even the good video address next to the bad audio address, reached ffprobe


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


async def test_repairing_and_removing_a_waiting_camera(client, pool, pki_settings):
    await client.post("/cameras", data=FORM, headers=HX)
    camera_id = (await fetch_one(pool, "SELECT id FROM network_camera"))["id"]
    r = await client.post(f"/cameras/{camera_id}/repair", headers=HX)
    assert r.status_code == 200 and "Đã tạo token ghép cặp mới" in r.text
    r = await client.post(f"/cameras/{camera_id}/remove", headers=HX)
    assert r.status_code == 200 and "Đã gỡ camera Phòng khách" in r.text
    assert (pki_settings.cameras_dir / camera_id / "disabled").exists()
    assert (await client.post(f"/cameras/{camera_id}/remove", headers=HX)).status_code == 404


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


async def test_a_paired_camera_shows_on_its_device_card_and_can_be_removed_there(client, pool, pki_settings):
    from helpers import make_csr_pem
    from loopi_brain import devices, pki
    await client.post("/cameras", data=FORM | {"has_audio": ""}, headers=HX)
    row = await fetch_one(pool, "SELECT id FROM network_camera")
    token = (await fetch_one(pool, "SELECT token FROM pair_token WHERE camera_id = %s", (row["id"],)))["token"]
    reg = await devices.register_device(
        pool, token, {"model": "x", "serial": "1", "agent_version": "0.1.0"},
        lambda device_id: pki.sign_csr(pki_settings.pki_dir, make_csr_pem(), device_id))
    page = (await client.get("/devices")).text
    assert "Camera chờ ghép cặp" not in page
    assert "Camera mạng: Camera IP (RTSP)" in page and "không ghi âm" in page and "S3cr3t" not in page
    assert f'hx-post="/cameras/{row["id"]}/remove"' in page and f'hx-target="#result-{reg["device_id"]}"' in page
    assert "commands/audio-on" not in page and "commands/audio-off" not in page  # a camera without sound
    assert "commands/pause" in page


async def test_the_form_warns_when_the_disk_would_fill(client, monkeypatch):
    from loopi_brain import capacity
    monkeypatch.setattr(capacity, "_free_bytes", lambda settings: 10 * 10**9)
    page = (await client.get("/devices")).text
    assert "vượt 85 % chỗ trống" in page

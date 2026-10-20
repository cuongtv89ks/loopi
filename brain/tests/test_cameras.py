import datetime as dt
import stat
import tomllib

import pytest

from helpers import fetch_one, make_csr_pem, create_token, HW
from loopi_brain import cameras, devices, pki

PASSWORD = "S3cr3t-pw"


def cam(**over) -> cameras.CameraInput:
    fields = dict(kind="ip", location="Phòng khách", video_url="rtsp://192.0.2.10:554/cam/realmonitor?channel=1&subtype=0",
                  audio_url="", username="admin", password=PASSWORD, has_audio=True)
    fields.update(over)
    return cameras.clean_input(**fields)


def config_of(settings, camera_id) -> dict:
    return tomllib.loads((settings.cameras_dir / camera_id / "loopi.toml").read_text())


async def test_a_camera_gets_a_mode_600_config_its_agent_can_run(pool, pki_settings):
    camera_id = await cameras.add_camera(pool, pki_settings, cam())
    path = pki_settings.cameras_dir / camera_id / "loopi.toml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    config = config_of(pki_settings, camera_id)
    assert config["brain"]["url"] == "https://proxy:8443"
    assert config["brain"]["ca_fingerprint"] == pki.fingerprint(pki.ca_cert_pem(pki_settings.pki_dir))
    token = config["brain"]["pair_token"]
    row = await fetch_one(pool, "SELECT camera_id, expires_at - now() AS ttl FROM pair_token WHERE token = %s", (token,))
    assert row["camera_id"] == camera_id and dt.timedelta(hours=23) < row["ttl"] <= dt.timedelta(hours=24)
    capture = config["capture"]
    assert capture["source"] == "stream" and capture["stream_url"].startswith("rtsp://192.0.2.10:554/")
    assert (capture["stream_username"], capture["stream_password"]) == ("admin", PASSWORD)
    assert capture["audio_enabled"] is True and capture["audio_gain_db"] == 0.0
    assert (capture["video_fps"], capture["video_bitrate_bps"]) == (2, 400000) and "audio_url" not in capture
    assert config["led"] == {"source": "none"}
    assert config["paths"]["data_dir"] == f"/srv/loopi/cameras/{camera_id}/data"
    assert config["schedule"]["timezone"] == pki_settings.tz


async def test_the_password_is_only_in_the_mode_600_file(pool, pki_settings):
    camera_id = await cameras.add_camera(pool, pki_settings, cam())
    row = await fetch_one(pool, "SELECT * FROM network_camera WHERE id = %s", (camera_id,))
    assert PASSWORD not in repr(row)
    assert row["username"] == "admin" and row["video_url"].startswith("rtsp://192.0.2.10")
    listed = await cameras.list_cameras(pool)
    assert PASSWORD not in repr(listed)


async def test_pairing_links_the_camera_and_names_its_place(pool, pki_settings):
    camera_id = await cameras.add_camera(pool, pki_settings, cam(location="Bếp"))
    token = config_of(pki_settings, camera_id)["brain"]["pair_token"]
    paired = await devices.register_device(pool, token, HW, lambda device_id: pki.sign_csr(pki_settings.pki_dir, make_csr_pem(), device_id))
    camera = await fetch_one(pool, "SELECT device_id FROM network_camera WHERE id = %s", (camera_id,))
    device = await fetch_one(pool, "SELECT location FROM device WHERE id = %s", (paired["device_id"],))
    assert camera["device_id"] == paired["device_id"] and device["location"] == "Bếp"


async def test_a_sound_url_and_a_camera_without_sound(pool, pki_settings):
    esp = await cameras.add_camera(pool, pki_settings, cam(kind="esp32", video_url="http://192.0.2.20/stream",
                                                            audio_url="http://192.0.2.20/audio", username="", password=""))
    assert config_of(pki_settings, esp)["capture"]["audio_url"] == "http://192.0.2.20/audio"
    silent = await cameras.add_camera(pool, pki_settings, cam(kind="esp32", video_url="http://192.0.2.21:81/stream",
                                                               username="", password="", has_audio=False))
    assert config_of(pki_settings, silent)["capture"]["audio_enabled"] is False


async def test_repairing_issues_a_new_token_and_keeps_the_password(pool, pki_settings):
    camera_id = await cameras.add_camera(pool, pki_settings, cam())
    first = config_of(pki_settings, camera_id)["brain"]["pair_token"]
    assert await cameras.repair_camera(pool, pki_settings, camera_id) is True
    config = config_of(pki_settings, camera_id)
    assert config["brain"]["pair_token"] != first and config["capture"]["stream_password"] == PASSWORD
    assert await cameras.repair_camera(pool, pki_settings, "01J0000000000000000000NOPE") is False


async def test_removing_a_camera_scrubs_the_password_and_revokes(pool, pki_settings):
    camera_id = await cameras.add_camera(pool, pki_settings, cam())
    token = config_of(pki_settings, camera_id)["brain"]["pair_token"]
    paired = await devices.register_device(pool, token, HW, lambda d: pki.sign_csr(pki_settings.pki_dir, make_csr_pem(), d))
    assert await cameras.remove_camera(pool, pki_settings, camera_id) is True
    camera_dir = pki_settings.cameras_dir / camera_id
    assert (camera_dir / "disabled").exists()
    assert config_of(pki_settings, camera_id)["capture"]["stream_password"] == ""
    assert (await fetch_one(pool, "SELECT revoked FROM device WHERE id = %s", (paired["device_id"],)))["revoked"] is True
    assert await cameras.list_cameras(pool) == []
    assert await cameras.remove_camera(pool, pki_settings, camera_id) is False  # already removed


@pytest.mark.parametrize(("over", "message"), [
    (dict(location="  "), "Tên vị trí không được để trống."),
    (dict(kind="webcam"), "Loại camera không hợp lệ."),
    (dict(video_url="rtsp://<IP>:554/cam"), "Thay <IP> bằng địa chỉ của camera."),
    (dict(video_url="ftp://192.0.2.10/x"), "Địa chỉ phải bắt đầu bằng rtsp://, http:// hoặc https://."),
    (dict(video_url="rtsp://admin:pw@192.0.2.10/x"), "Đừng ghi tên đăng nhập hay mật khẩu vào địa chỉ"),
    (dict(video_url="rtsp://192.0.2.10/a b"), "Địa chỉ không được có dấu cách."),
    (dict(audio_url="ftp://192.0.2.10/a"), "Địa chỉ âm thanh phải bắt đầu bằng http://, https:// hoặc rtsp://."),
])
def test_bad_camera_input_is_refused_with_a_vietnamese_reason(over, message):
    with pytest.raises(cameras.CameraError, match=message):
        cam(**over)


def test_credentials_are_percent_encoded_for_ffprobe():
    assert cameras.with_credentials("rtsp://192.0.2.10/cam", "admin", "p:w@") == "rtsp://admin:p%3Aw%40@192.0.2.10/cam"
    assert cameras.with_credentials("http://192.0.2.10/s", "", "") == "http://192.0.2.10/s"


async def test_a_failed_file_write_leaves_no_camera_behind(pool, pki_settings, monkeypatch):
    monkeypatch.setattr(cameras, "write_config", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        await cameras.add_camera(pool, pki_settings, cam())
    assert await fetch_one(pool, "SELECT count(*) AS n FROM network_camera") == {"n": 0}


def test_any_password_survives_the_round_trip_through_the_config_file():
    # JSON escapes a character outside the BMP as a surrogate pair, which TOML refuses: the agent could not parse it.
    password = 'p\u00e1ss "q" \\ \U0001F600 \x7f \t'
    text = cameras.render_config("01J0000000000000000000CAMS", cam(password=password), token="ABCD-EFGH",
                                 ca_fingerprint="aa", brain_url="https://proxy:8443", tz="Asia/Seoul")
    assert tomllib.loads(text)["capture"]["stream_password"] == password
    assert "\x7f" not in text and "\\u007f" in text  # a DEL character is escaped, TOML forbids it as it is


async def test_a_token_made_with_a_camera_id_links_that_camera_too(pool, pki_settings):
    camera_id = await cameras.add_camera(pool, pki_settings, cam(location="Sân"))
    token = (await devices.create_pair_token(pool, dt.timedelta(hours=1), camera_id))["token"]
    paired = await devices.register_device(pool, token, HW, lambda d: pki.sign_csr(pki_settings.pki_dir, make_csr_pem(), d))
    assert (await fetch_one(pool, "SELECT device_id FROM network_camera WHERE id = %s", (camera_id,)))["device_id"] == paired["device_id"]


async def test_an_ordinary_token_leaves_the_cameras_alone(pool, pki_settings):
    camera_id = await cameras.add_camera(pool, pki_settings, cam())
    paired = await devices.register_device(pool, await create_token(pool), HW,
                                           lambda d: pki.sign_csr(pki_settings.pki_dir, make_csr_pem(), d))
    assert (await fetch_one(pool, "SELECT device_id FROM network_camera WHERE id = %s", (camera_id,)))["device_id"] is None
    assert (await fetch_one(pool, "SELECT location, name FROM device WHERE id = %s", (paired["device_id"],)))["location"].startswith("loopi-")


async def test_the_app_creates_the_cameras_directory(client, pki_settings):
    assert pki_settings.cameras_dir.is_dir()

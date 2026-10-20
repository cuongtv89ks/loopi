import asyncio
import json
import logging
import os
import time

import pytest

from loopi_brain import camera_probe

STREAMS = {"streams": [
    {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "25/1"},
    {"codec_type": "audio", "codec_name": "aac"},
]}


def fake_ffprobe(tmp_path, monkeypatch, *, stdout="", stderr="", code=0, sleep=None):
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    script = fake / "ffprobe"
    body = f"echo \"$@\" > {tmp_path}/argv\nexec sleep {sleep}\n" if sleep else (
        f"echo \"$@\" > {tmp_path}/argv\nprintf '%s' '{stdout}'\nprintf '%s' '{stderr}' >&2\nexit {code}\n")
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")


async def test_a_stream_is_described(tmp_path, monkeypatch):
    fake_ffprobe(tmp_path, monkeypatch, stdout=json.dumps(STREAMS))
    result = await camera_probe.probe("rtsp://192.0.2.10/cam", "admin", "S3cr3t")
    assert result == camera_probe.ProbeResult(True, "Kết nối được: h264 1920×1080, 25 khung hình/giây, có âm thanh.",
                                              "h264", 1920, 1080, 25.0, True)
    argv = (tmp_path / "argv").read_text()
    assert "rtsp://admin:S3cr3t@192.0.2.10/cam" in argv and "-rtsp_transport tcp" in argv
    assert "-timeout 9000000 " in argv  # ffprobe's own limit is a second inside the probe's 10 s


@pytest.mark.parametrize(("stderr", "message"), [
    ("[rtsp @ 0x1] method DESCRIBE failed: 401 Unauthorized (rtsp://admin:S3cr3t@192.0.2.10/cam)",
     "Camera từ chối tên đăng nhập hoặc mật khẩu."),
    ("Server returned 404 Not Found", "Không tìm thấy luồng ở địa chỉ này."),
    ("Connection to tcp://192.0.2.10:554 failed: Connection refused", "Camera từ chối kết nối (kiểm tra cổng)."),
    ("Connection timed out", "Không kết nối được tới camera."),
    ("weird", "Không đọc được luồng từ camera."),
    ("[rtsp @ 0x1] method DESCRIBE failed: 403 Forbidden", "Camera từ chối tên đăng nhập hoặc mật khẩu."),
    ("[http @ 0x1] HTTP error 401 Authorization Required", "Camera từ chối tên đăng nhập hoặc mật khẩu."),
    # A pointer holding 401 is no answer from the camera, and a bare "not found" is no 404.
    ("[tcp @ 0x55d4014f8c80] Connection to tcp://192.0.2.10:554 failed: Connection refused",
     "Camera từ chối kết nối (kiểm tra cổng)."),
    ("[tcp @ 0x55d4044f8c80] Connection to tcp://192.0.2.10:554 failed: Connection refused",
     "Camera từ chối kết nối (kiểm tra cổng)."),
    ("Option not found", "Không đọc được luồng từ camera."),
])
async def test_ffprobe_errors_never_echo_the_address(tmp_path, monkeypatch, caplog, stderr, message):
    caplog.set_level(logging.DEBUG)
    fake_ffprobe(tmp_path, monkeypatch, stderr=stderr, code=1)
    result = await camera_probe.probe("rtsp://192.0.2.10/cam", "admin", "S3cr3t")
    assert result == camera_probe.ProbeResult(False, message)
    assert "S3cr3t" not in caplog.text and "192.0.2.10" not in caplog.text


async def test_a_camera_that_does_not_answer_in_time(tmp_path, monkeypatch):
    fake_ffprobe(tmp_path, monkeypatch, sleep=30)
    result = await camera_probe.probe("http://192.0.2.10/stream", "", "", timeout=0.3)
    assert result == camera_probe.ProbeResult(False, "Camera không trả lời trong 0.3 giây.")
    assert "-rw_timeout 1000000 " in (tmp_path / "argv").read_text()  # never below a second


@pytest.mark.parametrize(("timeout", "us"), [(10, "9000000"), (5, "4000000"), (1.5, "1000000")])
async def test_ffprobe_gives_up_a_second_before_the_probe(tmp_path, monkeypatch, timeout, us):
    fake_ffprobe(tmp_path, monkeypatch, stdout=json.dumps(STREAMS))
    await camera_probe.probe("rtsp://192.0.2.10/cam", "", "", timeout=timeout)
    assert f"-timeout {us} " in (tmp_path / "argv").read_text()
    await camera_probe.probe("http://192.0.2.10/stream", "", "", timeout=timeout)
    assert f"-rw_timeout {us} " in (tmp_path / "argv").read_text()
    assert camera_probe.PROBE_TIMEOUT_S == 10  # D36: Thử kết nối never takes longer


@pytest.mark.parametrize("stdout", ["null", "[]", '{"streams": null}', '{"streams": [1]}', '{"streams": {}}', "{"])
async def test_output_that_is_not_a_list_of_streams_is_not_read(tmp_path, monkeypatch, stdout):
    fake_ffprobe(tmp_path, monkeypatch, stdout=stdout)
    result = await camera_probe.probe("http://192.0.2.10/stream", "", "")
    assert result == camera_probe.ProbeResult(False, "Không đọc được luồng từ camera.")


async def test_output_that_is_not_utf8_is_not_read(tmp_path, monkeypatch):
    fake_ffprobe(tmp_path, monkeypatch)
    # printf's octal escape: the bytes C3 28, which no UTF-8 decoder takes.
    (tmp_path / "bin" / "ffprobe").write_text("#!/bin/sh\nprintf '{\"streams\": \"\\303(\"}'\n")
    result = await camera_probe.probe("http://192.0.2.10/stream", "", "")
    assert result == camera_probe.ProbeResult(False, "Không đọc được luồng từ camera.")


async def test_the_description_names_only_what_ffprobe_found(tmp_path, monkeypatch):
    fake_ffprobe(tmp_path, monkeypatch, stdout=json.dumps({"streams": [{"codec_type": "video", "width": 0}]}))
    result = await camera_probe.probe("http://192.0.2.10/stream", "", "")
    assert result == camera_probe.ProbeResult(
        True, "Kết nối được: không rõ kích thước, không rõ số khung hình/giây, không có âm thanh.", has_audio=False)
    fake_ffprobe(tmp_path, monkeypatch, stdout=json.dumps({"streams": [
        {"codec_type": "video", "codec_name": "mjpeg", "width": 640, "avg_frame_rate": "10/1"}]}))
    result = await camera_probe.probe("http://192.0.2.10/stream", "", "")
    assert result.message == "Kết nối được: mjpeg, không rõ kích thước, 10 khung hình/giây, không có âm thanh."
    assert (result.codec, result.width, result.height) == ("mjpeg", None, None)
    fake_ffprobe(tmp_path, monkeypatch, stdout=json.dumps({"streams": [
        {"codec_type": "video", "width": 1280, "height": 720, "avg_frame_rate": "25/1"}]}))
    result = await camera_probe.probe("http://192.0.2.10/stream", "", "")
    assert result.message == "Kết nối được: 1280×720, 25 khung hình/giây, không có âm thanh."
    assert "None" not in result.message


async def test_a_stream_without_a_picture(tmp_path, monkeypatch):
    fake_ffprobe(tmp_path, monkeypatch, stdout=json.dumps({"streams": [{"codec_type": "audio", "codec_name": "pcm_s16le"}]}))
    result = await camera_probe.probe("http://192.0.2.10/audio", "", "")
    assert result == camera_probe.ProbeResult(False, "Luồng này không có hình.", has_audio=True)


async def test_mjpeg_over_http_has_no_rtsp_flags(tmp_path, monkeypatch):
    fake_ffprobe(tmp_path, monkeypatch, stdout=json.dumps({"streams": [
        {"codec_type": "video", "codec_name": "mjpeg", "width": 800, "height": 600, "avg_frame_rate": "0/0",
         "r_frame_rate": "10/1"}]}))
    result = await camera_probe.probe("http://192.0.2.20:81/stream", "", "")
    assert (result.ok, result.fps, result.has_audio) == (True, 10.0, False)
    assert "-rtsp_transport" not in (tmp_path / "argv").read_text()


async def test_cancelling_the_probe_kills_ffprobe(tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    pid_file = tmp_path / "pid"
    script = fake / "ffprobe"
    script.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 30\n")  # exec: the shell's pid is sleep's
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")
    task = asyncio.create_task(camera_probe.probe("http://192.0.2.10/stream", "", ""))
    deadline = time.monotonic() + 2
    while not pid_file.exists() or not pid_file.read_text().strip():
        assert time.monotonic() < deadline, "the fake ffprobe never started"
        await asyncio.sleep(0.02)
    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    deadline = time.monotonic() + 2
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        assert time.monotonic() < deadline, "ffprobe is still running after the probe was cancelled"
        await asyncio.sleep(0.02)

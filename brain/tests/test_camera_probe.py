import asyncio
import json
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
    body = f"exec sleep {sleep}\n" if sleep else (
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


@pytest.mark.parametrize(("stderr", "message"), [
    ("[rtsp @ 0x1] method DESCRIBE failed: 401 Unauthorized (rtsp://admin:S3cr3t@192.0.2.10/cam)",
     "Camera từ chối tên đăng nhập hoặc mật khẩu."),
    ("Server returned 404 Not Found", "Không tìm thấy luồng ở địa chỉ này."),
    ("Connection to tcp://192.0.2.10:554 failed: Connection refused", "Camera từ chối kết nối (kiểm tra cổng)."),
    ("Connection timed out", "Không kết nối được tới camera."),
    ("weird", "Không đọc được luồng từ camera."),
])
async def test_ffprobe_errors_never_echo_the_address(tmp_path, monkeypatch, caplog, stderr, message):
    fake_ffprobe(tmp_path, monkeypatch, stderr=stderr, code=1)
    result = await camera_probe.probe("rtsp://192.0.2.10/cam", "admin", "S3cr3t")
    assert result == camera_probe.ProbeResult(False, message)
    assert "S3cr3t" not in caplog.text and "192.0.2.10" not in caplog.text


async def test_a_camera_that_does_not_answer_in_time(tmp_path, monkeypatch):
    fake_ffprobe(tmp_path, monkeypatch, sleep=30)
    result = await camera_probe.probe("http://192.0.2.10/stream", "", "", timeout=0.3)
    assert result == camera_probe.ProbeResult(False, "Camera không trả lời trong 10 giây.")


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

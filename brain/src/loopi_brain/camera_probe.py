"""'Thử kết nối' (D36): ffprobe a camera's stream for at most PROBE_TIMEOUT_S seconds and say what it carries. ffprobe's
error output quotes the address with the password, so it is never logged or shown: it is reduced to a fixed message."""
from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass

from .cameras import with_credentials

PROBE_TIMEOUT_S = 10
TIMEOUT_US = str(PROBE_TIMEOUT_S * 1_000_000)


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    message: str
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    has_audio: bool | None = None


def _args(url: str) -> list[str]:
    reading = ["-rtsp_transport", "tcp", "-timeout", TIMEOUT_US] if url.startswith("rtsp://") else ["-rw_timeout", TIMEOUT_US]
    return ["-v", "error", *reading, "-show_entries",
            "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate", "-of", "json", url]


def _rate(text: str | None) -> float | None:
    try:
        num, den = (text or "").split("/")
        return float(num) / float(den) if float(den) else None
    except ValueError:
        return None


def _failure(stderr: str) -> str:
    text = stderr.lower()
    if any(k in text for k in ("401", "403", "unauthorized", "authorization failed")):
        return "Camera từ chối tên đăng nhập hoặc mật khẩu."
    if "404" in text or "not found" in text:
        return "Không tìm thấy luồng ở địa chỉ này."
    if "connection refused" in text:
        return "Camera từ chối kết nối (kiểm tra cổng)."
    if any(k in text for k in ("timed out", "timeout", "no route to host", "unreachable")):
        return "Không kết nối được tới camera."
    return "Không đọc được luồng từ camera."


async def probe(url: str, username: str, password: str, *, timeout: float = PROBE_TIMEOUT_S) -> ProbeResult:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", *_args(with_credentials(url, username, password)),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        return ProbeResult(False, "Camera không trả lời trong 10 giây.")
    finally:
        # Timed out, or the awaiting task was cancelled (the browser closed the request, the server is stopping): the
        # process must not outlive the probe. A finished process has its returncode set, so this is a no-op then.
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        return ProbeResult(False, _failure(stderr.decode(errors="replace")))
    try:
        streams = json.loads(stdout or b"{}").get("streams", [])
    except json.JSONDecodeError:
        return ProbeResult(False, "Không đọc được luồng từ camera.")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return ProbeResult(False, "Luồng này không có hình.", has_audio=has_audio)
    fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
    sound = "có âm thanh" if has_audio else "không có âm thanh"
    shown_fps = f"{fps:g} khung hình/giây" if fps else "không rõ số khung hình/giây"
    message = f"Kết nối được: {video.get('codec_name')} {video.get('width')}×{video.get('height')}, {shown_fps}, {sound}."
    return ProbeResult(True, message, video.get("codec_name"), video.get("width"), video.get("height"), fps, has_audio)

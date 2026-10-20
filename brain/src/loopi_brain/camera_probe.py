"""'Thử kết nối' (D36): ffprobe a camera's stream for at most PROBE_TIMEOUT_S seconds and say what it carries. ffprobe's
error output quotes the address with the password, so it is never logged or shown: it is reduced to a fixed message."""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
from dataclasses import dataclass

from .cameras import with_credentials

PROBE_TIMEOUT_S = 10
UNREADABLE = "Không đọc được luồng từ camera."
_AUTH = re.compile(r"\b40[13]\b")
_NOT_FOUND = re.compile(r"\b404\b")


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    message: str
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    has_audio: bool | None = None


def _io_timeout_us(timeout: float) -> str:
    """ffprobe's own I/O limit: a second inside the probe's, never below a second, so ffprobe gives up and answers
    (refused, not found …) before the probe stops waiting."""
    return str(int(max(timeout - 1, 1) * 1_000_000))


def _args(url: str, timeout: float) -> list[str]:
    limit = _io_timeout_us(timeout)
    reading = ["-rtsp_transport", "tcp", "-timeout", limit] if url.startswith("rtsp://") else ["-rw_timeout", limit]
    return ["-v", "error", *reading, "-show_entries",
            "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate", "-of", "json", url]


def _rate(text: str | None) -> float | None:
    try:
        num, den = (text or "").split("/")
        return float(num) / float(den) if float(den) else None
    except ValueError:
        return None


def _failure(stderr: str) -> str:
    # Whole numbers only: ffmpeg's log lines start with a pointer ("[tcp @ 0x55d4014f8c80]") that may hold 401 or 404.
    text = stderr.lower()
    if _AUTH.search(text) or "unauthorized" in text or "authorization failed" in text:
        return "Camera từ chối tên đăng nhập hoặc mật khẩu."
    if _NOT_FOUND.search(text):
        return "Không tìm thấy luồng ở địa chỉ này."
    if "connection refused" in text:
        return "Camera từ chối kết nối (kiểm tra cổng)."
    if any(k in text for k in ("timed out", "timeout", "no route to host", "unreachable")):
        return "Không kết nối được tới camera."
    return UNREADABLE


def _streams(stdout: bytes) -> list[dict] | None:
    """ffprobe's `streams`, or None when the output is not JSON (or not UTF-8) holding a list of objects."""
    try:
        data = json.loads(stdout or b"{}")
    except ValueError:  # JSONDecodeError and UnicodeDecodeError both are
        return None
    streams = data.get("streams", []) if isinstance(data, dict) else None
    if not isinstance(streams, list) or not all(isinstance(s, dict) for s in streams):
        return None
    return streams


def _positive_int(value) -> int | None:
    return value if type(value) is int and value > 0 else None


async def probe(url: str, username: str, password: str, *, timeout: float = PROBE_TIMEOUT_S) -> ProbeResult:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", *_args(with_credentials(url, username, password), timeout),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        return ProbeResult(False, f"Camera không trả lời trong {timeout:g} giây.")
    finally:
        # Timed out, or the awaiting task was cancelled (the browser closed the request, the server is stopping): the
        # process must not outlive the probe. A finished process has its returncode set, so this is a no-op then.
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        return ProbeResult(False, _failure(stderr.decode(errors="replace")))
    streams = _streams(stdout)
    if streams is None:
        return ProbeResult(False, UNREADABLE)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return ProbeResult(False, "Luồng này không có hình.", has_audio=has_audio)
    codec = video.get("codec_name")
    codec = codec if isinstance(codec, str) and codec else None
    width, height = _positive_int(video.get("width")), _positive_int(video.get("height"))
    if not (width and height):
        width = height = None
    fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
    # Only what ffprobe found: no "None", no "0×0".
    size = f"{width}×{height}" if width else None
    head = " ".join(p for p in (codec, size) if p)
    parts = ([head] if head else []) + ([] if size else ["không rõ kích thước"])
    parts.append(f"{fps:g} khung hình/giây" if fps else "không rõ số khung hình/giây")
    parts.append("có âm thanh" if has_audio else "không có âm thanh")
    return ProbeResult(True, f"Kết nối được: {', '.join(parts)}.", codec, width, height, fps, has_audio)

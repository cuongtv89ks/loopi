"""Network cameras (P3c design §2, §4.1, D30, D36). The Thiết bị form adds one: the brain stores what the form said
(never the password), creates a pair token tied to it (24 h, R6) and writes `<cameras_dir>/<id>/loopi.toml` (mode
600) for the cameras container, whose supervisor starts an agent for it within seconds. Removing a camera writes a
`disabled` file (its agent stops), rewrites the file without the password (R9), and revokes its device; what it
recorded stays until retention or "Quên"."""
from __future__ import annotations

import datetime as dt
import json
import os
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

from . import devices, media, pki, places
from .config import Settings

CAMERA_TOKEN_TTL = dt.timedelta(hours=24)
CONTAINER_ROOT = "/srv/loopi/cameras"  # the same path inside the cameras container
CONFIG_FILE = "loopi.toml"
DISABLED_FILE = "disabled"
URL_MAX = 1000
VIDEO_FPS = 2
VIDEO_BITRATE_BPS = 400_000


@dataclass(frozen=True)
class CameraKind:
    label: str
    video_template: str
    username: str
    has_audio: bool
    hint: str


KINDS: dict[str, CameraKind] = {
    "ip": CameraKind("Camera IP (RTSP)", "rtsp://<IP>:554/cam/realmonitor?channel=1&subtype=0", "admin", True,
                     "Camera Imou: tên đăng nhập admin, mật khẩu là mã an toàn in dưới đáy camera."),
    "phone": CameraKind("Điện thoại — iPhone/Android (app phát RTSP hoặc MJPEG)", "http://<IP>:8080/video", "", True,
                        "Android \"IP Webcam\": http://<IP>:8080/video. iPhone: dùng địa chỉ RTSP mà app hiện ra."),
    "esp32": CameraKind("ESP32 có camera (MJPEG qua HTTP)", "http://<IP>:81/stream", "", False,
                        "Firmware có sẵn: http://<IP>:81/stream, không có âm thanh. Firmware Loopi: http://<IP>/stream"
                        " và âm thanh ở http://<IP>/audio."),
    "other": CameraKind("Khác (URL RTSP/HTTP tuỳ ý)", "", "", True, "Dán địa chỉ RTSP hoặc HTTP của luồng."),
}


class CameraError(ValueError):
    """Input the form refuses; the message is shown to the owner as it is."""


@dataclass(frozen=True)
class CameraInput:
    kind: str
    location: str
    video_url: str
    audio_url: str | None
    username: str
    password: str
    has_audio: bool

    def __repr__(self) -> str:  # never the password
        return f"CameraInput(kind={self.kind!r}, location={self.location!r}, video_url={self.video_url!r})"


def _clean_url(text: str, *, schemes: tuple[str, ...], what: str) -> str:
    url = text.strip()
    if not url.startswith(schemes):
        joined = ", ".join(schemes[:-1]) + f" hoặc {schemes[-1]}"
        raise CameraError(f"{what} phải bắt đầu bằng {joined}.")
    if "<" in url or ">" in url:
        raise CameraError("Thay <IP> bằng địa chỉ của camera.")
    if any(c.isspace() for c in url) or not url.isprintable():
        raise CameraError("Địa chỉ không được có dấu cách.")
    if len(url) > URL_MAX:
        raise CameraError(f"Địa chỉ dài quá {URL_MAX} ký tự.")
    authority = url.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0]
    if "@" in authority:
        raise CameraError("Đừng ghi tên đăng nhập hay mật khẩu vào địa chỉ; điền chúng vào ô bên dưới.")
    if not authority:
        raise CameraError("Địa chỉ không có tên máy hoặc IP.")
    return url


def clean_input(*, kind: str, location: str, video_url: str, audio_url: str, username: str, password: str,
                has_audio: bool) -> CameraInput:
    if kind not in KINDS:
        raise CameraError("Loại camera không hợp lệ.")
    try:
        place = places.clean_location(location)
    except places.LocationError as exc:
        raise CameraError(str(exc)) from exc
    video = _clean_url(video_url, schemes=("rtsp://", "http://", "https://"), what="Địa chỉ")
    audio = _clean_url(audio_url, schemes=("http://", "https://", "rtsp://"), what="Địa chỉ âm thanh") if audio_url.strip() else None
    return CameraInput(kind, place, video, audio, username.strip(), password, bool(has_audio))


def with_credentials(url: str, username: str, password: str) -> str:
    """The address with `user:password@` for ffprobe and ffmpeg, both percent-encoded; as it is without a user."""
    if not username:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{urllib.parse.quote(username, safe='')}:{urllib.parse.quote(password, safe='')}@{rest}"


def _toml(value) -> str:
    """A TOML value: JSON strings are TOML basic strings (kept as UTF-8: JSON's surrogate-pair escapes for a character
    outside the BMP are not valid TOML, and TOML wants a DEL character escaped); bools and numbers as TOML writes them."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def render_config(camera_id: str, cam: CameraInput, *, token: str | None, ca_fingerprint: str, brain_url: str,
                  tz: str) -> str:
    brain = {"url": brain_url, "ca_fingerprint": ca_fingerprint} | ({"pair_token": token} if token else {})
    capture = {
        "source": "stream", "stream_url": cam.video_url, "stream_username": cam.username,
        "stream_password": cam.password, "audio_enabled": cam.has_audio, "audio_gain_db": 0.0,
        "video_fps": VIDEO_FPS, "video_bitrate_bps": VIDEO_BITRATE_BPS,
    } | ({"audio_url": cam.audio_url} if cam.audio_url else {})
    sections = {
        "brain": brain, "capture": capture, "schedule": {"timezone": tz}, "led": {"source": "none"},
        "paths": {"data_dir": f"{CONTAINER_ROOT}/{camera_id}/data"},
    }
    lines = ["# Written by the brain (P3c-2) for the cameras container. Holds a password: mode 600, never commit."]
    for name, table in sections.items():
        lines.append(f"\n[{name}]")
        lines.extend(f"{key} = {_toml(value)}" for key, value in table.items())
    return "\n".join(lines) + "\n"


def write_config(cameras_dir: Path, camera_id: str, text: str) -> None:
    """Atomically: a mode-600 temp file, fsync, rename, fsync of the directory."""
    directory = cameras_dir / camera_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = directory / f".{CONFIG_FILE}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, directory / CONFIG_FILE)
    media.fsync_path(directory)


def _stored_password(settings: Settings, camera_id: str) -> str:
    path = settings.cameras_dir / camera_id / CONFIG_FILE
    try:
        return tomllib.loads(path.read_text())["capture"].get("stream_password", "")
    except (OSError, tomllib.TOMLDecodeError, KeyError):
        return ""


def _input_from_row(row: dict, password: str) -> CameraInput:
    return CameraInput(row["kind"], row["location"], row["video_url"], row["audio_url"], row["username"], password,
                       row["has_audio"])


async def _token(conn, camera_id: str) -> str:
    cur = await conn.execute(
        "INSERT INTO pair_token (token, expires_at, camera_id) VALUES (%s, now() + %s, %s) RETURNING token",
        (devices._new_token(), CAMERA_TOKEN_TTL, camera_id),
    )
    return (await cur.fetchone())["token"]


def _fingerprint(settings: Settings) -> str:
    return pki.fingerprint(pki.ca_cert_pem(settings.pki_dir))


async def add_camera(pool: AsyncConnectionPool, settings: Settings, cam: CameraInput) -> str:
    camera_id = devices.new_id()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO network_camera (id, kind, location, video_url, audio_url, username, has_audio)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (camera_id, cam.kind, cam.location, cam.video_url, cam.audio_url, cam.username, cam.has_audio),
        )
        token = await _token(conn, camera_id)
        # Written before the commit: a failed write rolls the camera and its token back.
        write_config(settings.cameras_dir, camera_id, render_config(
            camera_id, cam, token=token, ca_fingerprint=_fingerprint(settings), brain_url=settings.camera_brain_url,
            tz=settings.tz))
    return camera_id


async def repair_camera(pool: AsyncConnectionPool, settings: Settings, camera_id: str) -> bool:
    """A new 24 h token for a camera that has not paired yet; the file keeps its password; the supervisor restarts
    the agent because the file changed."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "SELECT * FROM network_camera WHERE id = %s AND removed_at IS NULL AND device_id IS NULL FOR UPDATE",
            (camera_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        token = await _token(conn, camera_id)
        write_config(settings.cameras_dir, camera_id, render_config(
            camera_id, _input_from_row(row, _stored_password(settings, camera_id)), token=token,
            ca_fingerprint=_fingerprint(settings), brain_url=settings.camera_brain_url, tz=settings.tz))
    return True


async def remove_camera(pool: AsyncConnectionPool, settings: Settings, camera_id: str) -> bool:
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "UPDATE network_camera SET removed_at = now() WHERE id = %s AND removed_at IS NULL RETURNING *",
            (camera_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        directory = settings.cameras_dir / camera_id
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        (directory / DISABLED_FILE).touch()  # first: the supervisor stops the agent within one scan
        write_config(settings.cameras_dir, camera_id, render_config(
            camera_id, _input_from_row(row, ""), token=None, ca_fingerprint=_fingerprint(settings),
            brain_url=settings.camera_brain_url, tz=settings.tz))
        if row["device_id"]:
            await conn.execute("UPDATE device SET revoked = TRUE WHERE id = %s", (row["device_id"],))
    return True


async def list_cameras(pool: AsyncConnectionPool) -> list[dict]:
    """Cameras not removed, oldest first, with their device's last heartbeat (no password anywhere)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT c.id, c.kind, c.location, c.video_url, c.audio_url, c.username, c.has_audio, c.device_id,"
            " c.created_at, d.last_heartbeat, d.last_heartbeat_at FROM network_camera c"
            " LEFT JOIN device d ON d.id = c.device_id WHERE c.removed_at IS NULL ORDER BY c.created_at, c.id"
        )
        return await cur.fetchall()

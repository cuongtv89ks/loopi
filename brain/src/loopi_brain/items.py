"""Ingest of uploaded segments: header validation, streaming write, idempotency."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets
import shutil
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import anyio
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from . import volume
from .config import Settings
from .media import fsync_path

KIND_CONTENT_TYPES = {"video": "video/h264", "audio": "audio/ogg", "marker": "application/json"}
KIND_EXT = {"video": "h264", "audio": "opus", "marker": "json"}
ULID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
MARKER_MAX_BYTES = 64 * 1024
MAX_SEGMENT_DURATION = dt.timedelta(hours=1)
FPS_RANGE = range(1, 31)


@dataclass(frozen=True)
class ItemHeader:
    id: str
    kind: str
    started_at: dt.datetime
    ended_at: dt.datetime
    sha256: str
    content_type: str
    video_fps: int | None


class ItemRejected(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _reject(detail: str, status: int = 422) -> ItemRejected:
    return ItemRejected(status, detail)


def _parse_time(name: str, value: str | None) -> dt.datetime:
    if not value:
        raise _reject(f"missing {name}")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise _reject(f"{name} is not RFC 3339") from exc
    if parsed.tzinfo is None:
        raise _reject(f"{name} must include a timezone")
    return parsed.astimezone(dt.UTC)


def parse_header(item_id: str, headers: Mapping[str, str], default_fps: int) -> ItemHeader:
    if not ULID_RE.fullmatch(item_id):
        raise _reject("item id must be a ULID")
    kind = headers.get("x-loopi-kind", "")
    if kind not in KIND_CONTENT_TYPES:
        raise _reject(f"unknown kind {kind!r}")
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != KIND_CONTENT_TYPES[kind]:
        raise _reject(f"content-type for {kind} must be {KIND_CONTENT_TYPES[kind]}")
    started = _parse_time("X-Loopi-Start", headers.get("x-loopi-start"))
    ended = _parse_time("X-Loopi-End", headers.get("x-loopi-end"))
    if ended < started:
        raise _reject("X-Loopi-End is before X-Loopi-Start")
    if ended - started > MAX_SEGMENT_DURATION:
        raise _reject("segment longer than 1 hour")
    sha = headers.get("x-loopi-sha256", "")
    if not SHA_RE.fullmatch(sha):
        raise _reject("X-Loopi-Sha256 must be 64 lowercase hex chars")
    fps = None
    if kind == "video":
        raw_fps = headers.get("x-loopi-video-fps")
        try:
            fps = int(raw_fps) if raw_fps is not None else default_fps
        except ValueError as exc:
            raise _reject("X-Loopi-Video-Fps must be an integer") from exc
        if fps not in FPS_RANGE:
            raise _reject("X-Loopi-Video-Fps must be 1..30")
    return ItemHeader(item_id, kind, started, ended, sha, content_type, fps)


def media_rel_path(device_id: str, header: ItemHeader) -> PurePosixPath:
    return PurePosixPath(
        "raw", device_id, header.started_at.strftime("%Y/%m/%d"), f"{header.id}.{KIND_EXT[header.kind]}"
    )


async def _existing(conn, item_id: str) -> dict | None:
    cur = await conn.execute("SELECT sha256, device_id FROM item WHERE id = %s", (item_id,))
    return await cur.fetchone()


def _duplicate_or_conflict(existing: dict, device_id: str, header: ItemHeader) -> tuple[int, dict]:
    if existing["sha256"] == header.sha256 and existing["device_id"] == device_id:
        return 200, {"id": header.id, "status": "duplicate"}
    raise _reject("item id already exists with different content", status=409)


async def _write_body(tmp: Path, body: AsyncIterator[bytes], limit: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    async with await anyio.open_file(tmp, "wb") as out:
        async for chunk in body:
            size += len(chunk)
            if size > limit:
                raise _reject(f"item larger than {limit} bytes", status=413)
            digest.update(chunk)
            await out.write(chunk)
    return digest.hexdigest(), size


def _has_nul(value) -> bool:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if "\x00" in item:
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _marker_meta(path: Path, size: int) -> dict:
    if size > MARKER_MAX_BYTES:
        raise _reject(f"marker larger than {MARKER_MAX_BYTES} bytes", status=413)
    try:
        meta = json.loads(path.read_bytes() or b"{}")
    except (ValueError, RecursionError) as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise _reject("marker body is not valid UTF-8 JSON") from exc
    if not isinstance(meta, dict):
        raise _reject("marker body must be a JSON object")
    if _has_nul(meta):
        raise _reject("marker body contains a NUL character")
    return meta


async def store_item(
    pool: AsyncConnectionPool,
    settings: Settings,
    device_id: str,
    header: ItemHeader,
    body: AsyncIterator[bytes],
) -> tuple[int, dict]:
    """Store one item. Returns (201|200, ack) or raises ItemRejected."""
    async with pool.connection() as conn:
        existing = await _existing(conn, header.id)
    if existing is not None:
        return _duplicate_or_conflict(existing, device_id, header)

    if not volume.ready(settings.media_root, settings.media_require_marker):
        raise _reject("brain storage is not mounted", status=507)  # the agent keeps the item queued and retries
    settings.media_root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(settings.media_root).free < settings.min_free_bytes:
        raise _reject("brain storage is full", status=507)

    rel = media_rel_path(device_id, header)
    final = settings.media_root / rel
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f".{final.name}.{secrets.token_hex(4)}.part")
    try:
        sha, size = await _write_body(tmp, body, settings.max_item_bytes)
        if sha != header.sha256:
            raise _reject("sha256 mismatch: body corrupted or truncated")
        meta: dict = {}
        status, view_path = "ready", str(rel)
        if header.kind == "video":
            meta, status, view_path = {"video_fps": header.video_fps}, "stored", None
        elif header.kind == "marker":
            meta, view_path = _marker_meta(tmp, size), None

        await anyio.to_thread.run_sync(fsync_path, tmp)
        async with pool.connection() as conn:
            async with conn.transaction():
                # Serialise concurrent uploads of the same id.
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (header.id,))
                existing = await _existing(conn, header.id)
                if existing is not None:
                    return _duplicate_or_conflict(existing, device_id, header)
                tmp.replace(final)
                try:
                    await anyio.to_thread.run_sync(fsync_path, final.parent)
                    await conn.execute(
                        "INSERT INTO item (id, device_id, kind, started_at, ended_at, sha256, size_bytes,"
                        " raw_path, view_path, meta, status)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (header.id, device_id, header.kind, header.started_at, header.ended_at,
                         header.sha256, size, str(rel), view_path, Jsonb(meta), status),
                    )
                except BaseException:
                    # Advisory lock still held: no concurrent upload of this id can have renamed yet.
                    final.unlink(missing_ok=True)
                    raise
    # A commit failure (after the lock is gone) deliberately leaves `final` in place: it may belong
    # to a committed row or a concurrent upload, and a retry simply overwrites any stale file.
    finally:
        tmp.unlink(missing_ok=True)
    return 201, {"id": header.id, "status": "created"}

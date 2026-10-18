"""OTA update records (P4a design §6-§8, spec D27): what the owner asked a device to install, and what the device last
said about it. The device is the source of truth: its report wins, even over a timeout the brain declared."""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from . import bundles, devices

log = logging.getLogger(__name__)
TERMINAL = ("succeeded", "rolled_back", "failed")
STALE_AFTER = dt.timedelta(minutes=30)
HISTORY = 5
# Owner-facing (Vietnamese)
TIMEOUT_ERROR = "Thiết bị không phản hồi trong 30 phút."
BUSY = "Thiết bị đang có một lần cập nhật chưa xong."
NOT_FOUND = "Không tìm thấy bản cập nhật này."
REVOKED = "Thiết bị đã bị thu hồi."
IN_USE = "Đang có một lần cập nhật dùng bản này."


class UpdateRefused(Exception):
    """The update cannot start; the message (Vietnamese) says why."""


class DeleteRefused(Exception):
    """The bundle must stay; the message (Vietnamese) says why."""


async def expire_stale(pool: AsyncConnectionPool) -> list[str]:
    """Give up on updates no report has touched for STALE_AFTER, and withdraw their commands if still unsent."""
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "UPDATE ota_update SET state = 'failed', error = %s, finished_at = now()"
                " WHERE finished_at IS NULL AND updated_at < now() - %s RETURNING id",
                (TIMEOUT_ERROR, STALE_AFTER),
            )
            ids = [row["id"] for row in await cur.fetchall()]
            if ids:
                # A device that comes back later must never start an update the brain gave up on.
                await conn.execute("DELETE FROM device_command WHERE id = ANY(%s) AND acked_at IS NULL", (ids,))
    for update_id in ids:
        log.warning("OTA update %s expired: no report for %s", update_id, STALE_AFTER)
    return ids


async def request_update(pool: AsyncConnectionPool, ota_dir: Path, device_id: str, bundle_name: str) -> str:
    """Queue an update command for one device (spec D27). Raises UpdateRefused, or LookupError for an unknown device."""
    bundle = bundles.find(ota_dir, bundle_name)
    if bundle is None:
        raise UpdateRefused(NOT_FOUND)
    await expire_stale(pool)
    payload = {"bundle": bundle.name, "version": bundle.version, "variant": bundle.variant,
               "sha256": bundle.sha256, "size": bundle.size}
    devices.validate_command("update", payload)
    command_id = devices.new_id()
    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute("SELECT revoked FROM device WHERE id = %s FOR UPDATE", (device_id,))
                device = await cur.fetchone()
                if device is None:
                    raise LookupError(device_id)
                if device["revoked"]:
                    raise UpdateRefused(REVOKED)
                cur = await conn.execute(
                    "SELECT 1 FROM ota_update WHERE device_id = %s AND finished_at IS NULL", (device_id,))
                if await cur.fetchone() is not None:
                    raise UpdateRefused(BUSY)
                await conn.execute(
                    "INSERT INTO device_command (id, device_id, type, payload) VALUES (%s, %s, 'update', %s)",
                    (command_id, device_id, Jsonb(payload)),
                )
                await conn.execute(
                    "INSERT INTO ota_update (id, device_id, bundle, version, variant) VALUES (%s, %s, %s, %s, %s)",
                    (command_id, device_id, bundle.name, bundle.version, bundle.variant),
                )
    except psycopg.errors.UniqueViolation as exc:  # a concurrent request for this device won the race
        raise UpdateRefused(BUSY) from exc
    log.info("OTA update %s: device %s -> %s", command_id, device_id, bundle.name)
    return command_id


async def record_report(pool: AsyncConnectionPool, device_id: str, report: dict) -> None:
    """Record the device's word about one of its own updates. Reviving an update the brain timed out never makes a
    second unfinished update for the device; a final state always lands."""
    command_id = report.get("command_id")
    if not command_id:
        return
    params = {"state": report["state"], "error": report.get("error"), "terminal": report["state"] in TERMINAL,
              "id": command_id, "device": device_id}
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE ota_update u SET state = %(state)s, error = %(error)s, updated_at = now(),"
            " finished_at = CASE WHEN %(terminal)s THEN COALESCE(u.finished_at, now()) ELSE NULL END"
            " WHERE u.id = %(id)s AND u.device_id = %(device)s"
            " AND (%(terminal)s OR u.finished_at IS NULL OR NOT EXISTS ("
            "   SELECT 1 FROM ota_update o WHERE o.device_id = u.device_id AND o.finished_at IS NULL AND o.id <> u.id))",
            params,
        )


async def recent_updates(pool: AsyncConnectionPool, device_ids: list[str]) -> dict[str, list[dict]]:
    """The last HISTORY updates of each device, newest first."""
    if not device_ids:
        return {}
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT * FROM (SELECT u.*, row_number() OVER ("
            "   PARTITION BY device_id ORDER BY requested_at DESC, id DESC) AS n"
            " FROM ota_update u WHERE device_id = ANY(%s)) r"
            " WHERE n <= %s ORDER BY device_id, requested_at DESC, id DESC",
            (device_ids, HISTORY),
        )
        rows = await cur.fetchall()
    history: dict[str, list[dict]] = {device_id: [] for device_id in device_ids}
    for row in rows:
        history[row["device_id"]].append(row)
    return history


async def delete_bundle(pool: AsyncConnectionPool, ota_dir: Path, stem: str) -> None:
    """Delete a bundle and its description (spec D29). Raises LookupError for an unknown name, DeleteRefused while an
    unfinished update uses it or a device runs that version."""
    if not bundles.STEM_RE.fullmatch(stem):
        raise LookupError(stem)
    name = f"{stem}.raucb"
    paths = (ota_dir / name, bundles.description_path(ota_dir, name))
    if not any(path.exists() for path in paths):
        raise LookupError(stem)
    await expire_stale(pool)
    bundle = bundles.find(ota_dir, name)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM ota_update WHERE bundle = %s AND finished_at IS NULL", (name,))
        if await cur.fetchone() is not None:
            raise DeleteRefused(IN_USE)
        if bundle is not None:
            cur = await conn.execute(
                "SELECT name FROM device WHERE NOT revoked AND last_heartbeat->>'os_version' = %s"
                " AND last_heartbeat->>'os_variant' = %s ORDER BY name",
                (bundle.version, bundle.variant),
            )
            running = [row["name"] for row in await cur.fetchall()]
            if running:
                raise DeleteRefused(f"Thiết bị {', '.join(running)} đang chạy bản này.")
    for path in paths:
        path.unlink(missing_ok=True)
    log.info("deleted OTA bundle %s", stem)

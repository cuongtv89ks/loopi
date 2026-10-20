"""How much disk the next days of recording take (P3c design §4.1, §5): every active device's bytes of the last 24 h
(a camera that has not recorded a day yet counts at the nominal 4.4 GB/day) times the kept video days, against the
media disk's free space; the camera form warns when one more camera would pass 85 % of it."""
from __future__ import annotations

import shutil
from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from . import store, volume
from .config import Settings

NOMINAL_BYTES_PER_DAY = 4_400_000_000  # one network camera: 720p, 2 fps, about 400 kbps, plus sound
WARN_RATIO = 0.85


@dataclass(frozen=True)
class Capacity:
    days: int
    bytes_per_day: int
    total_bytes: int
    free_bytes: int | None
    new_camera_bytes: int
    ratio: float | None
    warn_if_added: bool


def _free_bytes(settings: Settings) -> int | None:
    if not volume.ready(settings.media_root, settings.media_require_marker):
        return None
    try:
        return shutil.disk_usage(settings.media_root).free
    except OSError:
        return None


def gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB".replace(".", ",")


async def estimate(pool: AsyncConnectionPool, settings: Settings) -> Capacity:
    days = (await store.get_retention(pool)).video_days
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT coalesce(sum(i.size_bytes), 0)::bigint AS bytes FROM item i JOIN device d ON d.id = i.device_id"
            " WHERE NOT d.revoked AND d.last_heartbeat_at > now() - interval '7 days'"
            " AND i.started_at > now() - interval '24 hours'"
        )
        measured = (await cur.fetchone())["bytes"]
        cur = await conn.execute(
            "SELECT count(*) AS n FROM network_camera c LEFT JOIN device d ON d.id = c.device_id"
            " WHERE c.removed_at IS NULL AND (c.device_id IS NULL OR NOT EXISTS ("
            "   SELECT 1 FROM item i WHERE i.device_id = c.device_id AND i.started_at <= now() - interval '24 hours'))"
        )
        young = (await cur.fetchone())["n"]
    # A camera with less than a day of recordings is counted at the nominal rate instead of its partial day. What is
    # taken out is what the first query put in (same device filter), so a revoked or silent device never goes negative.
    if young:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT coalesce(sum(i.size_bytes), 0)::bigint AS bytes FROM item i"
                " JOIN network_camera c ON c.device_id = i.device_id JOIN device d ON d.id = i.device_id"
                " WHERE c.removed_at IS NULL AND NOT d.revoked AND d.last_heartbeat_at > now() - interval '7 days'"
                " AND i.started_at > now() - interval '24 hours' AND NOT EXISTS ("
                "   SELECT 1 FROM item o WHERE o.device_id = c.device_id AND o.started_at <= now() - interval '24 hours')"
            )
            measured -= (await cur.fetchone())["bytes"]
    per_day = measured + young * NOMINAL_BYTES_PER_DAY
    total = per_day * days
    free = _free_bytes(settings)
    new_camera = NOMINAL_BYTES_PER_DAY * days
    ratio = total / free if free else None
    warn = free is not None and total + new_camera > WARN_RATIO * free
    return Capacity(days, per_day, total, free, new_camera, ratio, warn)

"""Where each device stands (P3c design §4.1, §4.4, D34): an editable location name per device ("Phòng khách"),
shown on the timeline, in search, chat and MCP, and named in the text the chapter and recap models read. It starts
as the device's name; the owner changes it on the Thiết bị page. Pages and prompts name the place only when the rows
come from two or more devices (search and chat also when two cameras are active), so a brain with a single camera
looks and prompts as before."""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from psycopg_pool import AsyncConnectionPool

LOCATION_MAX = 60


class LocationError(ValueError):
    """A location name the page refuses; the message is shown to the owner as is."""


def clean_location(text: str) -> str:
    """The name to store: surrounding blanks dropped, inner runs of blanks made one space; refused when empty,
    longer than LOCATION_MAX characters, or holding a character that does not print."""
    cleaned = " ".join(text.split())
    if not cleaned:
        raise LocationError("Tên vị trí không được để trống.")
    if len(cleaned) > LOCATION_MAX:
        raise LocationError(f"Tên vị trí dài quá {LOCATION_MAX} ký tự.")
    if not cleaned.isprintable():
        raise LocationError("Tên vị trí có ký tự không hợp lệ.")
    return cleaned


async def set_location(pool: AsyncConnectionPool, device_id: str, location: str) -> bool:
    """Store an already cleaned name for a device that is not revoked; False when there is no such device."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE device SET location = %s WHERE id = %s AND NOT revoked RETURNING id", (location, device_id)
        )
        return await cur.fetchone() is not None


async def location_of(pool: AsyncConnectionPool, device_id: str) -> str | None:
    """The stored name of a device that is not revoked; None when there is no such device."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT location FROM device WHERE id = %s AND NOT revoked", (device_id,))
        row = await cur.fetchone()
        return None if row is None else row["location"]


def several(device_ids: Iterable[str | None]) -> bool:
    """Whether the rows come from two or more devices: only then is each one labelled with its place."""
    return len({d for d in device_ids if d is not None}) >= 2


async def active_cameras(pool: AsyncConnectionPool) -> int:
    """Devices that are recording places now: not revoked and heard from in the last 7 days (old pairings left
    behind do not count)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM device WHERE NOT revoked AND last_heartbeat_at > now() - interval '7 days'"
        )
        return (await cur.fetchone())["n"]


async def name_places(pool: AsyncConnectionPool, device_ids: Iterable[str | None]) -> bool:
    """Whether search and chat results name their place: when the hits themselves come from two or more devices, or
    when two or more cameras are active (a camera's results then still need the place to tell it from the other)."""
    return several(device_ids) or await active_cameras(pool) >= 2


def at(location: str | None, named: bool, sep: str = "") -> str:
    """The place in a line of model text (" at Phòng khách" + ``sep``), or nothing when places are not named."""
    return f" at {location}{sep}" if named and location else ""


async def day_places(pool: AsyncConnectionPool, start: dt.datetime, end: dt.datetime,
                     include: str | None = None) -> list[dict]:
    """Every device (revoked ones too) with a recording or a moment in [start, end), as {"id", "location"},
    ordered by location then id: the cameras a merged day shows. ``include`` (the camera a page is filtered to) is
    listed even with nothing in the range, in its place in that same order."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT d.id, d.location FROM device d"
            " WHERE EXISTS (SELECT 1 FROM item i WHERE i.device_id = d.id AND i.started_at >= %(start)s"
            "               AND i.started_at < %(end)s)"
            " OR EXISTS (SELECT 1 FROM moment m WHERE m.device_id = d.id AND m.ts_start >= %(start)s"
            "            AND m.ts_start < %(end)s)"
            " OR d.id = %(include)s::text"
            " ORDER BY d.location, d.id",
            {"start": start, "end": end, "include": include},
        )
        return await cur.fetchall()


async def is_active_device(pool: AsyncConnectionPool, device_id: str) -> bool:
    """Whether a device that is not revoked has this id: the only kind whose location can be renamed."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM device WHERE id = %s AND NOT revoked", (device_id,))
        return await cur.fetchone() is not None


async def known_device(pool: AsyncConnectionPool, device_id: str) -> bool:
    """Whether a device (revoked ones too) has this id: a camera filter naming anything else is refused."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM device WHERE id = %s", (device_id,))
        return await cur.fetchone() is not None

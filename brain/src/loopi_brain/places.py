"""Where each device stands (P3c design §4.1, §4.4, D34): an editable location name per device ("Phòng khách"),
shown on the timeline, in search, chat and MCP, and named in the text the chapter and recap models read. It starts
as the device's name; the owner changes it on the Thiết bị page. Pages and prompts name the place only when the rows
come from two or more devices, so a brain with a single camera looks and prompts as before."""
from __future__ import annotations

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


def several(device_ids: Iterable[str | None]) -> bool:
    """Whether the rows come from two or more devices: only then is each one labelled with its place."""
    return len({d for d in device_ids if d is not None}) >= 2


def at(location: str | None, named: bool, sep: str = "") -> str:
    """The place in a line of model text (" at Phòng khách" + ``sep``), or nothing when places are not named."""
    return f" at {location}{sep}" if named and location else ""

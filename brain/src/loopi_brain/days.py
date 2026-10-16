"""Local days. The household timeline is cut into days in one timezone (LOOPI_TZ, default Asia/Seoul),
across every device: chapters, recaps and commitments belong to a local day."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo


def day_bounds_utc(day: dt.date, zone: ZoneInfo) -> tuple[dt.datetime, dt.datetime]:
    """[start, end) of local ``day`` in UTC (23 or 25 hours long on a DST change)."""
    start = dt.datetime.combine(day, dt.time.min, tzinfo=zone)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=zone)
    return start.astimezone(dt.UTC), end.astimezone(dt.UTC)


def local_day(ts: dt.datetime, zone: ZoneInfo) -> dt.date:
    """The local day an aware timestamp falls on."""
    return ts.astimezone(zone).date()


def local_now(zone: ZoneInfo, now: dt.datetime | None = None) -> dt.datetime:
    """``now`` (default: the current time) in ``zone``."""
    return (now or dt.datetime.now(dt.UTC)).astimezone(zone)


def hm(ts: dt.datetime, zone: ZoneInfo) -> str:
    """Local clock time of an aware timestamp as ``HH:MM``."""
    return ts.astimezone(zone).strftime("%H:%M")

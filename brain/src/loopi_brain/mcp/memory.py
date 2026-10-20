"""What the MCP tools read (spec §10.5, P3 design §3.2): plain async functions over the database, one per tool,
returning pydantic models (their JSON schema becomes each tool's output schema). Times are local (``LOOPI_TZ``)
ISO 8601 with offset, days ``YYYY-MM-DD``; moment texts and summaries are clipped to TEXT_MAX characters, the recap
to MARKDOWN_MAX. Errors are NotFound or BadInput, whose messages are fixed English text that never repeats what the
caller sent. This module does not import the MCP SDK."""
from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

import anyio
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from .. import search, thumbs
from ..config import Settings
from ..days import day_bounds_utc
from ..providers.router import Router

TEXT_MAX = 500
MARKDOWN_MAX = 8000
SEARCH_LIMIT_MAX = 50
COMMITMENTS_MAX = 200
OBJECTS_MAX = 20  # caption objects listed for one moment
KINDS = ("visual", "speech", "marker")
STATUSES = ("open", "done", "dismissed")
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


class NotFound(Exception):
    """Nothing with that id; the message is fixed text."""


class BadInput(Exception):
    """An argument the tool cannot use; the message is fixed text."""


class MomentRef(BaseModel):
    moment_id: str
    kind: str
    ts_start: str
    ts_end: str
    day: str
    text: str
    lang: str | None
    location: str | None = None  # where the recording device stands (D34)


class SearchOut(BaseModel):
    moments: list[MomentRef]
    not_found: bool
    semantic: bool  # False: the embedding model could not be reached, so moments were matched by their words only


class ChapterOut(BaseModel):
    id: str
    ts_start: str
    ts_end: str
    title: str | None
    summary: str | None
    closed: bool


class MarkerOut(BaseModel):
    moment_id: str
    ts_start: str
    text: str
    location: str | None = None  # where the recording device stands (D34)


class TimelineOut(BaseModel):
    day: str
    chapters: list[ChapterOut]
    markers: list[MarkerOut]
    counts: dict[str, int]  # the day's moments per kind: visual, speech, marker


class RecapOut(BaseModel):
    day: str
    exists: bool
    markdown: str | None
    generated_at: str | None
    stale: bool


class CaptionOut(BaseModel):
    scene: str | None
    activity: str | None
    objects: list[str]
    people_count: int | None


class MomentOut(BaseModel):
    moment_id: str
    kind: str
    ts_start: str
    ts_end: str
    day: str
    text: str
    lang: str | None
    caption: CaptionOut | None
    image: bool  # whether a 320 px keyframe picture is attached to this result (D24)
    location: str | None = None  # where the recording device stands (D34)


class CommitmentOut(BaseModel):
    id: str
    day: str
    text: str
    status: str
    source_moment_id: str | None


class CommitmentsOut(BaseModel):
    commitments: list[CommitmentOut]


def iso(ts: dt.datetime, zone: ZoneInfo) -> str:
    return ts.astimezone(zone).isoformat(timespec="seconds")


def _clip(text: str | None, limit: int = TEXT_MAX) -> str | None:
    return None if text is None else text[:limit]


def parse_day(value: str) -> dt.date:
    """``YYYY-MM-DD`` -> a date; BadInput otherwise."""
    text = value.strip() if isinstance(value, str) else ""
    if not _DAY.fullmatch(text):
        raise BadInput("date must be YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise BadInput("date must be YYYY-MM-DD") from None


def parse_when(value: str | None, zone: ZoneInfo, *, end: bool) -> dt.datetime | None:
    """A search bound. ``YYYY-MM-DD``: a start bound is that day's local midnight, an end bound the next one (the end
    day is included). Otherwise an ISO 8601 date-time; one without an offset is local. None or blank stays None."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    if _DAY.fullmatch(text):
        start, stop = day_bounds_utc(parse_day(text), zone)
        return stop if end else start
    try:
        when = dt.datetime.fromisoformat(text)
    except ValueError:
        raise BadInput("start and end must be YYYY-MM-DD or ISO 8601 date-times") from None
    return when.replace(tzinfo=zone) if when.tzinfo is None else when


async def search_memories(
    pool: AsyncConnectionPool, router: Router | None, settings: Settings, query: str, start: str | None,
    end: str | None, limit: int,
) -> SearchOut:
    """The hybrid search of the Search page (P2b): meaning and words, the same thresholds."""
    zone = settings.zone
    first, last = parse_when(start, zone, end=False), parse_when(end, zone, end=True)
    if first is not None and last is not None and first >= last:
        raise BadInput("start must be before end")
    if not search.clean_query(query):
        raise BadInput("query is empty")
    result = await search.search_moments(
        pool, router, query, start=first, end=last, limit=max(1, min(SEARCH_LIMIT_MAX, limit)),
        max_distance=settings.search_max_distance, min_similarity=settings.search_min_similarity,
    )
    ends: dict[str, dt.datetime] = {}
    if result.hits:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT id, ts_end FROM moment WHERE id = ANY(%s)", ([h.id for h in result.hits],))
            ends = {row["id"]: row["ts_end"] for row in await cur.fetchall()}
    moments = [  # a moment forgotten since the search is left out
        MomentRef(moment_id=h.id, kind=h.kind, ts_start=iso(h.ts_start, zone), ts_end=iso(ends[h.id], zone),
                  day=h.ts_start.astimezone(zone).date().isoformat(), text=_clip(h.text), lang=h.lang,
                  location=h.location)
        for h in result.hits if h.id in ends
    ]
    return SearchOut(moments=moments, not_found=not moments, semantic=result.semantic)


async def get_timeline(pool: AsyncConnectionPool, day_text: str, zone: ZoneInfo) -> TimelineOut:
    day = parse_day(day_text)
    start, end = day_bounds_utc(day, zone)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, ts_start, ts_end, title, summary, closed FROM chapter WHERE day = %s ORDER BY ts_start, id",
            (day,),
        )
        chapters = await cur.fetchall()
        cur = await conn.execute(
            "SELECT m.id, m.ts_start, m.text, d.location FROM moment m LEFT JOIN device d ON d.id = m.device_id"
            " WHERE m.kind = 'marker' AND m.ts_start >= %s AND m.ts_start < %s ORDER BY m.ts_start, m.id",
            (start, end),
        )
        markers = await cur.fetchall()
        cur = await conn.execute(
            "SELECT kind, count(*) AS n FROM moment WHERE ts_start >= %s AND ts_start < %s GROUP BY kind", (start, end)
        )
        counts = {kind: 0 for kind in KINDS} | {row["kind"]: row["n"] for row in await cur.fetchall()}
    return TimelineOut(
        day=day.isoformat(),
        chapters=[
            ChapterOut(id=c["id"], ts_start=iso(c["ts_start"], zone), ts_end=iso(c["ts_end"], zone), title=c["title"],
                       summary=_clip(c["summary"]), closed=c["closed"])
            for c in chapters
        ],
        markers=[
            MarkerOut(moment_id=m["id"], ts_start=iso(m["ts_start"], zone), text=_clip(m["text"]),
                      location=m["location"])
            for m in markers
        ],
        counts=counts,
    )


async def get_day_recap(pool: AsyncConnectionPool, day_text: str, zone: ZoneInfo) -> RecapOut:
    day = parse_day(day_text)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT markdown, generated_at, stale FROM day_recap WHERE day = %s", (day,))
        row = await cur.fetchone()
    if row is None:
        return RecapOut(day=day.isoformat(), exists=False, markdown=None, generated_at=None, stale=False)
    return RecapOut(day=day.isoformat(), exists=True, markdown=_clip(row["markdown"], MARKDOWN_MAX),
                    generated_at=iso(row["generated_at"], zone), stale=row["stale"])


def _caption(caption: dict | None) -> CaptionOut | None:
    if not isinstance(caption, dict):
        return None
    objects = [o[:100] for o in caption.get("objects") or [] if isinstance(o, str)][:OBJECTS_MAX]
    people = caption.get("people_count")
    return CaptionOut(
        scene=_clip(caption.get("scene")) if isinstance(caption.get("scene"), str) else None,
        activity=_clip(caption.get("activity")) if isinstance(caption.get("activity"), str) else None,
        objects=objects,
        people_count=people if isinstance(people, int) and not isinstance(people, bool) else None,
    )


async def get_moment(
    pool: AsyncConnectionPool, settings: Settings, moment_id: str, images: bool
) -> tuple[MomentOut, bytes | None]:
    """One moment with its caption; with ``images`` (the D24 switch) also its keyframe's 320 px thumbnail as JPEG,
    unless the keyframe is a golden-set sample (D21) or its file cannot be read (the picture is dropped, not the
    answer)."""
    wanted = moment_id.strip().upper() if isinstance(moment_id, str) else ""
    if not _ULID.fullmatch(wanted):
        raise NotFound("no such moment")
    zone = settings.zone
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT m.id, m.kind, m.ts_start, m.ts_end, m.text, m.lang, m.keyframe_id, k.caption, d.location,"
            " EXISTS (SELECT 1 FROM eval_caption e WHERE e.keyframe_id = m.keyframe_id) AS golden"
            " FROM moment m LEFT JOIN keyframe k ON k.id = m.keyframe_id LEFT JOIN device d ON d.id = m.device_id"
            " WHERE m.id = %s",
            (wanted,),
        )
        row = await cur.fetchone()
    if row is None:
        raise NotFound("no such moment")
    picture = None
    if images and row["keyframe_id"] and not row["golden"]:
        try:
            path = await thumbs.keyframe_thumb(pool, settings.media_root, row["keyframe_id"])
            picture = await anyio.to_thread.run_sync(path.read_bytes)
        except (thumbs.ThumbUnavailable, OSError):
            picture = None
    out = MomentOut(
        moment_id=row["id"], kind=row["kind"], ts_start=iso(row["ts_start"], zone), ts_end=iso(row["ts_end"], zone),
        day=row["ts_start"].astimezone(zone).date().isoformat(), text=_clip(row["text"]), lang=row["lang"],
        caption=_caption(row["caption"]) if row["kind"] == "visual" else None, image=picture is not None,
        location=row["location"],
    )
    return out, picture


async def list_commitments(pool: AsyncConnectionPool, status: str | None) -> CommitmentsOut:
    if status is not None and status not in STATUSES:
        raise BadInput("status must be open, done or dismissed")
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, day, text, status, source_moment_id FROM commitment"
            " WHERE (%(status)s::text IS NULL OR status = %(status)s)"
            " ORDER BY day DESC, created_at DESC, id DESC LIMIT %(limit)s",
            {"status": status, "limit": COMMITMENTS_MAX},
        )
        rows = await cur.fetchall()
    return CommitmentsOut(commitments=[
        CommitmentOut(id=r["id"], day=r["day"].isoformat(), text=_clip(r["text"]), status=r["status"],
                      source_moment_id=r["source_moment_id"])
        for r in rows
    ])

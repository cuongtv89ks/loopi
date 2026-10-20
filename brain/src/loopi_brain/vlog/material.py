"""What a day's vlog is made of (spec §10.3, D25; P3 design §4.2), read only:
- the recordings of the hours with activity (a moment within ACTIVITY_MARGIN, MP4 view present, raw kept);
- the day's keyframes, spread over the day (the slideshow when no recording is active, e.g. after retention);
- the highlight candidates — marked moments, notable pictures, dense conversations — each with a keyframe picture,
  scored, merged when close, at most CANDIDATES_MAX, named H1… in time order (what the script model chooses from).
With several cameras (P3c-3, D35, plan R11) each BLOCK of the day with an active recording shows one camera, the one
with the most moments in it; the stills may come from any camera, and ``places`` names every device the material shows.
Times are UTC datetimes; paths are relative to the media root."""
from __future__ import annotations

import bisect
import datetime as dt
from collections import Counter
from dataclasses import dataclass, field

from psycopg_pool import AsyncConnectionPool

from ..days import day_bounds_utc
from ..llm import clip, spread

ACTIVITY_MARGIN = dt.timedelta(minutes=5)  # a recording is active with a moment within 5 minutes of it
BLOCK = dt.timedelta(minutes=10)  # D35: each active block shows one camera (plan R11)
MARKER_REACH = dt.timedelta(seconds=30)  # a marked moment's picture: the nearest keyframe within ±30 s
TALK_WINDOW = dt.timedelta(minutes=5)
TALK_MIN = 6  # speech moments within TALK_WINDOW that make a dense conversation
MERGE_GAP = dt.timedelta(minutes=2)  # candidates closer than this are one highlight (the higher score stays)
CANDIDATES_MAX = 40
TEXT_MAX = 200
SLIDES_MAX = 240  # keyframes in a slideshow day (0.25 s each: at most 60 s)
SCORE = {"marker": 3.0, "notable": 2.0, "talk": 1.0}
AUDIO_LEAD_S = 0.5  # a conversation's sound starts this much before its longest line


@dataclass(frozen=True)
class Segment:
    item_id: str
    path: str  # the MP4 view, relative to the media root
    start: dt.datetime
    device_id: str | None = None


@dataclass(frozen=True)
class Frame:
    keyframe_id: str
    path: str  # the JPEG, relative to the media root
    at: dt.datetime
    device_id: str | None = None


@dataclass(frozen=True)
class Candidate:
    ref: str  # H1… in time order: how the script model names it
    kind: str  # marker | notable | talk
    at: dt.datetime
    frame: Frame
    text: str
    score: float
    audio_item: str | None = None  # an audio item whose sound can play under the picture (talk only)
    audio_offset: float = 0.0  # seconds into that item


@dataclass(frozen=True)
class Material:
    day: dt.date
    segments: list[Segment]
    frames: list[Frame]
    candidates: list[Candidate]
    places: dict[str, str] = field(default_factory=dict)  # device id -> location, for every device shown

    @property
    def empty(self) -> bool:
        return not self.segments and not self.frames


_SEGMENTS = """
SELECT i.id, i.view_path, i.started_at, i.device_id FROM item i
 WHERE i.kind = 'video' AND i.analysis_state = 'done' AND i.raw_deleted_at IS NULL AND i.view_path IS NOT NULL
   AND i.started_at >= %(start)s AND i.started_at < %(end)s
   AND EXISTS (SELECT 1 FROM moment m WHERE m.ts_start >= i.started_at - %(margin)s
                                        AND m.ts_start <= i.ended_at + %(margin)s)
 ORDER BY i.started_at, i.id"""
_FRAMES = """
SELECT id, path, ts, caption, device_id FROM keyframe WHERE ts >= %(start)s AND ts < %(end)s ORDER BY ts, id"""
_BLOCK_MOMENTS = """
SELECT device_id, floor(extract(epoch FROM ts_start - %(start)s) / %(block)s)::int AS block, count(*) AS n
  FROM moment WHERE ts_start >= %(start)s AND ts_start < %(end)s GROUP BY 1, 2"""
_PLACES = "SELECT id, location FROM device WHERE id = ANY(%s)"
_MARKERS = """
SELECT ts_start, text FROM moment WHERE kind = 'marker' AND ts_start >= %(start)s AND ts_start < %(end)s
 ORDER BY ts_start, id"""
_SPEECH = """
SELECT m.ts_start, m.text, m.item_id, i.started_at AS item_start, i.raw_deleted_at
  FROM moment m LEFT JOIN item i ON i.id = m.item_id AND i.kind = 'audio'
 WHERE m.kind = 'speech' AND m.ts_start >= %(start)s AND m.ts_start < %(end)s ORDER BY m.ts_start, m.id"""


def choose_cameras(segments: list[Segment], counts: dict[tuple[int, str], int], start: dt.datetime) -> list[Segment]:
    """D35 (R11): in each BLOCK from ``start``, the segments of one camera — the one with the most moments in the
    block (``counts[(block, device)]``) among those with a segment there; ties: more segments, then the smaller id."""
    by_block: dict[int, list[Segment]] = {}
    for s in segments:
        by_block.setdefault((s.start - start) // BLOCK, []).append(s)
    kept: list[Segment] = []
    for block, segs in sorted(by_block.items()):
        per_device = Counter(s.device_id for s in segs)
        winner = min(per_device, key=lambda d: (-counts.get((block, d), 0), -per_device[d], d or ""))
        kept += [s for s in segs if s.device_id == winner]
    return sorted(kept, key=lambda s: (s.start, s.item_id))


def nearest(frames: list[Frame], at: dt.datetime, reach: dt.timedelta) -> Frame | None:
    """The frame closest to ``at`` within ``reach`` (frames sorted by time), or None."""
    times = [f.at for f in frames]
    i = bisect.bisect_left(times, at)
    best = None
    for f in frames[max(0, i - 1):i + 1]:
        if abs(f.at - at) <= reach and (best is None or abs(f.at - at) < abs(best.at - at)):
            best = f
    return best


def talk_windows(rows: list[dict]) -> list[list[dict]]:
    """Dense conversations: runs of at least TALK_MIN speech lines starting within TALK_WINDOW of the first, taken
    greedily in time order (rows sorted by ``ts_start``); a line belongs to one window at most."""
    windows, i = [], 0
    while i < len(rows):
        j = i
        while j + 1 < len(rows) and rows[j + 1]["ts_start"] < rows[i]["ts_start"] + TALK_WINDOW:
            j += 1
        if j - i + 1 >= TALK_MIN:
            windows.append(rows[i:j + 1])
            i = j + 1
        else:
            i += 1
    return windows


def merge(candidates: list[Candidate]) -> list[Candidate]:
    """At most CANDIDATES_MAX candidates, no two closer than MERGE_GAP (the higher score stays, then the earlier),
    in time order and named H1…."""
    kept: list[Candidate] = []
    for c in sorted(candidates, key=lambda c: (-c.score, c.at)):
        if all(abs(c.at - k.at) >= MERGE_GAP for k in kept):
            kept.append(c)
        if len(kept) == CANDIDATES_MAX:
            break
    kept.sort(key=lambda c: c.at)
    return [Candidate(f"H{n}", c.kind, c.at, c.frame, c.text, c.score, c.audio_item, c.audio_offset)
            for n, c in enumerate(kept, start=1)]


def _caption_text(caption: dict | None) -> str:
    if not isinstance(caption, dict):
        return ""
    for key in ("activity", "scene"):
        if isinstance(caption.get(key), str) and caption[key].strip():
            return caption[key].strip()
    return ""


async def vlog_material(pool: AsyncConnectionPool, day: dt.date, zone) -> Material:
    start, end = day_bounds_utc(day, zone)
    params = {"start": start, "end": end, "margin": ACTIVITY_MARGIN, "block": BLOCK.total_seconds()}
    async with pool.connection() as conn:
        segments = [Segment(r["id"], r["view_path"], r["started_at"], r["device_id"])
                    for r in await (await conn.execute(_SEGMENTS, params)).fetchall()]
        counts = {(r["block"], r["device_id"]): r["n"]
                  for r in await (await conn.execute(_BLOCK_MOMENTS, params)).fetchall()}
        frame_rows = await (await conn.execute(_FRAMES, params)).fetchall()
        markers = await (await conn.execute(_MARKERS, params)).fetchall()
        speech = await (await conn.execute(_SPEECH, params)).fetchall()
    segments = choose_cameras(segments, counts, start)
    frames = [Frame(r["id"], r["path"], r["ts"], r["device_id"]) for r in frame_rows]
    found: list[Candidate] = []
    for m in markers:
        frame = nearest(frames, m["ts_start"], MARKER_REACH)
        if frame is not None:
            found.append(Candidate("", "marker", m["ts_start"], frame, clip(m["text"], TEXT_MAX), SCORE["marker"]))
    for r, frame in zip(frame_rows, frames):
        caption = r["caption"]
        if isinstance(caption, dict) and caption.get("notable") is True:
            found.append(Candidate("", "notable", frame.at, frame, clip(_caption_text(caption), TEXT_MAX),
                                   SCORE["notable"]))
    for window in talk_windows(speech):
        first, last = window[0]["ts_start"], window[-1]["ts_start"]
        centre = first + (last - first) / 2
        frame = nearest(frames, centre, TALK_WINDOW)
        if frame is None:
            continue
        longest = max(window, key=lambda r: len(r["text"] or ""))
        audio, offset = None, 0.0
        if longest["item_id"] and longest["item_start"] is not None and longest["raw_deleted_at"] is None:
            audio = longest["item_id"]
            offset = max(0.0, (longest["ts_start"] - longest["item_start"]).total_seconds() - AUDIO_LEAD_S)
        found.append(Candidate("", "talk", centre, frame, clip(longest["text"] or "", TEXT_MAX),
                               SCORE["talk"] + min(len(window), 20) / 10, audio, offset))
    slides = spread(frames, SLIDES_MAX)
    candidates = merge(found)  # a camera whose candidates were all merged away is not shown: it names no place
    ids = sorted(({s.device_id for s in segments} | {f.device_id for f in slides}
                  | {c.frame.device_id for c in candidates}) - {None})
    async with pool.connection() as conn:
        found_places = await (await conn.execute(_PLACES, (ids,))).fetchall()
    return Material(day, segments, slides, candidates, {r["id"]: r["location"] for r in found_places})

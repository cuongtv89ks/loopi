"""Chapter boundaries of a local day (spec §10.1): a gap, a change of picture, or a change of topic in speech.

Pure and deterministic: the same moments always give the same chapters, so a chapter's summary is only
rewritten when its moments change. Unit-tested with synthetic embeddings.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass

import numpy as np

DRIFT_KINDS = ("visual", "speech")


@dataclass(frozen=True)
class ChapterParams:
    gap_s: float = 20 * 60  # no moment for this long (nobody home, paused, asleep): a new chapter
    min_s: float = 10 * 60  # a change of picture or topic splits only a chapter at least this long
    max_s: float = 3 * 3600  # no chapter runs longer than this
    visual_drift: float = 0.35  # cosine distance of a caption from the chapter's recent captions
    speech_drift: float = 0.45  # the same for speech: a change of topic
    window: int = 4  # how many recent captions (or speech lines) a new one is compared with
    settle_s: float = 30 * 60  # a chapter closes once everything up to its end is this old and analysed


@dataclass(frozen=True)
class Point:
    """One moment as the segmentation sees it."""

    id: str
    kind: str  # visual | speech | marker
    start: dt.datetime
    end: dt.datetime
    vector: np.ndarray | None = None  # unit-length embedding; None until embedded (markers never drift)


def unit(values) -> np.ndarray | None:
    """``values`` as a unit-length float vector; None for a zero vector."""
    vector = np.asarray(values, dtype=np.float64)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else None


def drift(vector: np.ndarray, window: list[np.ndarray]) -> float:
    """Cosine distance between ``vector`` and the mean direction of ``window`` (0 same, 1 unrelated)."""
    centre = np.mean(window, axis=0)
    norm = float(np.linalg.norm(centre))
    if norm == 0:
        return 1.0
    return float(1.0 - np.dot(vector, centre) / norm)


def _limit(kind: str, params: ChapterParams) -> float:
    return params.visual_drift if kind == "visual" else params.speech_drift


def _shifted(points: list[Point], i: int, windows: dict[str, list[np.ndarray]], params: ChapterParams) -> bool:
    """Point ``i`` differs from the chapter's recent points of its kind, and so does the next point of that
    kind: one odd frame (someone crossing the room) or one off-topic sentence does not split a chapter."""
    point = points[i]
    window = windows.get(point.kind)
    if point.vector is None or not window:
        return False
    limit = _limit(point.kind, params)
    if drift(point.vector, window) <= limit:
        return False
    for later in points[i + 1:]:
        if later.kind == point.kind and later.vector is not None:
            return drift(later.vector, window) > limit
    return False  # nothing to confirm it yet: decided when more moments arrive


def _remember(windows: dict[str, list[np.ndarray]], point: Point, size: int) -> None:
    if point.vector is not None and point.kind in windows:
        windows[point.kind] = (windows[point.kind] + [point.vector])[-size:]


def boundaries(points: list[Point], params: ChapterParams = ChapterParams()) -> list[int]:
    """Indexes of ``points`` (sorted by start, then id) where a chapter starts; [] for no points."""
    if not points:
        return []
    starts = [0]
    chapter_start, last_end = points[0].start, points[0].end
    windows: dict[str, list[np.ndarray]] = {kind: [] for kind in DRIFT_KINDS}
    _remember(windows, points[0], params.window)
    for i in range(1, len(points)):
        point = points[i]
        age = (point.start - chapter_start).total_seconds()
        gap = (point.start - last_end).total_seconds()
        if gap >= params.gap_s or age >= params.max_s or (age >= params.min_s and _shifted(points, i, windows, params)):
            starts.append(i)
            chapter_start = point.start
            windows = {kind: [] for kind in DRIFT_KINDS}
        last_end = max(last_end, point.end)
        _remember(windows, point, params.window)
    return starts


def segment(points: list[Point], params: ChapterParams = ChapterParams()) -> list[list[Point]]:
    """``points`` cut into chapters (each a non-empty list, in order)."""
    starts = boundaries(points, params)
    return [points[a:b] for a, b in zip(starts, [*starts[1:], len(points)], strict=False)]


def closed_count(segments: list[list[Point]], horizon: dt.datetime, day_end: dt.datetime) -> int:
    """How many leading segments are closed: the next segment starts at or before ``horizon`` (for the last one:
    the whole day is over, ``day_end`` <= ``horizon``), so the last, open segment is never closed while the day runs.

    Closed segments are frozen as they stood when they closed: the caller keeps their rows and re-segments only
    the moments after them, so data arriving later never changes a closed segment. A drift boundary is confirmed
    by the next moment of its kind, however far away, so a full rebuild of the whole day from scratch (after a
    forget or a reprocess) may draw different boundaries than the ones frozen earlier."""
    count = 0
    for k in range(len(segments)):
        limit = segments[k + 1][0].start if k + 1 < len(segments) else day_end
        if limit > horizon:
            break
        count += 1
    return count


def signature(points: list[Point]) -> str:
    """Identifies a chapter's members: equal signatures mean the same moments."""
    return hashlib.sha256("\n".join(p.id for p in points).encode()).hexdigest()[:32]

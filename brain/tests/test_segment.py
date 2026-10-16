import datetime as dt

import numpy as np

from loopi_brain.pipeline.segment import (
    ChapterParams,
    Point,
    boundaries,
    closed_count,
    drift,
    segment,
    signature,
    unit,
)

T = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
DIMS = 16


def vec(axis: int, wobble: float = 0.0) -> np.ndarray:
    """A unit vector along ``axis``, nudged a little towards the last axis (same subject, another frame)."""
    v = np.zeros(DIMS)
    v[axis] = 1.0
    v[-1] = wobble
    return unit(v)


def points(*runs) -> list[Point]:
    """``runs`` of (kind, axis or None, count, minutes between points, minutes of gap before the run)."""
    out: list[Point] = []
    t = T
    for kind, axis, count, every, gap in runs:
        t += dt.timedelta(minutes=gap)
        for n in range(count):
            vector = None if axis is None else vec(axis, wobble=0.05 * (n % 3))
            out.append(Point(f"m{len(out):04d}", kind, t, t + dt.timedelta(seconds=5), vector))
            t += dt.timedelta(minutes=every)
    return out


def test_drift_is_zero_for_the_same_direction_and_one_for_unrelated_ones():
    assert drift(vec(0), [vec(0), vec(0)]) < 1e-9
    assert abs(drift(vec(1), [vec(0)]) - 1.0) < 1e-9
    assert unit([0.0, 0.0]) is None


def test_a_long_gap_starts_a_new_chapter():
    day = points(("visual", 0, 15, 1, 0), ("visual", 0, 10, 1, 25))
    assert boundaries(day) == [0, 15]


def test_a_change_of_scene_splits_the_chapter_where_it_happens():
    day = points(("visual", 0, 15, 1, 0), ("visual", 1, 15, 1, 0))
    chapters = segment(day)
    assert [len(c) for c in chapters] == [15, 15]
    assert chapters[1][0].start == T + dt.timedelta(minutes=15)


def test_one_odd_frame_does_not_split_a_chapter():
    # Someone crosses the room once: the next caption is like the earlier ones again.
    day = points(("visual", 0, 15, 1, 0), ("visual", 1, 1, 1, 0), ("visual", 0, 15, 1, 0))
    assert boundaries(day) == [0]


def test_a_change_soon_after_a_chapter_starts_joins_it():
    # Five minutes of one scene are too short a chapter: the minimum length wins over the drift.
    day = points(("visual", 0, 5, 1, 0), ("visual", 1, 20, 1, 0))
    assert boundaries(day) == [0]


def test_a_change_of_topic_in_speech_splits_the_chapter():
    day = points(("speech", 2, 12, 1, 0), ("speech", 3, 12, 1, 0))
    assert boundaries(day) == [0, 12]


def test_pictures_and_speech_drift_separately():
    # Captions keep the same scene while two people talk on and on about the same thing: one chapter.
    day = sorted(points(("visual", 0, 30, 1, 0)) + [
        Point(f"s{n:03d}", "speech", T + dt.timedelta(minutes=n, seconds=30),
              T + dt.timedelta(minutes=n, seconds=40), vec(5)) for n in range(30)
    ], key=lambda p: (p.start, p.id))
    assert boundaries(day) == [0]


def test_a_long_activity_is_cut_at_the_maximum_length():
    day = points(("visual", 0, 85, 5, 0))  # one scene every 5 minutes for 7 hours
    chapters = segment(day)
    assert [len(c) for c in chapters] == [36, 36, 13]
    assert all(c[-1].start - c[0].start < dt.timedelta(hours=3) for c in chapters)


def test_markers_and_moments_without_embeddings_follow_time_only():
    day = points(("visual", None, 15, 1, 0), ("marker", None, 1, 1, 0), ("speech", None, 15, 1, 0))
    assert boundaries(day) == [0]
    assert boundaries(points(("marker", None, 1, 1, 0), ("marker", None, 1, 1, 30))) == [0, 1]


def test_the_open_end_of_the_day_waits_for_confirmation():
    # The newest caption differs, but nothing after it yet: no split until the next one shows the change.
    day = points(("visual", 0, 15, 1, 0), ("visual", 1, 1, 1, 0))
    assert boundaries(day) == [0]
    later = Point("z", "visual", T + dt.timedelta(minutes=17), T + dt.timedelta(minutes=17, seconds=5), vec(1))
    assert boundaries(day + [later]) == [0, 15]


def test_no_moments_no_chapters():
    assert boundaries([]) == [] and segment([]) == []


def test_closing_needs_the_next_chapter_settled_and_the_last_needs_the_day_over():
    day = points(("visual", 0, 15, 1, 0), ("visual", 1, 15, 1, 0), ("visual", 0, 10, 1, 30))
    chapters = segment(day)
    assert [c[0].start for c in chapters] == [T, T + dt.timedelta(minutes=15), T + dt.timedelta(minutes=60)]
    day_end = T + dt.timedelta(hours=15)
    assert closed_count(chapters, T + dt.timedelta(minutes=14), day_end) == 0
    assert closed_count(chapters, T + dt.timedelta(minutes=15), day_end) == 1
    assert closed_count(chapters, T + dt.timedelta(hours=2), day_end) == 2
    assert closed_count(chapters, day_end, day_end) == 3


def test_signature_names_the_members():
    day = points(("visual", 0, 3, 1, 0))
    assert signature(day) == signature(list(day)) and len(signature(day)) == 32
    assert signature(day[:2]) != signature(day)


def test_parameters_can_be_tuned():
    day = points(("visual", 0, 15, 1, 0), ("visual", 0, 10, 1, 25))
    assert boundaries(day, ChapterParams(gap_s=60 * 60)) == [0]

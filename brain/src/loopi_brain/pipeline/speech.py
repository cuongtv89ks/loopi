"""D33 (P3c design §4.3): one transcript per sentence when several microphones hear it. Pure functions:
``detect_speech`` measures each speech region with ``snr_db``; ``group_speech`` joins the regions that overlap across
microphones with ``groups`` and keeps the clearest with ``decide`` (plan rulings R1, R2, R4, R5)."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import numpy as np

from .. import audio_level

SAMPLE_RATE = audio_level.SAMPLE_RATE
NOISE_FLOOR_MIN_DBFS = -70.0  # a noise gate sends digital silence: a floor of ~0 must not win every group (R5)
SPEECH_PERCENTILE = 90  # the speech level: the loud frames of a region, whatever its pauses
GROUP_SLACK = dt.timedelta(seconds=2)  # a network camera's sound reaches its agent about a second late (R1)
COVERED_SHARE = 0.5  # covered when chosen speech elsewhere overlaps at least half of the region (R2)
ENVELOPE_HOP_S = 0.1  # S1: one loudness value per 100 ms
# S2: no unrelated synthetic pair of seeds 1-39 reaches it at start offsets <= 0.6 s (0.3-0.4 % do at 1-5 s);
# tuned at acceptance §8.2
SAME_SOUND_MIN = 0.7
SAME_SOUND_MIN_FRAMES = 10  # S2: at least 1 s in common to compare at all (and half of the shorter envelope)
WAIT = dt.timedelta(minutes=3)  # D33: the longest wait for a minute's audio from every microphone (R3)


def frame_rms(samples: np.ndarray) -> np.ndarray:
    """RMS of each whole 20 ms frame."""
    n = len(samples) // audio_level.FRAME
    if n == 0:
        return np.zeros(0)
    frames = np.asarray(samples[: n * audio_level.FRAME], dtype=np.float64).reshape(n, audio_level.FRAME)
    return np.sqrt((frames ** 2).mean(axis=1))


def noise_level(raw: np.ndarray) -> float:
    """This microphone's noise: the quiet frames of the whole segment, never below NOISE_FLOOR_MIN_DBFS."""
    return max(audio_level.noise_floor(raw), 10 ** (NOISE_FLOOR_MIN_DBFS / 20))


def snr_db(raw: np.ndarray, start_s: float, end_s: float, noise: float) -> float:
    """Speech over noise, in dB, for the region [start_s, end_s) of the raw (not normalised) audio; 0 when the region
    holds no whole frame."""
    rms = frame_rms(raw[int(start_s * SAMPLE_RATE): int(end_s * SAMPLE_RATE)])
    if rms.size == 0:
        return 0.0
    speech = float(np.percentile(rms, SPEECH_PERCENTILE))
    return 20 * math.log10(max(speech, 1e-9) / noise)


def envelope(raw: np.ndarray, start_s: float, end_s: float) -> list[float]:
    """S1: the region's loudness every ENVELOPE_HOP_S (mean of the 20 ms frame RMS), in dBFS, floored at
    NOISE_FLOOR_MIN_DBFS; only whole steps."""
    rms = frame_rms(raw[int(start_s * SAMPLE_RATE): int(end_s * SAMPLE_RATE)])
    per = round(ENVELOPE_HOP_S * SAMPLE_RATE / audio_level.FRAME)  # 5 frames of 20 ms
    steps = len(rms) // per
    floor = 10 ** (NOISE_FLOOR_MIN_DBFS / 20)
    means = rms[: steps * per].reshape(steps, per).mean(axis=1) if steps else np.zeros(0)
    return [float(20 * np.log10(max(m, floor))) for m in means]


@dataclass(frozen=True)
class Span:
    """A speech region as grouping sees it (times are absolute, UTC)."""

    id: str
    device_id: str
    start: dt.datetime
    end: dt.datetime
    snr_db: float
    state: str = "waiting"  # waiting | chosen | covered
    envelope: tuple[float, ...] | None = None  # S1: loudness every ENVELOPE_HOP_S from ``start``; None if not stored


@dataclass(frozen=True)
class Decision:
    id: str
    state: str  # chosen | covered
    covered_by: str | None = None


def _heard(a: Span, b: Span, slack: dt.timedelta) -> float:
    """Seconds of ``a`` that ``b``, widened by ``slack`` on both sides, overlaps."""
    lo, hi = max(a.start, b.start - slack), min(a.end, b.end + slack)
    return max(0.0, (hi - lo).total_seconds())


def _heard_together(a: Span, others: list[Span], slack: dt.timedelta) -> float:
    """Seconds of ``a`` covered by the union of ``others``, each widened by ``slack`` (overlaps are not added twice)."""
    intervals = sorted((max(a.start, c.start - slack), min(a.end, c.end + slack)) for c in others)
    heard, edge = 0.0, a.start
    for lo, hi in intervals:
        lo = max(lo, edge)
        if hi > lo:
            heard += (hi - lo).total_seconds()
            edge = hi
    return heard


def best_correlation(a: Span, b: Span, slack: dt.timedelta = GROUP_SLACK) -> float | None:
    """S2: the best Pearson correlation of the loudness envelopes of ``a`` and ``b`` over the lags within ±slack, on
    the frames both cover — at least SAME_SOUND_MIN_FRAMES of them and half of the shorter envelope (a short common
    stretch of unrelated sounds correlates by chance); -1 when every lag compared is flat (no shape to match). None
    when either has no envelope or no lag qualifies: nothing to compare."""
    if not a.envelope or not b.envelope:
        return None
    ea, eb = np.asarray(a.envelope), np.asarray(b.envelope)
    shift = round((b.start - a.start).total_seconds() / ENVELOPE_HOP_S)  # b's frame 0 sits at a's frame `shift`
    reach = round(slack.total_seconds() / ENVELOPE_HOP_S)
    needed = max(SAME_SOUND_MIN_FRAMES, math.ceil(min(len(ea), len(eb)) / 2))
    best, compared = -1.0, False
    for lag in range(-reach, reach + 1):
        off = shift + lag  # a[i] pairs with b[i - off]
        lo, hi = max(0, off), min(len(ea), len(eb) + off)
        if hi - lo < needed:
            continue
        compared = True
        x, y = ea[lo:hi], eb[lo - off:hi - off]
        if np.ptp(x) == 0 or np.ptp(y) == 0:
            continue  # a flat stretch has no shape to match
        x, y = x - x.mean(), y - y.mean()
        best = max(best, float(x @ y / math.sqrt((x @ x) * (y @ y))))  # Pearson, without corrcoef's overhead
    return best if compared else None


def same_sound(a: Span, b: Span, slack: dt.timedelta = GROUP_SLACK) -> bool:
    """S2: do ``a`` and ``b`` (two microphones) hold the same sound? Their best envelope correlation reaches
    SAME_SOUND_MIN. True when there is nothing to compare (no envelope, stored before the check existed, or no lag with
    enough frames in common) — today's time-only rule."""
    best = best_correlation(a, b, slack)
    return best is None or best >= SAME_SOUND_MIN


def groups(spans: list[Span], slack: dt.timedelta = GROUP_SLACK) -> list[list[Span]]:
    """Spans of different microphones that overlap within ``slack``, joined transitively; each group in time order,
    the groups by their first span."""
    ordered = sorted(spans, key=lambda s: (s.start, s.device_id, s.id))
    parent = list(range(len(ordered)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(ordered):
        for j in range(i + 1, len(ordered)):
            b = ordered[j]
            if b.start >= a.end + slack:
                break
            if b.device_id != a.device_id and b.start < a.end + slack and a.start < b.end + slack:
                parent[find(j)] = find(i)
    out: dict[int, list[Span]] = {}
    for i, s in enumerate(ordered):
        out.setdefault(find(i), []).append(s)
    return sorted(out.values(), key=lambda g: (g[0].start, g[0].id))


def decide(group: list[Span], slack: dt.timedelta = GROUP_SLACK, share: float = COVERED_SHARE) -> list[Decision]:
    """Decisions for the group's waiting spans (R2, R4). A group without decisions picks a winner — the microphone of
    its best-SNR span (then the earliest, then the smaller device id) — and chooses all the winner's spans. A group
    that already has decisions (late audio) keeps them. Every other waiting span, best SNR first, is covered by the
    chosen span of another microphone that holds the same sound (S3) and hears most of it when such chosen spans hear
    at least ``share`` of it, and is chosen otherwise."""
    waiting = [s for s in group if s.state == "waiting"]
    if not waiting:
        return []
    chosen = [s for s in group if s.state == "chosen"]
    decisions: list[Decision] = []
    if len(waiting) == len(group):
        winner = sorted(waiting, key=lambda s: (-s.snr_db, s.start, s.device_id))[0].device_id
        for s in waiting:
            if s.device_id == winner:
                decisions.append(Decision(s.id, "chosen"))
                chosen.append(s)
        waiting = [s for s in waiting if s.device_id != winner]
    for s in sorted(waiting, key=lambda s: (-s.snr_db, s.start, s.id)):
        others = [c for c in chosen if c.device_id != s.device_id and same_sound(s, c, slack)]
        length = (s.end - s.start).total_seconds()
        heard = _heard_together(s, others, slack)
        best = min(others, key=lambda c: (-_heard(s, c, slack), -c.snr_db, c.id), default=None)
        if best is not None and length > 0 and heard >= share * length:
            decisions.append(Decision(s.id, "covered", best.id))
        else:
            decisions.append(Decision(s.id, "chosen"))
            chosen.append(s)
    return decisions

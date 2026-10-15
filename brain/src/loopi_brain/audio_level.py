"""How loud the brain makes the audio it listens to and plays back (spec D19). Raw files stay as captured.

The device's USB microphone records speech far below full scale, so the brain raises each file by one
gain: the 99.9th percentile of |x| goes to -6 dBFS (a percentile, not the peak, because isolated
one-sample glitches reach 0 dBFS in real recordings), the gain never attenuates and stops at
``max_gain_db``, and it never lifts the room's noise floor (the 10th percentile of the 20 ms frame RMS)
above -40 dBFS, so a quiet night segment stays a soft hiss instead of being raised to speech loudness.
"""
from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16_000
FRAME = SAMPLE_RATE // 50  # 20 ms
TARGET_PEAK = 0.5  # -6 dBFS
PEAK_PERCENTILE = 99.9
MAX_GAIN_DB = 30.0
NOISE_FLOOR_MAX = 0.01  # -40 dBFS
FLOOR_PERCENTILE = 10.0


def dbfs(value: float) -> float:
    """``value`` (a linear amplitude, 1.0 = full scale) in dBFS; -120 for silence."""
    return 20 * float(np.log10(value)) if value > 1e-6 else -120.0


def noise_floor(samples: np.ndarray) -> float:
    """RMS of the quietest 20 ms frames (their 10th percentile); 0.0 for less than one frame."""
    usable = samples[: samples.size // FRAME * FRAME].astype(np.float64)
    if usable.size == 0:
        return 0.0
    rms = np.sqrt(np.mean(usable.reshape(-1, FRAME) ** 2, axis=1))
    return float(np.percentile(rms, FLOOR_PERCENTILE))


def level_gain(samples: np.ndarray, target_peak: float = TARGET_PEAK, max_gain_db: float = MAX_GAIN_DB) -> float:
    """Linear gain for 16 kHz mono float samples in [-1, 1]: brings the 99.9th percentile of |x| to
    ``target_peak``, within [1, 10 ** (max_gain_db / 20)] and the noise-floor limit; 1.0 for empty or
    silent input (no division by zero)."""
    if samples.size == 0:
        return 1.0
    peak = float(np.percentile(np.abs(samples), PEAK_PERCENTILE))
    if not peak > 0.0:  # digital silence (or NaN)
        return 1.0
    limit = 10 ** (max(max_gain_db, 0.0) / 20)
    floor = noise_floor(samples)
    if floor > 0.0:
        limit = min(limit, NOISE_FLOOR_MAX / floor)
    return max(1.0, min(target_peak / peak, limit))

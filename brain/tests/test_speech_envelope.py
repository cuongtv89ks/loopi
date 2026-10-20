import numpy as np

from helpers import fetch_one
from loopi_brain.pipeline import asr, speech

RATE = speech.SAMPLE_RATE


def test_the_envelope_follows_the_loudness_every_100_ms():
    audio = np.zeros(RATE * 2, dtype=np.float32)
    audio[RATE // 2: RATE] = 0.1  # loud from 0.5 s to 1.0 s
    env = speech.envelope(audio, 0.0, 2.0)
    assert len(env) == 20
    assert env[0] == speech.NOISE_FLOOR_MIN_DBFS  # digital silence is floored, never -inf
    assert abs(env[7] - 20 * np.log10(0.1)) < 0.5  # 0.7 s: the loud part, -20 dBFS
    assert speech.envelope(audio, 1.95, 2.0) == []  # less than one 100 ms step


def test_detection_returns_each_regions_envelope(tmp_path, monkeypatch):
    from helpers import make_opus

    path = tmp_path / "a.opus"
    make_opus(path, seconds=3)
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [asr.Region(0.5, 2.5)])
    ((region, snr, env),) = asr.detect_file(path)
    assert region == asr.Region(0.5, 2.5) and len(env) == 20

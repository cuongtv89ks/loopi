import numpy as np
import pytest

from loopi_brain import audio_level
from loopi_brain.audio_level import dbfs, level_gain, noise_floor

SR = audio_level.SAMPLE_RATE


def speech_like(peak_db: float, floor_db: float, seconds: float = 10, seed: int = 1) -> np.ndarray:
    """Half-second 440 Hz bursts peaking at ``peak_db`` every other half second, over white noise with an
    RMS of ``floor_db`` (both dBFS), as 16 kHz float32."""
    t = np.arange(int(seconds * SR)) / SR
    bursts = np.sin(2 * np.pi * 440 * t) * (np.floor(t * 2) % 2 == 0) * 10 ** (peak_db / 20)
    noise = np.random.default_rng(seed).normal(0.0, 10 ** (floor_db / 20), t.size)
    return (bursts + noise).astype(np.float32)


def test_quiet_speech_is_raised_to_minus_6_dbfs_at_the_99_9th_percentile():
    quiet = speech_like(-30, -70)
    gain = level_gain(quiet)
    assert gain == pytest.approx(0.5 / np.percentile(np.abs(quiet), 99.9))
    assert dbfs(gain) == pytest.approx(24.0, abs=0.2)
    assert np.percentile(np.abs(quiet * gain), 99.9) == pytest.approx(0.5)


def test_loud_audio_is_never_attenuated():
    assert level_gain(speech_like(-3, -50)) == 1.0
    assert level_gain(np.full(SR, 0.9, dtype=np.float32)) == 1.0


@pytest.mark.parametrize("samples", [np.zeros(SR, dtype=np.float32), np.zeros(0, dtype=np.float32)])
def test_silence_and_empty_input_get_no_gain(samples):
    with np.errstate(all="raise"):  # no division by zero, no log of zero
        assert level_gain(samples) == 1.0
        assert noise_floor(samples) == 0.0


def test_less_than_one_frame_has_no_noise_floor_but_still_a_bounded_gain():
    assert noise_floor(np.full(100, 0.001, dtype=np.float32)) == 0.0
    assert level_gain(np.full(100, 0.001, dtype=np.float32)) == pytest.approx(10 ** (30 / 20))


def test_the_gain_stops_at_max_gain_db():
    whisper = speech_like(-60, -100)  # wants +54 dB
    assert level_gain(whisper) == pytest.approx(10 ** (30 / 20))
    assert level_gain(whisper, max_gain_db=12.0) == pytest.approx(10 ** (12 / 20))
    assert level_gain(whisper, max_gain_db=0.0) == 1.0
    assert level_gain(whisper, max_gain_db=-5.0) == 1.0


def test_isolated_one_sample_glitches_do_not_spoil_the_gain():
    # Real segments carry single-sample glitches at 0 dBFS; a peak-based gain would be 1.0 here.
    speech = speech_like(-30, -75, seconds=60)
    glitched = speech.copy()
    glitched[::SR] = 1.0  # one full-scale sample per second
    assert np.abs(glitched).max() == 1.0
    assert level_gain(glitched) == pytest.approx(level_gain(speech), rel=0.01)
    assert dbfs(level_gain(glitched)) == pytest.approx(24.0, abs=0.2)


def test_a_noise_only_night_segment_stays_a_soft_hiss():
    # The USB mic's room noise as measured on 1 336 segments: about -55 dBFS RMS, day and night.
    night = np.random.default_rng(3).normal(0.0, 10 ** (-55 / 20), 60 * SR).astype(np.float32)
    gain = level_gain(night)
    assert 1.0 < gain < level_gain(speech_like(-40, -75, seconds=60)), "noise gets less gain than speech"
    assert dbfs(noise_floor(night * gain)) == pytest.approx(-40.0, abs=0.01), "the hiss stops at -40 dBFS"
    # After the device's own +20 dB the same room is already at -35 dBFS: no further gain.
    assert level_gain(night * np.float32(10.0)) == 1.0

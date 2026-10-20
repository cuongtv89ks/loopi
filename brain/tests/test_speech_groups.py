import datetime as dt

import numpy as np

from loopi_brain.pipeline import speech
from loopi_brain.pipeline.speech import Decision, Span

T = dt.datetime(2026, 10, 21, 1, 0, tzinfo=dt.UTC)
RATE = speech.SAMPLE_RATE


def span(id, device, start, end, snr, state="waiting"):
    return Span(id, device, T + dt.timedelta(seconds=start), T + dt.timedelta(seconds=end), snr, state)


def tone(seconds, level):
    t = np.arange(int(seconds * RATE)) / RATE
    return (level * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_snr_is_speech_over_the_microphones_own_noise():
    rng = np.random.default_rng(1)
    noise = (rng.standard_normal(RATE * 4) * 0.001).astype(np.float32)  # ≈ -60 dBFS
    audio = noise.copy()
    audio[RATE:RATE * 2] += tone(1, 0.1)  # a loud second
    level = speech.noise_level(audio)
    assert 30 < speech.snr_db(audio, 1.0, 2.0, level) < 45
    assert speech.snr_db(audio, 3.0, 3.5, level) < 6  # noise only
    assert speech.snr_db(audio, 4.0, 4.5, level) == 0.0  # past the end: no frames


def test_a_noise_gate_does_not_make_a_quiet_microphone_win():
    gated = np.zeros(RATE * 4, dtype=np.float32)  # digital silence between words
    gated[RATE:RATE * 2] = tone(1, 0.003)  # the speech, far away: ≈ -53 dBFS
    rng = np.random.default_rng(2)
    near = (rng.standard_normal(RATE * 4) * 0.003).astype(np.float32)  # a real room: ≈ -50 dBFS noise
    near[RATE:RATE * 2] += tone(1, 0.3)  # the same speech, close
    far_snr = speech.snr_db(gated, 1.0, 2.0, speech.noise_level(gated))
    near_snr = speech.snr_db(near, 1.0, 2.0, speech.noise_level(near))
    assert speech.noise_level(gated) == 10 ** (speech.NOISE_FLOOR_MIN_DBFS / 20)
    assert near_snr > far_snr


def test_overlapping_speech_on_two_microphones_is_one_group_and_the_clearer_one_is_kept():
    pi, cam = span("a", "pi", 0, 4, 25.0), span("b", "cam", 0.8, 4.6, 18.0)  # the camera lags by 0.8 s
    (group,) = speech.groups([pi, cam])
    assert speech.decide(group) == [Decision("a", "chosen"), Decision("b", "covered", "a")]


def test_speech_only_one_room_heard_is_kept_on_its_own():
    alone = span("a", "pi", 0, 3, 12.0)
    elsewhere = span("b", "cam", 30, 33, 15.0)
    groups = speech.groups([alone, elsewhere])
    assert [g[0].id for g in groups] == ["a", "b"]
    assert [speech.decide(g) for g in groups] == [[Decision("a", "chosen")], [Decision("b", "chosen")]]


def test_a_region_the_winner_hardly_heard_is_transcribed_too():
    pi = span("a", "pi", 0, 4, 25.0)
    cam = span("b", "cam", 3, 12, 15.0)  # overlaps 3 s (with the 2 s slack) of its 9 s
    assert speech.decide(speech.groups([pi, cam])[0]) == [Decision("a", "chosen"), Decision("b", "chosen")]


def test_the_same_microphones_regions_do_not_chain_into_one_group():
    first, second = span("a", "pi", 0, 2, 20.0), span("b", "pi", 2.5, 4, 20.0)
    assert len(speech.groups([first, second])) == 2


def test_late_audio_keeps_the_decision_already_made():
    chosen = span("a", "cam", 0, 4, 10.0, state="chosen")
    late = span("b", "pi", 0, 4, 30.0)  # clearer, but too late to be chosen
    assert speech.decide(speech.groups([chosen, late])[0]) == [Decision("b", "covered", "a")]


def test_three_microphones_keep_one_transcript():
    a, b, c = span("a", "pi", 0, 4, 20.0), span("b", "cam1", 0.5, 4.5, 26.0), span("c", "cam2", 0.2, 4.2, 14.0)
    assert speech.decide(speech.groups([a, b, c])[0]) == [
        Decision("b", "chosen"), Decision("a", "covered", "b"), Decision("c", "covered", "b")]


def test_ties_go_to_the_earlier_region_then_the_smaller_device():
    a, b = span("a", "pi", 0.5, 4, 20.0), span("b", "cam", 0, 4, 20.0)
    assert speech.decide(speech.groups([a, b])[0])[0] == Decision("b", "chosen")

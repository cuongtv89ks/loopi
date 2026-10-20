import datetime as dt

import numpy as np

from loopi_brain.pipeline import speech
from loopi_brain.pipeline.speech import Decision, Span

T = dt.datetime(2026, 10, 21, 1, 0, tzinfo=dt.UTC)
HOP = speech.ENVELOPE_HOP_S


def talk(seconds, seed):
    """A speech-like loudness envelope: syllables of random length and level over a -60 dB room."""
    rng = np.random.default_rng(seed)
    env, n = [], int(seconds / HOP)
    while len(env) < n:
        env += [-60.0] * int(rng.integers(1, 4)) + [float(rng.uniform(-35, -15))] * int(rng.integers(2, 6))
    return env[:n]


def span(id, device, start, env, snr, state="waiting"):
    return Span(id, device, T + dt.timedelta(seconds=start), T + dt.timedelta(seconds=start + len(env) * HOP), snr,
                state, tuple(env))


def test_the_same_sentence_on_two_microphones_matches():
    pi_env = talk(6, seed=1)
    noise = np.random.default_rng(9).normal(0, 1.5, len(pi_env))
    cam_env = [v - 12 + float(n) for v, n in zip(pi_env, noise)]  # quieter, noisier
    pi, cam = span("a", "pi", 0, pi_env, 25.0), span("b", "cam", 0.8, cam_env, 12.0)  # the camera lags 0.8 s
    assert speech.same_sound(cam, pi)
    assert speech.decide(speech.groups([pi, cam])[0]) == [Decision("a", "chosen"), Decision("b", "covered", "a")]


def test_two_different_conversations_are_both_kept():
    living, bedroom = span("a", "pi", 0, talk(6, seed=1), 25.0), span("b", "cam", 0.3, talk(6, seed=2), 15.0)
    assert not speech.same_sound(bedroom, living)
    assert speech.decide(speech.groups([living, bedroom])[0]) == [Decision("a", "chosen"), Decision("b", "chosen")]


def test_a_region_without_an_envelope_keeps_the_time_rule():
    pi = span("a", "pi", 0, talk(6, seed=1), 25.0)
    old = Span("b", "cam", T + dt.timedelta(seconds=0.5), T + dt.timedelta(seconds=6), 12.0)  # stored before 014
    assert speech.same_sound(old, pi)
    assert speech.decide(speech.groups([pi, old])[0])[1] == Decision("b", "covered", "a")


def test_a_flat_envelope_is_not_a_match():
    pi = span("a", "pi", 0, talk(6, seed=1), 25.0)
    hum = span("b", "cam", 0, [-40.0] * 60, 15.0)  # a steady fan: no shape to compare
    assert not speech.same_sound(hum, pi)


def test_too_little_common_time_counts_as_the_same():
    pi = span("a", "pi", 0, talk(6, seed=1), 25.0)
    edge = span("b", "cam", 7.9, talk(3, seed=3), 15.0)  # same group (within the slack), < 1 s common at every lag
    assert speech.same_sound(edge, pi)


def test_no_unrelated_pair_of_talk_envelopes_reaches_the_threshold():
    """S2's threshold, pinned on the same generator as the tests above: seeds 1-39 give 741 unrelated pairs (the
    second 0.3 s late); the best-lag correlation of none reaches SAME_SOUND_MIN, while one sentence heard twice
    (quieter, noisier, 0.8 s late) stays far above it."""
    envs = {seed: talk(6, seed) for seed in range(1, 40)}
    worst = max(speech.best_correlation(span("a", "pi", 0, envs[i], 1.0), span("b", "cam", 0.3, envs[j], 1.0))
                for i in envs for j in envs if i < j)
    assert worst < speech.SAME_SOUND_MIN

    noise = np.random.default_rng(9).normal(0, 1.5, len(envs[1]))
    heard_twice = [v - 12 + float(n) for v, n in zip(envs[1], noise)]
    assert speech.best_correlation(span("a", "pi", 0, envs[1], 1.0), span("b", "cam", 0.8, heard_twice, 1.0)) >= 0.9

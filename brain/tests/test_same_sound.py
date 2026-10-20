import datetime as dt
import warnings

import numpy as np
import pytest

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
    assert speech.best_correlation(old, pi) is None and speech.best_correlation(pi, old) is None
    assert speech.same_sound(old, pi) and speech.same_sound(pi, old)
    assert speech.decide(speech.groups([pi, old])[0])[1] == Decision("b", "covered", "a")


def test_a_flat_envelope_is_not_a_match():
    pi = span("a", "pi", 0, talk(6, seed=1), 25.0)
    hum = span("b", "cam", 0, [-40.0] * 60, 15.0)  # a steady fan: no shape to compare
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # no division by a zero spread (RuntimeWarning): the flat guard holds
        assert not speech.same_sound(hum, pi)
        assert not speech.same_sound(pi, hum)


def test_too_little_common_time_is_not_the_same():
    pi = span("a", "pi", 0, talk(6, seed=1), 25.0)
    edge = span("b", "cam", 7.9, talk(3, seed=3), 15.0)  # same group (within the slack), < 1 s common at every lag
    assert speech.best_correlation(edge, pi) is None
    assert not speech.same_sound(edge, pi)  # nothing to compare: not the same (I1), the time rule would lose it


def test_a_region_under_a_second_keeps_the_time_rule():
    pi = span("a", "pi", 0, talk(6, seed=1), 25.0)
    blip = span("b", "cam", 2.0, [-60.0, -25.0, -22.0, -30.0, -60.0], 15.0)  # 0.5 s: too short to have a shape
    assert speech.best_correlation(blip, pi) is None
    assert speech.same_sound(blip, pi)  # a: the waiting region, under SAME_SOUND_MIN_FRAMES frames


def test_a_short_chosen_region_does_not_cover_a_long_one_of_another_conversation():
    """I1: a 0.5 s "ừ" of the Pi inside an 8 s camera region (another conversation) is not the same sound."""
    pi = span("a", "pi", 3.0, [-60.0, -25.0, -22.0, -30.0, -60.0], 25.0)
    cam = span("b", "cam", 0, talk(8, seed=2), 15.0)
    assert speech.best_correlation(cam, pi) is None and not speech.same_sound(cam, pi)
    assert speech.decide(speech.groups([pi, cam])[0]) == [Decision("a", "chosen"), Decision("b", "chosen")]


def test_two_chosen_regions_reaching_a_long_one_only_by_the_slack_do_not_cover_it():
    """I1: 20 s Pi regions ending at 5 s and starting at 15 s around a 20 s camera region (another conversation):
    the union of their slack-widened ends hears 14 s of 20, but neither is compared with it (too little in common)."""
    before, after = span("a", "pi", -15, talk(20, seed=1), 25.0), span("c", "pi", 15, talk(20, seed=3), 25.0)
    cam = span("b", "cam", 0, talk(20, seed=2), 15.0)
    assert speech.best_correlation(cam, before) is None and speech.best_correlation(cam, after) is None
    assert speech.decide(speech.groups([before, cam, after])[0]) == [
        Decision("a", "chosen"), Decision("c", "chosen"), Decision("b", "chosen")]


def test_a_chosen_region_far_from_a_waiting_one_is_never_compared(monkeypatch):
    """M1: only the chosen regions that reach the waiting one (within the slack) are correlated."""
    env = talk(6, seed=1)
    near, far = span("a", "pi", 0, env, 25.0, "chosen"), span("c", "pi", 600, talk(6, seed=3), 25.0, "chosen")
    cam = span("b", "cam", 0.5, env, 15.0)  # the same sound as the near region
    seen, real = [], speech.same_sound
    monkeypatch.setattr(speech, "same_sound", lambda a, b, slack=speech.GROUP_SLACK: seen.append((a.id, b.id)) or
                        real(a, b, slack))
    assert speech.decide([near, far, cam]) == [Decision("b", "covered", "a")]
    assert seen == [("b", "a")]


def test_a_part_of_a_longer_sound_that_starts_later_matches():
    """Pins the sign and size of the shift: the camera's region holds seconds 3-8 of the Pi's 10 s, starting at 3 s."""
    env = talk(10, seed=4)
    pi, cam = span("a", "pi", 0, env, 25.0), span("b", "cam", 3.0, env[30:80], 12.0)
    assert speech.best_correlation(cam, pi) >= 0.9
    assert speech.best_correlation(pi, cam) >= 0.9
    assert speech.same_sound(cam, pi) and speech.same_sound(pi, cam)


def test_a_short_common_stretch_is_not_compared():
    pi, late = span("a", "pi", 0, talk(6, seed=1), 25.0), span("b", "cam", 6.0, talk(6, seed=2), 15.0)
    assert speech.best_correlation(pi, late) is None  # at most 2 s in common at any lag: under half of 6 s
    assert not speech.same_sound(late, pi)  # nothing to compare: not the same (I1)


@pytest.mark.parametrize("offset", [0.0, 0.3, 0.6])
def test_no_unrelated_pair_of_6_second_talk_envelopes_reaches_the_threshold(offset):
    """S2's threshold, pinned on the same generator as the tests above, for 6 s regions only: seeds 1-39 give 741
    unrelated pairs (the second ``offset`` seconds late); the best-lag correlation of none reaches SAME_SOUND_MIN. The
    offsets are those whose false-match rate is 0 %: with larger ones (1-5 s) a few pairs still match, and shorter
    regions (under about 3 s) match by chance far more often (I2): both are left to acceptance §8.2."""
    envs = {seed: talk(6, seed) for seed in range(1, 40)}
    worst = max(speech.best_correlation(span("a", "pi", 0, envs[i], 1.0), span("b", "cam", offset, envs[j], 1.0))
                for i in envs for j in envs if i < j)
    assert worst < speech.SAME_SOUND_MIN


@pytest.mark.parametrize("lag", [0.8, 1.5])
def test_one_sentence_heard_twice_stays_far_above_the_threshold(lag):
    """Quieter, noisier, and ``lag`` seconds late."""
    pi_env = talk(6, seed=1)
    noise = np.random.default_rng(9).normal(0, 1.5, len(pi_env))
    heard_twice = [v - 12 + float(n) for v, n in zip(pi_env, noise)]
    assert speech.best_correlation(span("a", "pi", 0, pi_env, 1.0), span("b", "cam", lag, heard_twice, 1.0)) >= 0.9

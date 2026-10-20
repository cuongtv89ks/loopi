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
    pi, cam = span("p", "pi", 0, 4, 20.0), span("c", "cam", 1, 5, 20.0)  # equal SNR: the earlier region wins
    assert speech.decide(speech.groups([pi, cam])[0]) == [Decision("p", "chosen"), Decision("c", "covered", "p")]


def test_ties_in_start_go_to_the_smaller_device():
    pi, cam = span("p", "pi", 0, 4, 20.0), span("c", "cam", 0, 4, 20.0)
    assert speech.decide(speech.groups([pi, cam])[0]) == [Decision("c", "chosen"), Decision("p", "covered", "c")]


def test_the_chosen_regions_of_other_microphones_are_measured_as_a_union():
    p1, p2 = span("p1", "pi", 10, 12, 25.0), span("p2", "pi", 13, 15, 25.0)
    cam = span("c", "cam", 0, 14, 10.0)  # p1 and p2 hear 6 s of its 14 s together: under half, so it is kept
    (group,) = speech.groups([p1, p2, cam])
    assert speech.decide(group) == [Decision("p1", "chosen"), Decision("p2", "chosen"), Decision("c", "chosen")]


def test_the_slack_boundary_is_exclusive():
    pi = span("a", "pi", 0, 4, 20.0)
    assert len(speech.groups([pi, span("b", "cam", 5.9, 9, 15.0)])) == 1
    assert len(speech.groups([pi, span("b", "cam", 6.0, 9, 15.0)])) == 2
    assert len(speech.groups([pi, span("b", "cam", 7, 9, 15.0)])) == 2


def test_a_region_is_covered_when_the_chosen_speech_hears_at_least_half_of_it():
    pi = span("a", "pi", 0, 4, 25.0)

    def decided(lo, hi):
        return speech.decide(speech.groups([pi, span("b", "cam", lo, hi, 15.0)])[0])[1]

    assert decided(2, 10) == Decision("b", "covered", "a")  # exactly half
    assert decided(2.1, 10) == Decision("b", "chosen")  # just under half
    assert decided(3, 8) == Decision("b", "covered", "a")  # 60 %
    assert decided(3, 10) == Decision("b", "chosen")  # 3 of 7 s


def test_all_regions_of_the_winning_microphone_are_chosen():
    p1, p2 = span("p1", "pi", 0, 2, 30.0), span("p2", "pi", 6, 8, 5.0)
    cam = span("c", "cam", 1, 7, 20.0)
    assert speech.decide(speech.groups([p1, cam, p2])[0]) == [
        Decision("p1", "chosen"), Decision("p2", "chosen"), Decision("c", "covered", "p1")]


def test_a_group_with_nothing_waiting_has_no_decisions():
    assert speech.decide([span("a", "pi", 0, 4, 20.0, state="chosen")]) == []


def test_a_long_region_links_the_short_ones_after_it():
    long = span("a", "pi", 0, 30, 20.0)
    short = [span("b", "cam", 1, 2, 10.0), span("c", "pi", 3, 4, 10.0), span("d", "cam", 25, 26, 10.0)]
    (group,) = speech.groups([long, *short])
    assert [s.id for s in group] == ["a", "b", "c", "d"]


def test_a_chain_through_a_third_microphone_forms_one_group():
    a, b, c = span("a", "pi", 0, 2, 20.0), span("b", "cam", 3, 5, 20.0), span("c", "cam2", 6, 8, 20.0)
    assert len(speech.groups([a, b, c])) == 1  # a and c never overlap within the slack: only through b


def test_a_chosen_region_of_the_same_microphone_never_covers_a_region():
    chosen = span("c", "pi", 0, 4, 20.0, state="chosen")
    waiting = span("w", "pi", 1, 3, 10.0)  # wholly heard by the chosen region, from the same microphone
    assert speech.decide([chosen, waiting]) == [Decision("w", "chosen")]


def test_covered_by_is_the_chosen_region_that_hears_most():
    c1 = span("c1", "pi", 0, 3, 20.0, state="chosen")
    c2 = span("c2", "cam2", 0, 8, 15.0, state="chosen")
    w = span("w", "cam", 0, 10, 10.0)  # c1 hears 5 s of it, c2 hears 10 s
    assert speech.decide([c1, c2, w]) == [Decision("w", "covered", "c2")]


def test_equal_hearing_and_snr_cover_by_the_smaller_id_whatever_the_order():
    c1 = span("c1", "pi", 0, 10, 15.0, state="chosen")
    c2 = span("c2", "cam2", 0, 10, 15.0, state="chosen")
    w = span("w", "cam", 0, 10, 10.0)
    assert speech.decide([c1, c2, w]) == [Decision("w", "covered", "c1")]
    assert speech.decide([c2, c1, w]) == [Decision("w", "covered", "c1")]

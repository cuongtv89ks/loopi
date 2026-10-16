import unicodedata

import pytest

from loopi_brain.evaluation import metrics
from loopi_brain.evaluation.metrics import Errors


def test_text_is_compared_without_case_punctuation_or_unicode_form():
    assert metrics.normalise("Mai, mình đi CHỢ nhé!") == "mai mình đi chợ nhé"
    decomposed = unicodedata.normalize("NFD", "Mai mình đi chợ")
    assert decomposed != "Mai mình đi chợ" and metrics.words(decomposed) == ["mai", "mình", "đi", "chợ"]
    assert metrics.normalise("  Thank you.  ") == metrics.normalise("thank you") == "thank you"
    assert metrics.normalise("내일 회의는 열 시예요.") == "내일 회의는 열 시예요"
    assert metrics.normalise("9:30 — “ok” ...") == "9 30 ok"  # symbols and punctuation are spaces
    assert metrics.words("...") == [] and metrics.chars(" - ") == []


def test_both_vietnamese_tone_mark_placements_are_the_same_word():
    old_style = "Mẹ khỏe không, tùy con. Hòa bình, thủy, họa, góa"
    new_style = "mẹ khoẻ không tuỳ con hoà bình thuỷ hoạ goá"
    assert metrics.normalise(old_style) == metrics.normalise(new_style) == new_style
    assert metrics.wer(old_style, new_style).edits == 0 and metrics.cer(old_style, new_style).edits == 0
    # Closed syllables, "qu" words and other diphthongs are written one way only and stay as they are.
    assert metrics.normalise("Hoàng toán khuya quý của mía") == "hoàng toán khuya quý của mía"


def test_word_errors_count_substitutions_deletions_and_insertions():
    assert metrics.wer("a b c d", "a x c d e") == Errors(substitutions=1, deletions=0, insertions=1, reference=4)
    assert metrics.wer("a b c d", "a x c d e").rate == 0.5
    assert metrics.wer("mai mình đi chợ mua rau", "Mai mình đi chợ mua rao.").rate == pytest.approx(1 / 6)
    assert metrics.wer("một hai ba", "") == Errors(0, 3, 0, 3)  # nothing heard: every word deleted
    assert metrics.wer("", "thank you").rate is None  # no reference words: no rate (the no-speech check counts these)
    assert metrics.wer("", "thank you") == Errors(0, 0, 2, 0)
    assert metrics.wer("đi chợ", "đi chợ") == Errors(0, 0, 0, 2) and metrics.wer("đi chợ", "đi chợ").rate == 0.0


def test_korean_is_scored_by_characters_without_spaces():
    # The space mistake ("열 시" heard as "열시") is no error; the dropped particle "는" is one.
    errors = metrics.cer("내일 회의는 열 시예요", "내일 회의 열시예요")
    assert errors == Errors(0, 1, 0, 9) and errors.rate == pytest.approx(1 / 9)


def test_corpus_rates_add_the_errors_of_every_clip():
    total = sum([metrics.wer("a b c d", "a x c d"), metrics.wer("e f", "")], Errors())
    assert total == Errors(1, 2, 0, 6) and total.rate == 0.5  # 3 errors over 6 words, not the mean of 25 % and 100 %


def test_an_alignment_of_long_clips_stays_exact():
    reference = " ".join(f"w{n}" for n in range(400))
    heard = " ".join(f"w{n}" for n in range(400) if n % 10)  # every tenth word missed
    assert metrics.wer(reference, heard) == Errors(0, 40, 0, 400)


def test_recall_hits_and_reciprocal_rank():
    # Two correct moments: one found first, one found at rank 7.
    assert metrics.recall_at([1, 7], relevant=2, k=5) == 0.5
    assert metrics.recall_at([1, 7], relevant=2, k=10) == 1.0
    # Capped (D22): eight correct moments and a perfect first five is a full score, not 5/8.
    assert metrics.recall_at([1, 2, 3, 4, 5], relevant=8, k=5) == 1.0
    assert metrics.hit_at([7], k=5) == 0.0 and metrics.hit_at([7], k=10) == 1.0
    assert metrics.reciprocal_rank([7, 3]) == pytest.approx(1 / 3)
    assert metrics.recall_at([], relevant=3, k=5) == 0.0 and metrics.reciprocal_rank([]) == 0.0
    with pytest.raises(ValueError):
        metrics.recall_at([1], relevant=0, k=5)  # a question without a correct moment is not scored


def test_intervals_carry_the_size_of_the_sample():
    low, high = metrics.wilson(18, 25)  # 72 % of 25 questions
    assert (round(low, 3), round(high, 3)) == (0.524, 0.857)
    assert metrics.wilson(0, 4)[0] == 0.0 and metrics.wilson(4, 4)[1] == 1.0 and metrics.wilson(0, 0) is None
    clips = [Errors(1, 0, 0, 10), Errors(3, 0, 0, 10), Errors(0, 0, 0, 10), Errors(6, 0, 0, 10)]
    low, high = metrics.bootstrap(clips)
    assert low < sum(clips, Errors()).rate < high and 0.0 <= low and high <= 0.6
    assert metrics.bootstrap(clips) == metrics.bootstrap(clips)  # the same seed, the same interval
    assert metrics.bootstrap(clips[:1]) is None and metrics.bootstrap([Errors(0, 0, 2, 0)] * 3) is None

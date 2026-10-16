import pytest
from pydantic import BaseModel

from loopi_brain.llm import ASR_CAVEAT, AnswerInvalid, clip, clipped, marker_line, parse_answer, spread, within


class Answer(BaseModel):
    title: clipped(10)
    n: int


def test_answers_are_parsed_and_long_text_is_clipped_not_rejected():
    answer = parse_answer('{"title": "Một buổi sáng rất dài ở nhà", "n": 2, "extra": 1}', Answer)
    assert answer.title == "Một buổi…" and len(answer.title) <= 10 and answer.n == 2


@pytest.mark.parametrize("text", ["not json", "[]", '{"title": "x"}', '{"title": "x", "n": "many"}'])
def test_unusable_answers_are_invalid(text):
    with pytest.raises(AnswerInvalid):
        parse_answer(text, Answer)


def test_clip_flattens_whitespace():
    assert clip("  a\n b  ", 10) == "a b"
    assert clip("abcdefghij", 5) == "abcd…"


def test_spread_keeps_order_and_covers_the_whole_list():
    assert spread(list(range(10)), 3) == [0, 3, 6]
    assert spread([1, 2], 5) == [1, 2]
    assert spread([1, 2], 0) == []


def test_within_stops_at_the_budget():
    assert within(["aaaa", "bbbb", "cccc"], 10) == ["aaaa", "bbbb"]
    assert within(["a" * 20], 10) == []


def test_the_asr_caveat_names_the_known_hallucinations():
    assert "Thank you." in ASR_CAVEAT and "Hẹn gặp lại các bạn" in ASR_CAVEAT


def test_a_marker_line_carries_the_note_only_when_there_is_one():
    assert marker_line("09:40", None) == "09:40 the user pressed the mark button"
    assert marker_line("09:40", "   ") == "09:40 the user pressed the mark button"
    assert marker_line("09:40", 7) == "09:40 the user pressed the mark button"  # a note that is not text is ignored
    assert marker_line("09:40", "chìa khoá") == "09:40 the user pressed the mark button: chìa khoá"
    long_note = marker_line("09:40", "x " * 200)
    assert long_note.startswith("09:40 the user pressed the mark button: x x") and long_note.endswith("…")
    assert len(long_note) == len("09:40 the user pressed the mark button: ") + 100

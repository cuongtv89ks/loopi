import traceback

import pytest
from pydantic import BaseModel

from loopi_brain.llm import (
    ASR_CAVEAT, AnswerInvalid, clip, clipped, marker_line, parse_answer, spread, storable, within,
)


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


# A recap cut off by num_predict in the middle of its first sentence (private speech, as a model would write it).
CUT_OFF = '{"main_events": ["Mẹ hẹn bác sĩ Lan lúc 9 giờ sáng mai'
PRIVATE_WORDS = ("Mẹ", "hẹn", "bác sĩ", "Lan", "giờ", "sáng", "chín")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (CUT_OFF, "the answer is not JSON (Unterminated string starting at, character 17)"),
        ("Mẹ hẹn bác sĩ Lan", "the answer is not JSON (Expecting value, character 0)"),
        ('{"title": "Mẹ hẹn bác sĩ Lan", "n": "chín giờ"}', "the answer does not fit (n: int_parsing)"),
        ('["Mẹ hẹn bác sĩ Lan"]', "the answer does not fit (answer: model_type)"),
        ('{"n": 9}', "the answer does not fit (title: missing)"),
    ],
)
def test_an_unusable_answer_says_where_and_why_but_never_quotes_the_answer(text, message):
    """The error is stored on the Day and Xử lý pages and logged with its traceback by the worker; what the model
    wrote is private (and may be about a range that is forgotten later)."""
    with pytest.raises(AnswerInvalid) as caught:
        parse_answer(text, Answer)
    assert str(caught.value) == f"{message}; answer of {len(text)} characters"
    assert caught.value.__cause__ is None and caught.value.__context__ is None  # nothing chained that quotes it
    printed = "".join(traceback.format_exception(caught.value))
    assert not any(word in printed for word in PRIVATE_WORDS)


def test_a_number_too_long_to_convert_is_not_json_either():
    text = '{"title": "x", "n": ' + "9" * 5000 + "}"
    with pytest.raises(AnswerInvalid) as caught:
        parse_answer(text, Answer)
    assert str(caught.value) == f"the answer is not JSON (ValueError); answer of {len(text)} characters"


def test_an_answer_with_many_problems_names_the_first_three():
    with pytest.raises(AnswerInvalid) as caught:
        parse_answer('{"title": [1], "n": [2]}', Answer)
    assert str(caught.value) == "the answer does not fit (title: string_type, n: int_type); answer of 24 characters"

    class Many(BaseModel):
        items: list[int]

    with pytest.raises(AnswerInvalid) as caught:
        parse_answer('{"items": ["a", "b", "c", "d", "e"]}', Many)
    assert str(caught.value) == (
        "the answer does not fit (items.0: int_parsing, items.1: int_parsing, items.2: int_parsing, 2 more);"
        " answer of 36 characters"
    )


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


def test_clip_drops_what_a_database_text_column_cannot_hold():
    assert clip("a\x00b \ud800c\udfffd\udbff", 20) == "ab cd"  # NUL and lone surrogates (both halves, any position)
    assert clip("\x00" * 5 + "abcdef", 4) == "abc…"  # the limit counts what is kept


def test_storable_drops_only_what_a_database_cannot_hold():
    assert storable(" a\x00b\n\ud800  c\udfff ") == " ab\n  c "  # whitespace is kept, unlike clip()


def test_a_model_answer_with_nul_or_a_lone_surrogate_is_cleaned_by_clipped_text():
    answer = parse_answer('{"title": "Bữa\\u0000 sáng\\ud83d", "n": 1}', Answer)
    assert answer.title == "Bữa sáng"

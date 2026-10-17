"""Shared pieces of the text tasks (chapter summaries, day recaps, chat): what the prompts say about the
material, answer parsing, and bounding the material so a prompt always fits the context window."""
from __future__ import annotations

import json
import re
from typing import Annotated, TypeVar

from pydantic import BaseModel, BeforeValidator, ValidationError

T = TypeVar("T")
M = TypeVar("M", bound=BaseModel)

# Every prompt that quotes transcripts says this (P2c filters hallucinations better; prompts must not trust them).
ASR_CAVEAT = (
    "Speech lines come from automatic speech recognition of a home microphone. They can contain recognition "
    "mistakes, filler words, and lines invented on silence or noise, such as a lone \"Thank you.\" or a "
    "YouTube-style outro (\"Hẹn gặp lại các bạn trong những video tiếp theo\", \"Subscribe to the channel\"). "
    "Ignore such lines and anything that makes no sense in context; never build on them."
)
PICTURE_NOTE = "Picture lines are short English descriptions of single frames of a still camera, written by a vision model."


class AnswerInvalid(Exception):
    """The model answered, but not with the JSON that was asked for (retried like any job error)."""


SHOWN_PROBLEMS = 3


def _not_json(exc: ValueError) -> str:
    """Why the answer is not JSON: the JSON parser's fixed message and position (never the text around it)."""
    if isinstance(exc, json.JSONDecodeError):
        return f"the answer is not JSON ({exc.msg}, character {exc.pos})"
    return f"the answer is not JSON ({type(exc).__name__})"  # e.g. a number too long to convert


def _misfit(exc: ValidationError) -> str:
    """Where and why the answer does not fit the model: the field path and the error type of the first few problems,
    never the offending value (pydantic's own text quotes it). The answer models ignore unknown keys, so every
    path is made of the model's field names and list positions."""
    errors = exc.errors(include_input=False, include_context=False, include_url=False)
    shown = [f"{'.'.join(str(part) for part in e['loc']) or 'answer'}: {e['type']}" for e in errors[:SHOWN_PROBLEMS]]
    if len(errors) > SHOWN_PROBLEMS:
        shown.append(f"{len(errors) - SHOWN_PROBLEMS} more")
    return f"the answer does not fit ({', '.join(shown)})"


def parse_answer(text: str, model: type[M], invalid: type[Exception] = AnswerInvalid) -> M:
    """The model's JSON answer as ``model``; ``invalid`` (AnswerInvalid unless a task has its own) if it is not JSON
    or does not fit.

    The error says where and why, and how long the answer was, but never quotes any of it: it is stored on job
    rows shown on the Day and Xử lý pages, logged with its traceback, and may concern a range that is forgotten
    later. It is raised outside the ``except`` blocks, so no exception that quotes the answer is chained to it."""
    try:
        data = json.loads(text)
    except ValueError as exc:  # JSONDecodeError is a ValueError
        problem = _not_json(exc)
    else:
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            problem = _misfit(exc)
    raise invalid(f"{problem}; answer of {len(text)} characters")


# What a database text or JSONB column cannot hold: NUL, and the lone surrogates that a JSON answer can spell
# (\ud800 ...). PostgreSQL refuses them with an error that quotes the text around them.
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


def storable(text: str) -> str:
    """``text`` without the characters a database text or JSONB column cannot hold; nothing else changes."""
    return _UNSTORABLE.sub("", text)


def clip(text: str, limit: int) -> str:
    """``text`` on one line (whitespace collapsed, NUL and lone surrogates dropped), cut to ``limit`` characters;
    "…" marks a cut."""
    flat = " ".join(storable(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def clipped(limit: int):
    """A pydantic string type that is clipped to ``limit`` characters instead of rejected (truncate, don't reject)."""
    return Annotated[str, BeforeValidator(lambda v: clip(v, limit) if isinstance(v, str) else v)]


def spread(items: list[T], limit: int) -> list[T]:
    """At most ``limit`` items, evenly spread over ``items``, in their order."""
    if limit <= 0:
        return []
    if len(items) <= limit:
        return list(items)
    step = len(items) / limit
    return [items[int(k * step)] for k in range(limit)]


def within(lines: list[str], budget: int) -> list[str]:
    """The leading ``lines`` whose total length (with newlines) stays within ``budget`` characters."""
    kept: list[str] = []
    used = 0
    for line in lines:
        used += len(line) + 1
        if used > budget:
            break
        kept.append(line)
    return kept


def marker_line(at: str, note: str | None) -> str:
    """How a press of the mark button reads in a prompt: ``"09:40 the user pressed the mark button: note"``."""
    text = f": {clip(note, 100)}" if isinstance(note, str) and note.strip() else ""
    return f"{at} the user pressed the mark button{text}"

"""Shared pieces of the text tasks (chapter summaries, day recaps, chat): what the prompts say about the
material, answer parsing, and bounding the material so a prompt always fits the context window."""
from __future__ import annotations

import json
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


def parse_answer(text: str, model: type[M]) -> M:
    """The model's JSON answer as ``model``; AnswerInvalid if it is not JSON or misses a field."""
    try:
        return model.model_validate(json.loads(text))
    except (ValueError, ValidationError) as exc:  # JSONDecodeError is a ValueError
        raise AnswerInvalid(f"{exc}; got {text[:200]!r}") from exc


def clip(text: str, limit: int) -> str:
    """``text`` on one line (whitespace collapsed), cut to ``limit`` characters; "…" marks a cut."""
    flat = " ".join(text.split())
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

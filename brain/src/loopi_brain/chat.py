"""Questions about one's own memories, answered with citations (spec §10.4).

1. The chat model reads the time a question refers to as structured output (dates relative to the local
   today); anything unusable means no time filter. A question without a time word skips this call (it costs a
   whole qwen3:8b run on a CPU).
2. Hybrid retrieval (``search.py``) within that time.
3. No relevant moment: "Không tìm thấy trong ký ức." -- the model is not asked to answer at all.
4. Otherwise the chat model answers from the numbered sources only, citing them as [n]; the answer streams.

``chat_events`` yields (event name, payload) pairs that the web layer sends as Server-Sent Events.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import AsyncIterator

from pydantic import BaseModel, ConfigDict, Field
from psycopg_pool import AsyncConnectionPool

from .config import Settings
from .days import day_bounds_utc, local_now
from .llm import ASR_CAVEAT, PICTURE_NOTE, AnswerInvalid, clip, parse_answer
from .pipeline import moments as moment_rules
from .providers.base import ProviderError
from .providers.router import Router
from .search import Hit, clean_query, search_moments

log = logging.getLogger(__name__)
NOT_FOUND = "Không tìm thấy trong ký ức."
SOURCES = 8
SOURCE_TEXT_MAX = 400
ANSWER_MAX_TOKENS = 600
FILTER_MAX_TOKENS = 60
MAX_FILTER_DAYS = 366
CITATION = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")
# Words (vi, en, ko) or date-like numbers that can name a time; without one, no time filter is asked for.
TIME_WORDS = re.compile(
    r"\b(?:hôm|ngày|tuần|tháng|năm|sáng|trưa|chiều|tối|đêm|khuya|thứ|chủ nhật|lúc|giờ|mai|qua|nay|kia|trước|gần đây"
    r"|today|yesterday|tomorrow|morning|noon|afternoon|evening|night|week|month|year|monday|tuesday|wednesday"
    r"|thursday|friday|saturday|sunday|last|ago|recently|when)\b"
    r"|어제|오늘|내일|그제|아침|점심|저녁|밤|주말|지난|요일|\d{1,4}\s*[/.\-]\s*\d{1,2}",
    re.IGNORECASE,
)
FILTER_SCHEMA = {
    "type": "object",
    "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
    "required": ["from", "to"],
}
FILTER_SYSTEM = (
    "You read a question about the user's personal diary and say which local dates it is about. "
    "Today is {today}. Answer with JSON: from and to = the first and the last date the question refers to, as "
    "YYYY-MM-DD (the same date for a single day: \"hôm qua\" is yesterday, \"tuần trước\" is Monday to Sunday of "
    "last week, \"sáng nay\" is today); both empty strings when the question names no time."
)
ANSWER_SYSTEM = (
    "You answer questions about the user's own life, using only the numbered diary sources given, which a still "
    f"camera and a microphone in their home recorded. {PICTURE_NOTE} {ASR_CAVEAT} Answer briefly, in the "
    "language of the question (usually Vietnamese). Cite every fact with the number of its source in square "
    "brackets, like [2] or [1][3]. If the sources do not answer the question, say exactly "
    f"\"{NOT_FOUND}\" and do not guess."
)
KIND_WORDS = {"visual": "picture", "speech": "speech", "marker": "marked moment"}


class FilterAnswer(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    first: str = Field(alias="from")
    last: str = Field(alias="to")


def mentions_time(question: str) -> bool:
    return TIME_WORDS.search(question) is not None


def _date(text: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(text.strip())
    except ValueError:
        return None


def clean_filter(first: dt.date | None, last: dt.date | None, today: dt.date) -> tuple[dt.date | None, dt.date | None]:
    """A usable [first, last] range: one missing end copies the other, swapped ends are put in order, the future
    is cut off (all of it in the future: no filter) and at most MAX_FILTER_DAYS are kept (the latest ones)."""
    if first is None and last is None:
        return None, None
    first, last = first or last, last or first
    if first > last:
        first, last = last, first
    if first > today:
        return None, None
    last = min(last, today)
    first = max(first, last - dt.timedelta(days=MAX_FILTER_DAYS - 1))
    return first, last


async def time_filter(router: Router, question: str, today: dt.date) -> tuple[dt.date | None, dt.date | None]:
    """The local days ``question`` is about, or (None, None) for no filter (also when the model cannot say)."""
    messages = [
        {"role": "system", "content": FILTER_SYSTEM.format(today=today.strftime("%A, %Y-%m-%d"))},
        {"role": "user", "content": question},
    ]
    try:
        result = await router.chat("chat", messages, json_schema=FILTER_SCHEMA, temperature=0.0,
                                   max_tokens=FILTER_MAX_TOKENS)
        answer = parse_answer(result.text, FilterAnswer)
    except (ProviderError, AnswerInvalid) as exc:
        log.warning("chat: no time filter (%s)", exc)
        return None, None
    return clean_filter(_date(answer.first), _date(answer.last), today)


def source_lines(hits: list[Hit], zone) -> list[str]:
    lines = []
    for n, hit in enumerate(hits, start=1):
        kind = KIND_WORDS.get(hit.kind, hit.kind)
        if hit.kind == "speech":
            kind = f"speech ({hit.lang or '?'})"
        text = hit.text
        if hit.kind == "marker":
            note = moment_rules.marker_note(hit.item_meta or {})
            text = f"the user marked this moment: {note}" if note else "the user marked this moment"
        lines.append(f"[{n}] {hit.ts_start.astimezone(zone):%Y-%m-%d %H:%M}, {kind}: {clip(text, SOURCE_TEXT_MAX)}")
    return lines


def answer_messages(question: str, hits: list[Hit], zone) -> list[dict]:
    sources = "\n".join(source_lines(hits, zone))
    return [
        {"role": "system", "content": ANSWER_SYSTEM},
        {"role": "user", "content": f"Sources:\n{sources}\n\nQuestion: {question}"},
    ]


def cited(text: str, count: int) -> list[int]:
    """The source numbers ``text`` cites that exist (1..count), in first-cited order."""
    numbers: list[int] = []
    for match in CITATION.finditer(text):
        for part in re.split(r"[,;]", match.group(1)):
            n = int(part)
            if 1 <= n <= count and n not in numbers:
                numbers.append(n)
    return numbers


def describe_filter(first: dt.date, last: dt.date) -> str:
    if first == last:
        return f"Tìm trong ngày {first:%d/%m/%Y}"
    return f"Tìm từ {first:%d/%m/%Y} đến {last:%d/%m/%Y}"


async def chat_events(
    pool: AsyncConnectionPool,
    router: Router | None,
    question: str,
    settings: Settings,
    now: dt.datetime | None = None,
) -> AsyncIterator[tuple[str, dict]]:
    """The steps of one answer, as (event, payload): status, filter, sources (Hit list), token, answer, error, done."""
    zone = settings.zone
    question = clean_query(question)  # exactly as the search reads it: no control characters, one line, <= 500 chars
    if not question:
        yield "answer", {"text": "Hãy nhập một câu hỏi.", "not_found": True}
        yield "done", {}
        return
    yield "status", {"text": "Đang hiểu câu hỏi…"}
    first = last = None
    if router is not None and mentions_time(question):
        first, last = await time_filter(router, question, local_now(zone, now).date())
    if first is not None:
        yield "filter", {"text": describe_filter(first, last)}
    yield "status", {"text": "Đang tìm trong ký ức…"}
    start = day_bounds_utc(first, zone)[0] if first else None
    end = day_bounds_utc(last, zone)[1] if last else None
    result = await search_moments(pool, router, question, start=start, end=end, limit=SOURCES,
                                  max_distance=settings.search_max_distance,
                                  min_similarity=settings.search_min_similarity)
    if not result.hits:
        yield "answer", {"text": NOT_FOUND, "not_found": True}
        yield "done", {}
        return
    yield "sources", {"hits": result.hits}
    if router is None:
        yield "error", {"text": "Không trả lời được: cấu hình provider lỗi. Các nguồn liên quan ở trên."}
        yield "done", {}
        return
    yield "status", {"text": "Đang viết câu trả lời…"}
    pieces: list[str] = []
    try:
        async for piece, _version in router.stream_chat("chat", answer_messages(question, result.hits, zone),
                                                        max_tokens=ANSWER_MAX_TOKENS):
            pieces.append(piece)
            yield "token", {"text": piece}
    except ProviderError as exc:
        log.warning("chat answer failed: %s", exc)
        yield "error", {"text": "Không trả lời được lúc này: máy chủ model đang lỗi hoặc không chạy. "
                                "Các nguồn liên quan ở trên."}
        yield "done", {}
        return
    text = "".join(pieces).strip()
    # "Không tìm thấy" from the model, citing nothing, is a not-found answer, not an uncited one.
    not_found = text.startswith(NOT_FOUND.rstrip(".")) and not cited(text, len(result.hits))
    yield "answer", {"text": text, "not_found": not_found}
    yield "done", {}

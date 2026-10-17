"""The vlog's script (spec §10.3 "LLM viết kịch bản dưới dạng JSON"; P3 design §4.3): a Vietnamese title and up to
HIGHLIGHTS_MAX highlights chosen among the day's candidates by their refs, each with one line, at most AUDIO_MAX
with their own sound. Written by the recap's model (D23, ruling R1); when the model cannot answer usefully, a rule
picks them, so every evening has a vlog. Nothing here is logged but counts."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from pydantic import BaseModel

from ..llm import AnswerInvalid, clipped, parse_answer
from ..providers.base import ProviderError, ProviderUnavailable
from ..providers.router import Router
from .material import Candidate, Material

log = logging.getLogger(__name__)

TITLE_MAX = 80
LINE_MAX = 120
HIGHLIGHTS_MAX = 10
HIGHLIGHTS_RULE = 8
AUDIO_MAX = 3
AUDIO_RULE = 2
SCRIPT_MAX_TOKENS = 800
RULE = "rule"
KIND_LABELS = {"marker": "đánh dấu", "notable": "khung hình đáng chú ý", "talk": "hội thoại"}
SCRIPT_SYSTEM = (
    "Bạn dựng kịch bản cho một vlog ngắn (khoảng một phút) về một ngày ở nhà, từ danh sách khoảnh khắc nổi bật. "
    "Chọn 6 đến 10 khoảnh khắc đáng xem nhất bằng mã của chúng (H1, H2, …), mỗi khoảnh khắc một câu mô tả ngắn, "
    "tự nhiên, bằng tiếng Việt, không bịa thêm điều không có trong mô tả. Chỉ đặt audio=true cho tối đa 3 khoảnh "
    "khắc có ghi '(có tiếng)'. Đặt một tiêu đề ngắn cho cả ngày. Lời nói trong danh sách do máy nhận dạng, có thể sai."
)
SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": TITLE_MAX},
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "line": {"type": "string", "maxLength": LINE_MAX},
                    "audio": {"type": "boolean"},
                },
                "required": ["ref", "line", "audio"],
            },
            "maxItems": HIGHLIGHTS_MAX,
        },
    },
    "required": ["title", "highlights"],
}


class HighlightAnswer(BaseModel):
    ref: str
    line: clipped(LINE_MAX)
    audio: bool = False


class ScriptAnswer(BaseModel):
    title: clipped(TITLE_MAX)
    highlights: list[HighlightAnswer]


@dataclass(frozen=True)
class Highlight:
    candidate: Candidate
    line: str
    audio: bool


@dataclass(frozen=True)
class Script:
    title: str
    highlights: list[Highlight]
    model_version: str


def default_title(day: dt.date) -> str:
    return f"Ngày {day:%d/%m}"


def script_messages(material: Material, zone) -> list[dict]:
    lines = []
    for c in material.candidates:
        sound = " (có tiếng)" if c.audio_item else ""
        lines.append(f"{c.ref} · {c.at.astimezone(zone):%H:%M} · {KIND_LABELS[c.kind]}{sound} · {c.text}")
    user = f"Ngày {material.day:%d/%m/%Y}. Các khoảnh khắc:\n" + "\n".join(lines)
    return [{"role": "system", "content": SCRIPT_SYSTEM}, {"role": "user", "content": user}]


def _cap_audio(highlights: list[Highlight], limit: int) -> list[Highlight]:
    out, used = [], 0
    for h in highlights:
        audio = h.audio and h.candidate.audio_item is not None and used < limit
        used += audio
        out.append(Highlight(h.candidate, h.line, audio))
    return out


def resolve(answer: ScriptAnswer, material: Material) -> list[Highlight]:
    """The answer's highlights that name a candidate, each candidate once, in time order, at most HIGHLIGHTS_MAX,
    sound only where the candidate has it and on AUDIO_MAX of them at most."""
    by_ref = {c.ref: c for c in material.candidates}
    chosen: dict[str, Highlight] = {}
    for h in answer.highlights:
        ref = h.ref.strip().upper()
        if ref in by_ref and ref not in chosen and h.line.strip():
            chosen[ref] = Highlight(by_ref[ref], h.line.strip(), h.audio)
    ordered = sorted(chosen.values(), key=lambda h: h.candidate.at)[:HIGHLIGHTS_MAX]
    return _cap_audio(ordered, AUDIO_MAX)


def rule_script(material: Material) -> Script:
    """The fallback: the HIGHLIGHTS_RULE best candidates in time order, their own text as lines, sound on the first
    AUDIO_RULE conversations that have it."""
    best = sorted(material.candidates, key=lambda c: (-c.score, c.at))[:HIGHLIGHTS_RULE]
    ordered = sorted(best, key=lambda c: c.at)
    highlights = _cap_audio([Highlight(c, c.text[:LINE_MAX], c.kind == "talk") for c in ordered], AUDIO_RULE)
    return Script(default_title(material.day), highlights, RULE)


async def write_script(router: Router, material: Material, zone) -> Script:
    """The model's script, or the rule's when the model is unavailable, fails or names no usable candidate."""
    if not material.candidates:
        return rule_script(material)
    try:
        result = await router.chat("recap", script_messages(material, zone), json_schema=SCRIPT_SCHEMA,
                                   temperature=0.0, max_tokens=SCRIPT_MAX_TOKENS)
        answer = parse_answer(result.text, ScriptAnswer)
    except (ProviderUnavailable, ProviderError, AnswerInvalid) as exc:
        log.warning("vlog script of %s: the rule script is used (%s)", material.day, type(exc).__name__)
        return rule_script(material)
    highlights = resolve(answer, material)
    if not highlights:
        log.warning("vlog script of %s: the model named no usable highlight; the rule script is used", material.day)
        return rule_script(material)
    return Script(answer.title.strip() or default_title(material.day), highlights, result.model_version)


def to_json(written: Script) -> dict:
    """The script as stored on the vlog row (times in UTC ISO 8601)."""
    return {
        "title": written.title,
        "highlights": [
            {
                "ref": h.candidate.ref,
                "kind": h.candidate.kind,
                "at": h.candidate.at.astimezone(dt.UTC).isoformat(),
                "keyframe_id": h.candidate.frame.keyframe_id,
                "jpeg": h.candidate.frame.path,
                "line": h.line,
                "speech": h.candidate.text if h.candidate.kind == "talk" else None,
                "audio_item": h.candidate.audio_item if h.audio else None,
                "audio_offset": h.candidate.audio_offset,
            }
            for h in written.highlights
        ],
    }

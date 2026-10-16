"""transcribe_audio: level normalisation, then Silero VAD + faster-whisper per speech region (spec §9.2, D19).

Step 4: the raw audio is kept like video (spec D20, 7 days by default); ``retention.py`` then deletes it
together with its playback copy ``view/<device>/YYYY/MM/DD/<id>.opus`` (see ``media.normalise_audio``).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import anyio
import numpy as np

from .. import audio_level, devices
from ..config import ASR_RULES, Settings
from . import dispatch, jobs
from .context import PipelineContext, get_context
from .jobs import job_app
from .state import error_text, finish_item, item_exists, lock_pending_item, mark_item_done, record_item_error

log = logging.getLogger(__name__)
SAMPLE_RATE = 16_000
MERGE_GAP_S = 1.0
MIN_REGION_S = 0.5
# Whisper detects one language per region from its start, so regions stay short enough for a
# Vietnamese–Korean conversation to change language between them.
MAX_REGION_S = 20.0
VAD_THRESHOLD = 0.5  # Silero's default speech probability; LOOPI_ASR_VAD_THRESHOLD
WHISPER_SIZE = "large-v3-turbo"
WHISPER_COMPUTE = "int8"
# Whisper invents these on silence, music or TV noise. A segment is dropped only when such phrases
# make up most of it (HALLUCINATION_COVERAGE of its normalised text), never because real speech
# merely contains one ("thanks for watching the kids").
HALLUCINATIONS = (
    "hẹn gặp lại các bạn trong những video tiếp theo",
    "hãy subscribe cho kênh ghiền mì gõ để không bỏ lỡ những video hấp dẫn",
    "cảm ơn các bạn đã theo dõi",
    "hãy subscribe cho kênh",
    "đăng ký kênh",
    "đăng kí kênh",
    "시청해 주셔서 감사합니다",
    "구독과 좋아요 부탁드립니다",
    "구독과 좋아요",
    "thanks for watching",
    "thank you for watching",
    "subtitles by the amara.org community",
)
# Fragments are too common in real speech to condemn a segment alone ("Hẹn gặp lại em" is a
# goodbye): they count toward the coverage only when a full phrase above matched in the same segment
# ("Cảm ơn các bạn đã theo dõi và hẹn gặp lại").
HALLUCINATION_FRAGMENTS = (
    "hẹn gặp lại",
)
HALLUCINATION_COVERAGE = 0.8
# Rules that can be switched on (LOOPI_ASR_RULES; none by default, spec §13: only on the golden set's numbers).
# Each one drops more of what Whisper writes on silence, TV or room noise, at the risk of dropping real speech.
RULES = ASR_RULES
# "outro": a segment that is a video's or a newscast's sign-off, worded in ways the phrase list above does not cover
# ("Hãy subscribe cho kênh … video …", "Hãy đăng ký kênh để ủng hộ mình nhé", "MBC 뉴스 …입니다"). The patterns read
# the text in lower case with punctuation as spaces; like P2a's phrases, a segment is dropped only when the matched
# spans make up HALLUCINATION_COVERAGE of it, never because real speech merely contains a sign-off ("Mai mình đi
# chợ nhé, hãy đăng ký kênh để ủng hộ mình"). The optional closing particles belong to the sign-off, as whole words
# ("nha" is not the start of "nhanh").
OUTRO_PATTERNS = (
    re.compile(
        r"\bhãy (?:like (?:và )?)?(?:subscribe|đăng k[ýí]) (?:cho )?kênh\b.*\b(?:video|ủng hộ)\b"
        r"(?: (?:mình|nhé|nha|nhá|ạ)\b)*"
    ),
    re.compile(r"^mbc 뉴스 \w+입니다$"),
)
# "lone_phrase": a speech region whose every segment is one of these (lower case, no spaces or punctuation): what
# Whisper wrote most often on a quiet home's normalised noise (P2a: "Thank you." 26 times in one night, "Yeah.").
LONE_PHRASES = frozenset({"thankyou", "thankyouverymuch", "thanks", "yeah", "amen", "mmhmm", "bye", "you"})
# "short_foreign": a short region in a language the household rarely speaks, chosen with low confidence (on noise
# Whisper often picks English with avg_logprob ≈ -0.85 and no_speech_prob 0.0, which the P2a filter keeps).
FOREIGN_LANGUAGES = frozenset({"en"})
FOREIGN_MAX_PROB = 0.5
FOREIGN_MAX_WORDS = 3


@dataclass(frozen=True)
class Region:
    start: float  # seconds from the start of the file
    end: float


@dataclass(frozen=True)
class SegmentResult:
    start: float  # seconds from the start of the region
    end: float
    text: str
    avg_logprob: float
    no_speech_prob: float
    compression_ratio: float


@dataclass(frozen=True)
class RegionResult:
    lang: str
    lang_prob: float
    segments: list[SegmentResult]


class Transcriber(Protocol):
    def transcribe(self, chunk: np.ndarray) -> RegionResult: ...


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFC", text).lower()
    return re.sub(r"[\W_]+", "", text)


def _spaced(text: str) -> str:
    """Lower case, NFC, punctuation as spaces, whitespace collapsed (what the outro patterns read)."""
    return " ".join(re.sub(r"[\W_]+", " ", unicodedata.normalize("NFC", text).lower()).split())


def is_outro(text: str) -> bool:
    """True when the outro patterns cover at least HALLUCINATION_COVERAGE of the segment (letters and digits only,
    as in ``is_hallucination``)."""
    spaced = _spaced(text)
    covered = [False] * len(spaced)
    for pattern in OUTRO_PATTERNS:
        for match in pattern.finditer(spaced):
            covered[match.start(): match.end()] = [True] * (match.end() - match.start())
    total = len(spaced.replace(" ", ""))
    hit = sum(1 for char, mark in zip(spaced, covered) if mark and char != " ")
    return total > 0 and hit >= HALLUCINATION_COVERAGE * total


_HALLUCINATIONS = tuple(sorted({_normalise(p) for p in HALLUCINATIONS}, key=len, reverse=True))
_FRAGMENTS = tuple(sorted({_normalise(p) for p in HALLUCINATION_FRAGMENTS}, key=len, reverse=True))


def _mark(covered: list[bool], norm: str, phrases: tuple[str, ...]) -> bool:
    """Mark every occurrence of ``phrases`` in ``norm`` as covered; true if there was one."""
    found = False
    for phrase in phrases:
        start = norm.find(phrase)
        while start != -1:
            found = True
            covered[start: start + len(phrase)] = [True] * len(phrase)
            start = norm.find(phrase, start + 1)
    return found


def is_hallucination(text: str) -> bool:
    """True when known hallucinated phrases cover at least 80 % of the segment (lower case,
    NFC, punctuation and spaces removed); fragments count only next to a full phrase."""
    norm = _normalise(text)
    if not norm:
        return False
    covered = [False] * len(norm)
    if _mark(covered, norm, _HALLUCINATIONS):
        _mark(covered, norm, _FRAGMENTS)
    return sum(covered) >= HALLUCINATION_COVERAGE * len(norm)


def keep_segment(seg: SegmentResult) -> bool:
    """Drop text without a letter or digit, likely non-speech, repetition loops and known hallucinations."""
    if not _normalise(seg.text):
        return False
    if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
        return False
    if seg.compression_ratio > 2.4:
        return False
    return not is_hallucination(seg.text)


def kept_segments(result: RegionResult, rules: frozenset[str] = frozenset()) -> list[SegmentResult]:
    """The segments of one speech region worth storing: each one that passes ``keep_segment``, then the rules
    switched on in ``rules`` (names from RULES; an unknown name is a ValueError)."""
    unknown = sorted(rules - set(RULES))
    if unknown:
        raise ValueError(f"unknown ASR rules: {', '.join(unknown)} (known: {', '.join(RULES)})")
    kept = [s for s in result.segments if keep_segment(s)]
    if "outro" in rules:
        kept = [s for s in kept if not is_outro(s.text)]
    if kept and "lone_phrase" in rules and all(_normalise(s.text) in LONE_PHRASES for s in kept):
        return []
    if (kept and "short_foreign" in rules and result.lang in FOREIGN_LANGUAGES
            and result.lang_prob < FOREIGN_MAX_PROB
            and sum(len(s.text.split()) for s in kept) <= FOREIGN_MAX_WORDS):
        return []
    return kept


def _cap(chunks: list[tuple[float, float]]) -> list[Region]:
    """One merged group of speech chunks -> regions of at most MAX_REGION_S: split at the longest
    silence between chunks (of equally long ones, the one nearest the middle) while possible,
    else (one long chunk) into equal parts."""
    start, end = chunks[0][0], max(c[1] for c in chunks)
    if end - start <= MAX_REGION_S:
        return [Region(start, end)]
    if len(chunks) > 1:
        middle = (start + end) / 2

        def preference(k: int) -> tuple[float, float]:
            return round(chunks[k][0] - chunks[k - 1][1], 1), -abs(chunks[k][0] - middle)

        cut = max(range(1, len(chunks)), key=preference)
        return _cap(chunks[:cut]) + _cap(chunks[cut:])
    parts = int(-(-(end - start) // MAX_REGION_S))  # ceil
    step = (end - start) / parts
    return [Region(start + k * step, start + (k + 1) * step) for k in range(parts)]


def merge_regions(raw: list[dict], sample_rate: int = SAMPLE_RATE) -> list[Region]:
    """Silero sample ranges -> regions in seconds: gaps under 1 s merged, regions longer than 20 s
    split again, regions under 0.5 s dropped."""
    groups: list[list[tuple[float, float]]] = []
    for chunk in raw:
        span = (chunk["start"] / sample_rate, chunk["end"] / sample_rate)
        if groups and span[0] - max(c[1] for c in groups[-1]) < MERGE_GAP_S:
            groups[-1].append(span)
        else:
            groups.append([span])
    regions = [region for group in groups for region in _cap(group)]
    return [r for r in regions if r.end - r.start >= MIN_REGION_S]


def load_audio(path: Path) -> np.ndarray:
    """Decode any audio file (Ogg/Opus here) to 16 kHz mono float32 (PyAV, no ffmpeg binary needed)."""
    from faster_whisper import decode_audio

    return decode_audio(str(path), sampling_rate=SAMPLE_RATE)


def normalise(audio: np.ndarray, max_gain_db: float = audio_level.MAX_GAIN_DB) -> np.ndarray:
    """``audio`` raised by the shared level gain (spec D19) and clipped to [-1, 1]; unchanged when the
    gain is 1 (loud enough already, or silence)."""
    gain = audio_level.level_gain(audio, max_gain_db=max_gain_db)
    if gain == 1.0:
        return audio
    return np.clip(audio * np.float32(gain), -1.0, 1.0)


def speech_regions(audio: np.ndarray, threshold: float = VAD_THRESHOLD) -> list[Region]:
    """Where someone speaks, by the Silero VAD model bundled with faster-whisper (no download); ``threshold`` is the
    speech probability a frame needs."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    raw = get_speech_timestamps(
        audio, VadOptions(threshold=threshold, min_silence_duration_ms=500, speech_pad_ms=200), SAMPLE_RATE
    )
    return merge_regions(raw)


def pick_language(all_probs: list[tuple[str, float]], allowed: list[str]) -> tuple[str, float]:
    """The most likely language among ``allowed`` (Whisper's detector knows ~100)."""
    candidates = [(lang, prob) for lang, prob in all_probs if lang in allowed]
    return max(candidates, key=lambda c: c[1]) if candidates else (allowed[0], 0.0)


class WhisperTranscriber:
    """faster-whisper large-v3-turbo, int8 on CPU; the model loads on first use (≈ 1.6 GB download once)."""

    def __init__(self, settings: Settings):
        from faster_whisper.tokenizer import _LANGUAGE_CODES  # Whisper's language codes

        unknown = [code for code in settings.asr_language_list if code not in _LANGUAGE_CODES]
        if not settings.asr_language_list or unknown:  # fail at worker start, not on every audio job
            raise ValueError(f"LOOPI_ASR_LANGUAGES must list Whisper language codes; unknown: {unknown or 'none set'}")
        self._languages = settings.asr_language_list
        self._threads = settings.whisper_cpu_threads
        self._root = settings.model_dir / "whisper"
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    WHISPER_SIZE, device="cpu", compute_type=WHISPER_COMPUTE,
                    cpu_threads=self._threads, download_root=str(self._root),
                )
            return self._model

    def transcribe(self, chunk: np.ndarray) -> RegionResult:
        model = self._load()
        _lang, _prob, all_probs = model.detect_language(chunk)
        lang, prob = pick_language(all_probs, self._languages)
        segments, _info = model.transcribe(
            chunk, language=lang, beam_size=5, condition_on_previous_text=False, vad_filter=False
        )
        return RegionResult(lang, prob, [
            SegmentResult(s.start, s.end, s.text.strip(), s.avg_logprob, s.no_speech_prob, s.compression_ratio)
            for s in segments
        ])


def transcribe_file(
    path: Path,
    transcriber: Transcriber,
    max_gain_db: float = audio_level.MAX_GAIN_DB,
    vad_threshold: float = VAD_THRESHOLD,
) -> list[tuple[Region, RegionResult]]:
    """Blocking: decode, normalise the level, find speech, transcribe each region. Segment times stay
    region-relative. The live stage and the evaluation runner both go this way."""
    audio = normalise(load_audio(path), max_gain_db)
    results = []
    for region in speech_regions(audio, vad_threshold):
        chunk = audio[int(region.start * SAMPLE_RATE): int(region.end * SAMPLE_RATE)]
        results.append((region, transcriber.transcribe(chunk)))
    return results


async def _transcribe(ctx: PipelineContext, item_id: str) -> int:
    pool = ctx.pool
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT device_id, kind, started_at, raw_path, analysis_state FROM item WHERE id = %s", (item_id,)
        )
        item = await cur.fetchone()
    if item is None or item["kind"] != "audio" or item["analysis_state"] != "pending":
        return 0
    path = ctx.settings.media_root / item["raw_path"]
    if not path.is_file():
        await finish_item(pool, item_id, "skipped")
        return 0
    settings = ctx.settings
    rules = settings.asr_rule_set
    results = await anyio.to_thread.run_sync(
        transcribe_file, path, ctx.transcriber, settings.asr_max_gain_db, settings.asr_vad_threshold
    )
    version = ctx.router.model_version("asr")
    start: dt.datetime = item["started_at"]
    moments = 0
    async with pool.connection() as conn, conn.transaction():
        if not await lock_pending_item(conn, item_id):  # deleted while Whisper was running: nothing to store
            log.info("item %s was deleted during its transcription; nothing stored", item_id)
            return 0
        # Re-running replaces the item's transcript (moments go with their segments: ON DELETE CASCADE).
        await conn.execute("DELETE FROM transcript_segment WHERE item_id = %s", (item_id,))
        for region, result in results:
            kept = kept_segments(result, rules)
            if not kept:
                continue
            segment_ids = []
            for seg in kept:
                segment_id = devices.new_id()
                segment_ids.append(segment_id)
                await conn.execute(
                    "INSERT INTO transcript_segment (id, item_id, device_id, start_ts, end_ts, lang, text,"
                    " avg_logprob, no_speech_prob, lang_prob, model_version)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (segment_id, item_id, item["device_id"],
                     start + dt.timedelta(seconds=region.start + seg.start),
                     start + dt.timedelta(seconds=region.start + seg.end),
                     result.lang, seg.text.strip(), seg.avg_logprob, seg.no_speech_prob, result.lang_prob, version),
                )
            await conn.execute(
                "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, lang, item_id,"
                " transcript_segment_id, model_version) VALUES (%s, %s, %s, %s, 'speech', %s, %s, %s, %s, %s)",
                (devices.new_id(), item["device_id"],
                 start + dt.timedelta(seconds=region.start + kept[0].start),
                 start + dt.timedelta(seconds=region.start + kept[-1].end),
                 " ".join(s.text.strip() for s in kept), result.lang, item_id, segment_ids[0], version),
            )
            moments += 1
        await mark_item_done(conn, item_id)
    if moments:
        await dispatch.defer_embed(pool)
    log.info("item %s: %d speech regions, %d kept", item_id, len(results), moments)
    return moments


async def transcribe_audio(ctx: PipelineContext, item_id: str) -> int:
    """Transcribe a pending audio item; return the number of speech moments written."""
    try:
        return await _transcribe(ctx, item_id)
    except Exception as exc:
        if not await item_exists(ctx.pool, item_id):  # deleted while we worked (e.g. its file went first)
            log.info("item %s was deleted during its transcription: %s", item_id, error_text(exc))
            return 0
        await record_item_error(ctx.pool, item_id, exc)
        raise


@job_app.task(name=jobs.TRANSCRIBE_AUDIO, queue="audio", retry=jobs.RETRY)
async def transcribe_audio_job(item_id: str) -> None:
    await transcribe_audio(get_context(), item_id)

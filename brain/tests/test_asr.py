import datetime as dt
import os

import numpy as np
import psycopg
import pytest

from helpers import (
    T0,
    FakeTranscriber,
    fetch_all,
    fetch_one,
    insert_item,
    jobs_by_lock,
    make_ctx,
    make_opus,
    paired_device,
)
from loopi_brain.config import Settings
from loopi_brain.pipeline import asr
from loopi_brain.pipeline.asr import Region, RegionResult, SegmentResult

SR = asr.SAMPLE_RATE


def seg(text, start=0.0, end=2.0, logprob=-0.3, no_speech=0.05, ratio=1.2) -> SegmentResult:
    return SegmentResult(start, end, text, logprob, no_speech, ratio)


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


async def _audio_item(pool, settings, device_id, seconds=5, started_at=T0) -> str:
    item_id = await insert_item(pool, device_id, kind="audio", started_at=started_at, seconds=seconds)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    make_opus(settings.media_root / row["raw_path"], seconds=seconds)
    return item_id


def test_merge_regions_joins_short_gaps_and_drops_blips():
    raw = [
        {"start": 0, "end": 1 * SR},
        {"start": int(1.5 * SR), "end": 3 * SR},  # gap 0.5 s -> merged
        {"start": 5 * SR, "end": int(5.3 * SR)},  # 0.3 s blip alone -> dropped
        {"start": 8 * SR, "end": 10 * SR},
    ]
    assert asr.merge_regions(raw) == [Region(0.0, 3.0), Region(8.0, 10.0)]
    assert asr.merge_regions([]) == []


def test_a_minute_of_turn_taking_is_split_into_regions_of_at_most_20_s():
    # 12 turns of 4.4 s with 0.6 s pauses merge into one 60 s stretch; the 0.9 s pause after
    # turn 4 is the longest, so the first cut goes there.
    starts = [0.0, 5.0, 10.0, 15.0, 20.3, 25.3, 30.3, 35.3, 40.3, 45.3, 50.3, 55.3]
    raw = [{"start": int(s * SR), "end": int((s + 4.4) * SR)} for s in starts]
    regions = asr.merge_regions(raw)
    assert [(r.start, r.end) for r in regions] == [
        pytest.approx((0.0, 19.4), abs=0.01), pytest.approx((20.3, 39.7), abs=0.01),
        pytest.approx((40.3, 59.7), abs=0.01)]
    one_long_turn = asr.merge_regions([{"start": 0, "end": 60 * SR}])
    assert one_long_turn == [Region(0.0, 20.0), Region(20.0, 40.0), Region(40.0, 60.0)]


@pytest.mark.parametrize(
    "text",
    [
        "Hẹn gặp lại các bạn trong những video tiếp theo!",
        "Cảm ơn các bạn đã theo dõi.",
        "Hãy subscribe cho kênh Ghiền Mì Gõ để không bỏ lỡ những video hấp dẫn",
        "Cảm ơn các bạn đã theo dõi và hẹn gặp lại!",
        "시청해주셔서 감사합니다.",
        "Thanks for watching!",
        "Subtitles by the Amara.org community",
    ],
)
def test_known_hallucinations_are_dropped(text):
    assert asr.is_hallucination(text)
    assert not asr.keep_segment(seg(text))


@pytest.mark.parametrize(
    "text",
    ["Anh đã đăng ký kênh K+ chưa?", "Thanks for watching the kids yesterday!", "Hẹn gặp lại anh vào thứ Hai nhé"],
)
def test_real_speech_that_contains_a_phrase_is_kept(text):
    assert not asr.is_hallucination(text)
    assert asr.keep_segment(seg(text))


@pytest.mark.parametrize(
    ("segment", "kept"),
    [
        (seg("Mai mình đi chợ nhé"), True),
        (seg("내일 회의는 열 시예요"), True),
        (seg("   "), False),
        (seg("uh", logprob=-1.5, no_speech=0.8), False),
        (seg("probably real", logprob=-1.5, no_speech=0.4), True),
        (seg("la la la la la la la la", ratio=2.6), False),
    ],
)
def test_segment_filters(segment, kept):
    assert asr.keep_segment(segment) is kept


def test_pick_language_is_restricted_to_the_configured_ones():
    probs = [("zh", 0.6), ("ko", 0.25), ("vi", 0.1), ("en", 0.05)]
    assert asr.pick_language(probs, ["vi", "ko", "en"]) == ("ko", 0.25)
    assert asr.pick_language([("ja", 1.0)], ["vi", "ko"]) == ("vi", 0.0)


def test_silence_has_no_speech_regions(tmp_path):
    audio = asr.load_audio(make_opus(tmp_path / "silence.opus", seconds=5))
    assert audio.dtype == np.float32 and len(audio) == 5 * SR
    assert asr.speech_regions(audio) == []


async def test_speech_regions_become_segments_and_moments(pool, pki_settings, dev, jobs_open, monkeypatch):
    item_id = await _audio_item(pool, pki_settings, dev["device_id"])
    monkeypatch.setattr(asr, "speech_regions", lambda _audio: [Region(0.5, 2.5), Region(3.0, 4.5), Region(4.6, 4.9)])
    fake = FakeTranscriber(
        RegionResult("vi", 0.9, [seg("Mai mình đi chợ", 0.0, 1.0), seg("nhớ mua trứng nhé", 1.0, 2.0)]),
        RegionResult("ko", 0.8, [seg("네 알겠어요", 0.2, 1.4)]),
        RegionResult("en", 0.4, [seg("Thanks for watching!", 0.0, 0.3)]),
    )
    ctx = make_ctx(pool, pki_settings, transcriber=fake)
    assert await asr.transcribe_audio(ctx, item_id) == 2
    assert fake.chunks == [2 * SR, int(1.5 * SR), int(0.3 * SR)]
    segments = await fetch_all(pool, "SELECT * FROM transcript_segment ORDER BY start_ts")
    assert [(s["lang"], s["text"]) for s in segments] == [
        ("vi", "Mai mình đi chợ"), ("vi", "nhớ mua trứng nhé"), ("ko", "네 알겠어요")]
    assert segments[0]["start_ts"] == T0 + dt.timedelta(seconds=0.5)
    assert segments[2]["end_ts"] == T0 + dt.timedelta(seconds=4.4)
    assert {s["model_version"] for s in segments} == {"local:faster-whisper/large-v3-turbo-int8"}
    found = await fetch_all(pool, "SELECT * FROM moment ORDER BY ts_start")
    assert [(m["kind"], m["lang"], m["text"]) for m in found] == [
        ("speech", "vi", "Mai mình đi chợ nhớ mua trứng nhé"), ("speech", "ko", "네 알겠어요")]
    assert found[0]["transcript_segment_id"] == segments[0]["id"]
    assert (found[0]["ts_start"], found[0]["ts_end"]) == (T0 + dt.timedelta(seconds=0.5), T0 + dt.timedelta(seconds=2.5))
    item = await fetch_one(pool, "SELECT analysis_state, analysis_error FROM item WHERE id = %s", (item_id,))
    assert item == {"analysis_state": "done", "analysis_error": None}
    assert "embed" in await jobs_by_lock(pool)
    async with pool.connection() as conn:  # re-running replaces the transcript instead of duplicating it
        await conn.execute("UPDATE item SET analysis_state = 'pending' WHERE id = %s", (item_id,))
    assert await asr.transcribe_audio(ctx, item_id) == 2
    assert len(await fetch_all(pool, "SELECT id FROM transcript_segment")) == 3
    assert len(await fetch_all(pool, "SELECT id FROM moment")) == 2


async def test_hallucinated_text_on_noise_leaves_no_transcript(pool, pki_settings, dev, jobs_open, monkeypatch):
    item_id = await _audio_item(pool, pki_settings, dev["device_id"])
    monkeypatch.setattr(asr, "speech_regions", lambda _audio: [Region(0.0, 4.0)])
    tv_noise = RegionResult("vi", 0.5, [
        seg("Hẹn gặp lại các bạn trong những video tiếp theo", 0.0, 2.0),
        seg("Thanks for watching", 2.0, 3.0),
        seg("...", 3.0, 3.5, no_speech=0.9, logprob=-1.4),
    ])
    assert await asr.transcribe_audio(make_ctx(pool, pki_settings, transcriber=FakeTranscriber(tv_noise)), item_id) == 0
    assert await fetch_all(pool, "SELECT id FROM transcript_segment") == []
    assert await fetch_all(pool, "SELECT id FROM moment") == []
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "done"
    assert "embed" not in await jobs_by_lock(pool)


async def test_real_silence_is_done_without_calling_whisper(pool, pki_settings, dev, jobs_open):
    item_id = await _audio_item(pool, pki_settings, dev["device_id"])
    fake = FakeTranscriber()
    assert await asr.transcribe_audio(make_ctx(pool, pki_settings, transcriber=fake), item_id) == 0
    assert fake.chunks == []
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "done"


async def test_item_deleted_during_transcription_stores_nothing(pool, pki_settings, dev, jobs_open, monkeypatch):
    item_id = await _audio_item(pool, pki_settings, dev["device_id"])
    monkeypatch.setattr(asr, "speech_regions", lambda _audio: [Region(0.0, 2.0)])

    class DeletingTranscriber(FakeTranscriber):
        def transcribe(self, chunk):  # runs in the worker thread, like Whisper
            with psycopg.connect(pki_settings.database_url, autocommit=True) as conn:
                conn.execute("DELETE FROM item WHERE id = %s", (item_id,))
            return super().transcribe(chunk)

    speech = RegionResult("vi", 0.9, [seg("Mai mình đi chợ")])
    ctx = make_ctx(pool, pki_settings, transcriber=DeletingTranscriber(speech))
    assert await asr.transcribe_audio(ctx, item_id) == 0
    assert await fetch_all(pool, "SELECT id FROM transcript_segment") == []
    assert await fetch_all(pool, "SELECT id FROM moment") == []
    assert "embed" not in await jobs_by_lock(pool)


async def test_missing_file_is_skipped_and_corrupt_file_records_the_error(pool, pki_settings, dev):
    ctx = make_ctx(pool, pki_settings, transcriber=FakeTranscriber())
    gone = await insert_item(pool, dev["device_id"], kind="audio")
    assert await asr.transcribe_audio(ctx, gone) == 0
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (gone,)))["analysis_state"] == "skipped"
    corrupt = await insert_item(pool, dev["device_id"], kind="audio", started_at=T0 + dt.timedelta(minutes=1))
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (corrupt,))
    (pki_settings.media_root / row["raw_path"]).parent.mkdir(parents=True, exist_ok=True)
    (pki_settings.media_root / row["raw_path"]).write_bytes(b"OggS-fake")
    with pytest.raises(ValueError):  # PyAV's InvalidDataError
        await asr.transcribe_audio(ctx, corrupt)
    item = await fetch_one(pool, "SELECT analysis_state, analysis_error FROM item WHERE id = %s", (corrupt,))
    assert item["analysis_state"] == "pending" and item["analysis_error"]
    video = await insert_item(pool, dev["device_id"], kind="video")
    assert await asr.transcribe_audio(ctx, video) == 0


def test_whisper_transcriber_loads_once_and_restricts_the_language(monkeypatch, tmp_path):
    import faster_whisper

    created = []

    class StubSegment:
        start, end, text, avg_logprob, no_speech_prob, compression_ratio = 0.0, 1.5, " xin chào ", -0.2, 0.01, 1.1

    class StubModel:
        def __init__(self, size, **kwargs):
            created.append((size, kwargs))
            self.calls = []

        def detect_language(self, audio):
            return "zh", 0.7, [("zh", 0.7), ("vi", 0.2), ("en", 0.1)]

        def transcribe(self, audio, **kwargs):
            self.calls.append(kwargs)
            return iter([StubSegment()]), object()

    monkeypatch.setattr(faster_whisper, "WhisperModel", StubModel)
    settings = Settings(database_url="postgresql://x", model_dir=tmp_path, whisper_cpu_threads=3)
    whisper = asr.WhisperTranscriber(settings)
    chunk = np.zeros(SR, dtype=np.float32)
    first = whisper.transcribe(chunk)
    whisper.transcribe(chunk)
    assert created == [("large-v3-turbo", {"device": "cpu", "compute_type": "int8", "cpu_threads": 3,
                                           "download_root": str(tmp_path / "whisper")})]
    assert first == RegionResult("vi", 0.2, [SegmentResult(0.0, 1.5, "xin chào", -0.2, 0.01, 1.1)])
    assert whisper._model.calls[0] == {"language": "vi", "beam_size": 5, "condition_on_previous_text": False,
                                       "vad_filter": False}


@pytest.mark.parametrize("languages", ["vi,vn", " , "])
def test_unknown_or_missing_languages_stop_the_worker_at_start(languages):
    with pytest.raises(ValueError, match="LOOPI_ASR_LANGUAGES"):
        asr.WhisperTranscriber(Settings(database_url="postgresql://x", asr_languages=languages))


@pytest.mark.skipif(os.environ.get("LOOPI_TEST_WHISPER") != "1", reason="set LOOPI_TEST_WHISPER=1 (downloads 1.6 GB once)")
def test_real_whisper_model_transcribes_a_chunk(tmp_path):
    model_dir = os.environ.get("LOOPI_MODEL_DIR", str(tmp_path / "models"))
    whisper = asr.WhisperTranscriber(Settings(database_url="postgresql://x", model_dir=model_dir))
    tone = asr.load_audio(make_opus(tmp_path / "tone.opus", "sine=frequency=440:sample_rate=16000", 3))
    result = whisper.transcribe(tone)
    assert result.lang in {"vi", "ko", "en"} and 0.0 <= result.lang_prob <= 1.0
    assert all(isinstance(s, SegmentResult) for s in result.segments)

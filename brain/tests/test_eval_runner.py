import datetime as dt
import logging
import threading
import time

import psycopg
import pytest

from helpers import (
    FakeOllama,
    FakeTranscriber,
    analysed_audio,
    captioned_keyframe,
    fetch_all,
    fetch_one,
    insert_item,
    insert_moment,
    make_ctx,
    make_opus,
    paired_device,
    run_jobs,
)
from loopi_brain import acceptance, cli
from loopi_brain.evaluation import golden, questions, runner
from loopi_brain.pipeline import asr
from loopi_brain.pipeline.asr import Region, RegionResult, SegmentResult

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
SECRET = "Mai mình đi chợ nhé"


def seg(text, start=0.0, end=2.0) -> SegmentResult:
    return SegmentResult(start, end, text, -0.3, 0.05, 1.2)


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def one_region(monkeypatch):
    """Every clip has one speech region (the real Silero VAD finds none in a synthetic file); records thresholds."""
    thresholds = []

    def regions(_audio, threshold):
        thresholds.append(threshold)
        return [Region(0.0, 2.0)]

    monkeypatch.setattr(asr, "speech_regions", regions)
    return thresholds


async def _clip(pool, settings, device_id, minutes, label) -> str:
    item = await analysed_audio(pool, settings, device_id, NINE + dt.timedelta(minutes=minutes))
    return await golden.add_audio(pool, settings, item, label)


async def _golden_clips(pool, settings, device_id) -> None:
    """A Vietnamese clip, a Korean one and one without speech, created in that order (the order they are heard)."""
    await _clip(pool, settings, device_id, 0, golden.AudioLabel("vi", False, SECRET))
    await _clip(pool, settings, device_id, 1, golden.AudioLabel("ko", False, "네 알겠어요"))
    await _clip(pool, settings, device_id, 2, golden.AudioLabel(None, True, ""))


HEARD = (
    RegionResult("vi", 0.9, [seg("Mai mình đi chợ")]),  # one word missed: WER 1/5
    RegionResult("ko", 0.9, [seg("네 알겠어요")]),  # exact: CER 0
    RegionResult("en", 0.3, [seg("Thank you.")]),  # invented on silence
)


async def _run(pool, settings, transcriber, ollama=None, **overrides) -> dict:
    ctx = make_ctx(pool, settings, ollama=ollama or FakeOllama(), transcriber=transcriber)
    run_id, created = await runner.start_run(pool, settings, **overrides)
    assert created
    await run_jobs(ctx)
    return await runner.latest_run(pool, run_id)


async def test_one_transcription_scores_every_rule_set(pool, pki_settings, dev, jobs_open, one_region, caplog):
    await _golden_clips(pool, pki_settings, dev["device_id"])
    fake = FakeTranscriber(*HEARD)
    with caplog.at_level(logging.INFO):
        run = await _run(pool, pki_settings, fake)
    assert run["status"] == "done" and len(fake.chunks) == 3  # each clip heard once, whatever the rule sets
    audio = run["results"]["asr"]
    assert (audio["clips"], audio["failed"]) == (3, 0)
    assert set(audio["variants"]) == {"live", "none", "outro", "lone_phrase", "short_foreign", "all"}
    live = audio["variants"]["live"]
    assert live["rules"] == [] and live["languages"]["vi"]["score"] == pytest.approx(0.2)
    assert live["languages"]["vi"]["metric"] == "wer" and live["languages"]["vi"]["words"] == 5
    assert live["languages"]["ko"]["metric"] == "cer" and live["languages"]["ko"]["score"] == 0.0
    assert live["languages"]["vi"]["over_limit"] is False
    assert live["languages"]["vi"]["score_ci"] is None  # one clip: no interval
    assert live["no_speech"] == {"clips": 1, "with_text": 0, "invented": 1, "words": 2, "rate": 1.0,
                                 "rate_ci": [pytest.approx(0.2065, abs=1e-4), 1.0]}
    for name in ("lone_phrase", "short_foreign", "all"):
        assert audio["variants"][name]["no_speech"]["invented"] == 0, name
        assert audio["variants"][name]["languages"]["vi"]["score"] == pytest.approx(0.2), name  # real speech kept
        # Nothing was dropped from the clips with speech: these rules were not tested on real speech here.
        assert audio["variants"][name]["dropped"] == {"segments": 0, "words": 0}, name
    assert audio["variants"]["outro"]["no_speech"]["invented"] == 1
    assert audio["recorded"] == {"first": "2026-10-14", "last": "2026-10-14"}
    assert (audio["prefilled"], audio["blind"]) == (2, 0)
    assert run["config"]["models"] == {"asr": "local:faster-whisper/large-v3-turbo-int8",
                                       "caption": "ollama:minicpm-v4.6:1b", "embed": "ollama:qwen3-embedding:0.6b"}
    jobs = await fetch_all(pool, "SELECT task_name, status, lock, priority, queue_name, args FROM procrastinate_jobs"
                                 " ORDER BY id")
    # One job per clip under Whisper's lock, then the numbers under their own: live ASR never waits for the numbers.
    assert [(j["lock"], j["args"]["final"]) for j in jobs] == [("asr", False)] * 3 + [("eval", True)]
    assert {(j["task_name"], j["status"], j["priority"], j["queue_name"]) for j in jobs} == {
        ("run_eval", "succeeded", runner.EVAL_PRIORITY, "light")}
    heard = await fetch_all(pool, "SELECT segments FROM eval_run_audio ORDER BY sample_id")
    assert heard[2]["segments"][0]["lang"] == "en" and heard[2]["segments"][0]["lang_prob"] == pytest.approx(0.3)
    assert SECRET not in caplog.text and "Thank you" not in caplog.text
    report = runner.format_run(run, pki_settings.zone)
    assert ("| live (none) | 20.0 % (1 clips) | 0.0 % (1 clips) | — | 1/1 (2 words) | 0 segments (0 words) |"
            in report)
    table = report.split("| Rules |")[1].split("\n\n")[0]
    rows = [line.split(" |")[0] for line in table.splitlines()[2:]]
    assert rows == ["| live (none)", "| none", "| outro", "| lone_phrase", "| short_foreign", "| all"]
    assert "| all | 20.0 % (1 clips) | 0.0 % (1 clips) | — | 0/1 (0 words) | 0 segments (0 words) |" in report
    assert "| vi | 1 | 5 | 20.0 % (—) | 15 | " in report and "| en | 0 | — | — | — | — | not measurable (no clip) |" in report
    assert "recorded 2026-10-14 … 2026-10-14; references edited from Whisper's text: 2, typed blind: 0" in report
    assert SECRET not in report and "Thank you" not in report  # numbers only


async def test_a_waiting_evaluation_never_goes_ahead_of_a_live_transcription(pool, pki_settings, dev, jobs_open,
                                                                              one_region):
    from loopi_brain.pipeline import dispatch

    await _golden_clips(pool, pki_settings, dev["device_id"])
    await runner.start_run(pool, pki_settings)  # its first step waits: lock "asr", priority -20
    recorded = await insert_item(pool, dev["device_id"], kind="audio", started_at=NINE + dt.timedelta(hours=1),
                                 seconds=5)
    raw = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (recorded,)))["raw_path"]
    make_opus(pki_settings.media_root / raw, seconds=5)
    assert await dispatch.defer_item(pool, recorded) is not None  # a live recording arrives after the request
    await run_jobs(make_ctx(pool, pki_settings, transcriber=FakeTranscriber(*HEARD)))
    order = await fetch_all(pool, "SELECT j.task_name FROM procrastinate_events e JOIN procrastinate_jobs j"
                                  " ON j.id = e.job_id WHERE e.type = 'started' ORDER BY e.id")
    # The live audio is detected, grouped (inside the detection) and transcribed, and its moment embedded, before
    # any of the four evaluation jobs.
    assert [r["task_name"] for r in order] == ["detect_speech", "transcribe_audio", "embed_moments"] + ["run_eval"] * 4


async def test_what_a_rule_drops_from_real_speech_is_counted(pool, pki_settings, dev, jobs_open, monkeypatch):
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _threshold: [Region(0.0, 2.0), Region(3.0, 5.0)])
    await _clip(pool, pki_settings, dev["device_id"], 0, golden.AudioLabel("en", False, "Yeah. Can you call me back"))
    run = await _run(pool, pki_settings, FakeTranscriber(RegionResult("en", 0.9, [seg("Yeah.")]),
                                                         RegionResult("en", 0.9, [seg("Can you call me back")])))
    variants = run["results"]["asr"]["variants"]
    assert variants["none"]["dropped"] == {"segments": 0, "words": 0}
    assert variants["lone_phrase"]["dropped"] == {"segments": 1, "words": 1}  # a real "Yeah." is lost
    assert variants["lone_phrase"]["languages"]["en"]["score"] == pytest.approx(1 / 6)
    assert variants["none"]["languages"]["en"]["score"] == 0.0


async def test_a_language_above_30_percent_is_flagged(pool, pki_settings, dev, jobs_open, one_region):
    await _clip(pool, pki_settings, dev["device_id"], 0, golden.AudioLabel("vi", False, SECRET))
    run = await _run(pool, pki_settings, FakeTranscriber(RegionResult("vi", 0.9, [seg("Mai đi")])))
    vi = run["results"]["asr"]["variants"]["live"]["languages"]["vi"]
    assert vi["score"] == pytest.approx(0.6) and vi["over_limit"] is True
    assert "60.0 % (1 clips) > 30 %" in runner.format_run(run, pki_settings.zone)


async def test_the_live_rules_and_the_overrides_are_what_the_run_hears_with(pool, pki_settings, dev, jobs_open,
                                                                             one_region):
    await _golden_clips(pool, pki_settings, dev["device_id"])
    worker = pki_settings.model_copy(update={"asr_rules": "lone_phrase", "asr_vad_threshold": 0.45})
    ctx = make_ctx(pool, worker, transcriber=FakeTranscriber(*HEARD))  # the api's settings have no rule on
    run_id, _ = await runner.start_run(pool, pki_settings, max_gain_db=12.0, vad_threshold=0.3)
    assert "not started yet" in runner.format_run(await runner.latest_run(pool, run_id), pki_settings.zone)
    await run_jobs(ctx)
    run = await runner.latest_run(pool, run_id)
    assert one_region == [0.3, 0.3, 0.3]
    assert run["config"]["max_gain_db"] == 12.0 and run["config"]["overrides"] == {"max_gain_db": 12.0,
                                                                                   "vad_threshold": 0.3}
    live = run["results"]["asr"]["variants"]["live"]
    assert live["rules"] == ["lone_phrase"] and live["no_speech"]["invented"] == 0  # the worker's live rules
    run_id, _ = await runner.start_run(pool, pki_settings)
    await run_jobs(ctx)
    plain = await runner.latest_run(pool, run_id)
    assert (plain["config"]["max_gain_db"], plain["config"]["vad_threshold"]) == (30.0, 0.45)
    assert one_region[3:] == [0.45, 0.45, 0.45]
    for bad in ({"max_gain_db": 31.0}, {"vad_threshold": 1.0}):  # D19: never more than 30 dB
        with pytest.raises(ValueError):
            await runner.start_run(pool, pki_settings, **bad)


async def test_a_clip_whisper_cannot_read_is_counted_and_the_run_goes_on(pool, pki_settings, dev, jobs_open,
                                                                         one_region):
    await _golden_clips(pool, pki_settings, dev["device_id"])
    [first] = await fetch_all(pool, "SELECT id, path FROM eval_audio ORDER BY id LIMIT 1")
    (pki_settings.eval_dir / first["path"]).write_bytes(b"OggS-not-audio")
    run = await _run(pool, pki_settings, FakeTranscriber(*HEARD[1:]))
    assert run["status"] == "done" and run["results"]["asr"]["failed"] == 1
    row = await fetch_one(pool, "SELECT segments, error FROM eval_run_audio WHERE sample_id = %s", (first["id"],))
    assert row["segments"] is None and row["error"]
    assert "vi" not in run["results"]["asr"]["variants"]["live"]["languages"]


async def test_a_language_whose_clips_all_failed_is_not_reported_as_having_no_clip(pool, pki_settings, dev, jobs_open,
                                                                                    one_region):
    d = dev["device_id"]
    await _clip(pool, pki_settings, d, 0, golden.AudioLabel("vi", False, SECRET))
    await _clip(pool, pki_settings, d, 1, golden.AudioLabel("ko", False, "네 알겠어요"))
    await _clip(pool, pki_settings, d, 2, golden.AudioLabel("ko", False, "알겠어요 네"))
    await _clip(pool, pki_settings, d, 3, golden.AudioLabel(None, True, ""))
    broken = await fetch_all(pool, "SELECT path FROM eval_audio WHERE lang = 'ko' OR no_speech")
    for row in broken:  # both Korean clips and the one without speech cannot be read
        (pki_settings.eval_dir / row["path"]).write_bytes(b"OggS-not-audio")
    run = await _run(pool, pki_settings, FakeTranscriber(HEARD[0]))
    audio = run["results"]["asr"]
    assert (audio["clips"], audio["failed"], audio["failed_by_lang"]) == (4, 3, {"ko": 2, "silent": 1})
    assert "ko" not in audio["variants"]["live"]["languages"]  # nothing of it was heard
    report = runner.format_run(run, pki_settings.zone)
    assert "Clips: 4 (3 could not be transcribed)" in report
    assert "| ko | 0 (2 failed) | — | — | — | — | not measurable (2 clips could not be transcribed) |" in report
    assert "| en | 0 | — | — | — | — | not measurable (no clip) |" in report  # no clip was ever labelled
    assert "| live (none) | 20.0 % (1 clips) | — (2 failed) | — | 0/0 (0 words) |" in report
    assert "1 no-speech clip could not be transcribed" in report
    text = "\n".join(acceptance._evaluation_lines(run, pki_settings.zone))
    assert "Golden clips: 4 (3 could not be transcribed), recorded 2026-10-14 … 2026-10-14;" in text
    assert "| ko | 0 (2 failed) | — | — | — | — | not measurable (2 clips could not be transcribed) |" in text
    assert "| en | 0 | — | — | — | — | not measurable (no clip) |" in text
    assert ("Baseline measured for: vi; not measurable (no clip): en; not measurable (clips could not be "
            "transcribed): ko (2)") in text
    assert "1 no-speech clip could not be transcribed" in text
    for lines in (report, text):
        assert "ko | 0 | — | — | — | — | not measurable (no clip)" not in lines
        assert SECRET not in lines and "알겠어요" not in lines  # numbers only


async def test_one_failed_clip_among_the_heard_ones_is_counted_in_its_language_row(pool, pki_settings, dev, jobs_open,
                                                                                   one_region):
    d = dev["device_id"]
    await _clip(pool, pki_settings, d, 0, golden.AudioLabel("vi", False, SECRET))
    await _clip(pool, pki_settings, d, 1, golden.AudioLabel("vi", False, "Hai ba bốn"))
    [first] = await fetch_all(pool, "SELECT path FROM eval_audio ORDER BY id LIMIT 1")
    (pki_settings.eval_dir / first["path"]).write_bytes(b"OggS-not-audio")
    run = await _run(pool, pki_settings, FakeTranscriber(RegionResult("vi", 0.9, [seg("Hai ba bốn")])))
    audio = run["results"]["asr"]
    assert (audio["clips"], audio["failed"], audio["failed_by_lang"]) == (2, 1, {"vi": 1})
    assert "| vi | 1 (1 failed) | 3 | 0.0 % (—) |" in runner.format_run(run, pki_settings.zone)
    assert "| vi | 1 (1 failed) | 3 | 0.0 % (—) |" in "\n".join(acceptance._evaluation_lines(run, pki_settings.zone))


async def test_captions_are_summed_per_caption_model(pool, pki_settings, dev, jobs_open):
    d = dev["device_id"]
    for minutes, score, flags in ((0, 5, set()), (60, 2, {"wrong_people", "invented_objects"}), (120, 4, set())):
        kf = await captioned_keyframe(pool, pki_settings, d, NINE + dt.timedelta(minutes=minutes))
        await golden.add_caption(pool, pki_settings, kf, score, flags)
    run = await _run(pool, pki_settings, FakeTranscriber())
    assert run["results"]["captions"] == {"ollama:minicpm-v4.6:1b": {
        "scored": 3, "mean": pytest.approx(11 / 3), "good": 2, "wrong_people": 1, "invented_objects": 1,
        "wrong_activity": 0}}
    assert run["results"]["asr"]["clips"] == 0
    assert "- ollama:minicpm-v4.6:1b: 3 scored, mean 3.67, ≥ 4: 2;" in runner.format_run(run, pki_settings.zone)


async def test_recall_is_measured_through_the_hybrid_search(pool, pki_settings, dev, jobs_open):
    d = dev["device_id"]
    # FakeOllama embeds every question as the axis-0 vector: moments on axis 0 mean the same, axis 3 is unrelated.
    market = await insert_moment(pool, d, kind="speech", ts=NINE, axis=0, text="Mai mình đi chợ")
    room = await insert_moment(pool, d, kind="visual", ts=NINE + dt.timedelta(hours=1), axis=3, text="A dark room")
    found = await questions.create_question(pool, "Khi nào đi chợ?", None, None)
    await questions.add_answer(pool, found, market)
    missed = await questions.create_question(pool, "Phòng tối lúc nào?", None, None)
    await questions.add_answer(pool, missed, room)
    elsewhere = await questions.create_question(pool, "Đi chợ hôm sau?", dt.date(2026, 10, 15), dt.date(2026, 10, 15))
    await questions.add_answer(pool, elsewhere, market)  # the question's days exclude its answer: not found
    await questions.create_question(pool, "Chưa có câu trả lời", None, None)  # not scored
    run = await _run(pool, pki_settings, FakeTranscriber())
    recall = run["results"]["recall"]
    assert recall["questions"] == 3 and recall["semantic"] is True and recall["found5"] == 1
    assert (recall["hit5"], recall["recall5"], recall["hit10"], recall["mrr"]) == pytest.approx((1 / 3,) * 4)
    assert recall["hit5_ci"] == pytest.approx([0.0615, 0.7923], abs=1e-4)  # 1 of 3: a wide interval
    assert recall["meets_target"] is False
    assert [q["ranks"] for q in recall["per_question"]] == [[1], [], []]
    assert len(recall["grid"]) == len(runner.DISTANCES) * len(runner.SIMILARITIES)
    assert {g["found5"] for g in recall["grid"]} == {1}
    report = runner.format_run(run, pki_settings.zone)
    assert ("hit@5 33.3 % = 1/3 (95 % 6.1–79.2 %; target 70 %, D22: not met), capped recall@5 33.3 %, hit@10 33.3 %,"
            " MRR 0.33") in report
    assert "each searched with the owner's days for it, not the chat's time filter" in report
    assert "Grid: best 1/3 found in the first 5 at max distance 0.6, min similarity 0.2" in report
    assert "đi chợ" not in report.lower()


async def test_each_question_is_embedded_once_and_a_failing_embedding_ends_recall(pool, pki_settings, dev, jobs_open):
    market = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, axis=0, text="Mai mình đi chợ")
    for text in ("Khi nào đi chợ?", "Hôm đó đi chợ không?"):
        await questions.add_answer(pool, await questions.create_question(pool, text, None, None), market)
    ollama = FakeOllama()
    run = await _run(pool, pki_settings, FakeTranscriber(), ollama=ollama)
    assert run["results"]["recall"]["found5"] == 2
    assert ollama.paths().count("/api/embed") == 2  # once per question, for the live thresholds and all 12 cells
    ollama.error = 500  # the model fails on the first question: no recall, and no second wait
    before = len(ollama.requests)
    run = await _run(pool, pki_settings, FakeTranscriber(), ollama=ollama)
    assert run["results"]["recall"] == {"questions": 2,
                                        "error": "a question could not be embedded (the embedding model failed)"}
    assert [p for p, _ in ollama.requests[before:]].count("/api/embed") == 1


async def test_recall_waits_for_the_embedding_model(pool, pki_settings, dev, jobs_open):
    market = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, axis=0, text="Mai mình đi chợ")
    await questions.add_answer(pool, await questions.create_question(pool, "Đi chợ?", None, None), market)
    ollama = FakeOllama()
    ollama.down = True
    run = await _run(pool, pki_settings, FakeTranscriber(), ollama=ollama)
    assert run["status"] == "done"
    assert run["results"]["recall"] == {"questions": 1, "error": "the embedding model is not available"}
    assert "Recall: not measured (the embedding model is not available)" in runner.format_run(run, pki_settings.zone)


async def test_one_run_at_a_time_and_a_run_whose_job_is_gone_does_not_block_the_next(pool, pki_settings, jobs_open):
    first, created = await runner.start_run(pool, pki_settings)
    assert created and await runner.start_run(pool, pki_settings) == (first, False)
    async with pool.connection() as conn:  # the worker died and the job was dead-lettered and cleared
        await conn.execute("DELETE FROM procrastinate_jobs")
    second, created = await runner.start_run(pool, pki_settings)
    assert created and second != first
    stopped = await runner.latest_run(pool, first)
    assert stopped["status"] == "failed" and "start a new one" in stopped["error"]
    assert "Progress: 0/0 clips; error: the run stopped" in runner.format_run(stopped, pki_settings.zone)
    assert [r["id"] for r in await runner.list_runs(pool)] == [second, first]
    [job] = await fetch_all(pool, "SELECT lock, args FROM procrastinate_jobs")
    assert (job["lock"], job["args"]["final"]) == ("eval", True)  # no golden clip: straight to the numbers


async def test_two_runs_side_by_side_give_the_noise_floor(pool, pki_settings, dev, jobs_open, one_region):
    await _golden_clips(pool, pki_settings, dev["device_id"])
    market = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, axis=0, text="Mai mình đi chợ")
    await questions.add_answer(pool, await questions.create_question(pool, "Khi nào đi chợ?", None, None), market)
    first = await _run(pool, pki_settings, FakeTranscriber(*HEARD))
    second = await _run(pool, pki_settings, FakeTranscriber(RegionResult("vi", 0.9, [seg("Mai mình đi chợ nhé")]),
                                                            *HEARD[1:]))
    text = runner.format_compare(first, second)
    assert "| vi WER | 20.0 % (1 clips) | 0.0 % (1 clips) | -20.0 points |" in text
    assert "| ko CER | 0.0 % (1 clips) | 0.0 % (1 clips) | +0.0 points |" in text
    assert "| no speech: clips with invented text | 1/1 | 1/1 | +0 |" in text and SECRET not in text
    assert "| hit@5: questions found | 1/1 | 1/1 | +0 |" in text
    assert runner.format_compare(first, {**second, "status": "running"}) == "Both runs must be finished."


def test_a_comparison_shows_the_size_of_every_sample_so_two_golden_sets_cannot_pass_as_one():
    def run(run_id, vi_clips, ko_clips, questions, found5):
        def row(metric, clips, score):
            return {"metric": metric, "clips": clips, "score": score}

        languages = {"vi": row("wer", vi_clips, 0.2), **({"ko": row("cer", ko_clips, 0.1)} if ko_clips else {})}
        return {"id": run_id, "status": "done", "results": {
            "asr": {"variants": {"live": {"languages": languages, "no_speech": {"clips": 6, "invented": 2}}}},
            "recall": {"questions": questions, "found5": found5}}}

    # The owner relabelled between the runs: 6 → 4 Vietnamese clips, Korean gone, 25 → 20 questions.
    text = runner.format_compare(run("A", 6, 6, 25, 18), run("B", 4, 0, 20, 15))
    assert "| vi WER | 20.0 % (6 clips) | 20.0 % (4 clips) | +0.0 points |" in text
    assert "| ko CER | 10.0 % (6 clips) | — | — |" in text
    assert "| no speech: clips with invented text | 2/6 | 2/6 | +0 |" in text
    assert "| hit@5: questions found | 18/25 | 15/20 | -3 |" in text


async def test_what_the_rules_would_drop_from_the_live_transcripts_is_counted(pool, pki_settings, dev):
    from helpers import insert_segment

    d = dev["device_id"]
    noise = await insert_item(pool, d, kind="audio", started_at=NINE)
    await insert_segment(pool, noise, d, NINE, "Thank you.", lang="en", lang_prob=0.3)
    await insert_segment(pool, noise, d, NINE + dt.timedelta(seconds=3), "Yeah.", lang="en", lang_prob=0.3)  # same region
    outro = await insert_item(pool, d, kind="audio", started_at=NINE + dt.timedelta(minutes=1))
    await insert_segment(pool, outro, d, NINE + dt.timedelta(minutes=1), "Hãy đăng ký kênh để ủng hộ mình nhé")
    speech = await insert_item(pool, d, kind="audio", started_at=NINE + dt.timedelta(minutes=2))
    await insert_segment(pool, speech, d, NINE + dt.timedelta(minutes=2), SECRET)
    old = await insert_item(pool, d, kind="audio", started_at=NINE + dt.timedelta(minutes=3))
    await insert_segment(pool, old, d, NINE + dt.timedelta(minutes=3), "Okay go", lang="en", lang_prob=None)
    found = await runner.rules_impact(pool, dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
    assert (found["segments"], found["recordings"], found["no_probability"]) == (5, 4, 1)
    assert found["rules"]["lone_phrase"] == {"segments": 2, "words": 3, "recordings": 1, "by_lang": {"en": 2}}
    assert found["rules"]["short_foreign"] == {"segments": 2, "words": 3, "recordings": 1, "by_lang": {"en": 2}}
    assert found["rules"]["outro"] == {"segments": 1, "words": 9, "recordings": 1, "by_lang": {"vi": 1}}
    assert found["rules"]["all"]["segments"] == 3
    text = runner.format_rules_impact(found)
    assert "| outro | 1 | 9 | 1 | vi 1 |" in text and SECRET not in text and "Thank you" not in text
    assert (await runner.rules_impact(pool, dt.datetime.now(dt.UTC)))["segments"] == 0


async def test_a_clip_deleted_before_its_job_leads_straight_to_the_numbers(pool, pki_settings, dev, jobs_open):
    await _clip(pool, pki_settings, dev["device_id"], 0, golden.AudioLabel("vi", False, SECRET))
    run_id, _ = await runner.start_run(pool, pki_settings)
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM eval_audio")  # the owner deleted it while the job waited
    await run_jobs(make_ctx(pool, pki_settings, transcriber=FakeTranscriber()))
    run = await runner.latest_run(pool, run_id)
    assert run["status"] == "done" and run["results"]["asr"]["clips"] == 0
    jobs = await fetch_all(pool, "SELECT lock FROM procrastinate_jobs ORDER BY id")
    assert [j["lock"] for j in jobs] == ["asr", "eval"]


async def test_a_question_not_embedded_beforehand_is_never_waited_for():
    class Router:
        calls = 0

        async def embed(self, task, texts):
            Router.calls += 1
            return texts

    cached = runner._CachedEmbeddings(Router())
    await cached.prefetch("embed", ["Khi nào đi chợ?", "Khi nào đi chợ?", " "])
    assert Router.calls == 1  # each question once; an empty one is never asked
    with pytest.raises(runner.ProviderUnavailable):
        await cached.embed("embed", ["another question"])
    assert Router.calls == 1


async def test_an_unexpected_error_fails_the_run_without_a_retry(pool, pki_settings, jobs_open, monkeypatch):
    async def broken(pool):
        raise RuntimeError("database went away")

    monkeypatch.setattr(runner.questions, "answered", broken)
    run = await _run(pool, pki_settings, FakeTranscriber())
    assert run["status"] == "failed" and run["error"] == "RuntimeError: database went away"
    jobs = await fetch_all(pool, "SELECT status FROM procrastinate_jobs")
    assert [j["status"] for j in jobs] == ["succeeded"]
    ctx = make_ctx(pool, pki_settings, transcriber=FakeTranscriber())
    assert await runner.run_step(ctx, run["id"]) == "nothing"  # a finished run is never resumed


async def test_a_neighbouring_moment_is_correct_only_once_the_picked_one_is_gone(pool, pki_settings, dev, jobs_open):
    """Task 5 ruling: the runner scores answers as questions.answered() hands them over, with their ``live`` flag."""
    d = dev["device_id"]
    item = await insert_item(pool, d, kind="audio", started_at=NINE)
    # The owner picked the first region (on another subject, so the search never finds it); the search finds the second,
    # a neighbour of the same recording that starts inside the slack.
    picked = await insert_moment(pool, d, kind="speech", ts=NINE, axis=3, text="A dark room", item_id=item, seconds=2)
    await insert_moment(pool, d, kind="speech", ts=NINE + dt.timedelta(seconds=3), axis=0, text="Mai mình đi chợ",
                        item_id=item, seconds=2)
    await questions.add_answer(pool, await questions.create_question(pool, "Khi nào đi chợ?", None, None), picked)
    run = await _run(pool, pki_settings, FakeTranscriber())
    assert run["results"]["recall"]["found5"] == 0  # the picked moment exists: its neighbour is another moment
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM moment WHERE id = %s", (picked,))  # the day was reprocessed
    run = await _run(pool, pki_settings, FakeTranscriber())
    assert run["results"]["recall"]["found5"] == 1  # its successor is the correct moment now


async def test_the_scoring_runs_off_the_event_loop(pool, pki_settings, dev, jobs_open, one_region, monkeypatch):
    """R-I1: the six rule sets are scored in plain Python (about 28 s for 18 long clips); on the worker's event loop
    that would block its other job slots, so the numbers job hands the scoring to a thread."""
    await _golden_clips(pool, pki_settings, dev["device_id"])
    threads = []
    real = runner.asr_numbers

    def spy(clips, rules):
        threads.append(threading.get_ident())
        return real(clips, rules)

    monkeypatch.setattr(runner, "asr_numbers", spy)
    run = await _run(pool, pki_settings, FakeTranscriber(*HEARD))
    assert run["status"] == "done" and len(threads) == len(runner.VARIANTS)
    assert threading.get_ident() not in threads  # none of the six ran on the event loop's thread
    assert run["results"]["asr"]["variants"]["live"]["languages"]["vi"]["score"] == pytest.approx(0.2)


def test_the_cli_starts_a_run_waits_and_shows_it(database_url, monkeypatch, capsys):
    monkeypatch.setenv("LOOPI_DATABASE_URL", database_url)
    monkeypatch.setattr(cli, "EVAL_POLL_S", 0.05)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("TRUNCATE eval_run CASCADE")
        conn.execute("DELETE FROM procrastinate_jobs")
    assert cli.main(["eval", "show"]) == 1 and "no evaluation run yet" in capsys.readouterr().out
    assert cli.main(["eval", "run", "--no-wait"]) == 0
    run_id = capsys.readouterr().out.split(": ")[1].strip()
    assert cli.main(["eval", "run", "--timeout", "0"]) == 1  # no worker here: it is still waiting
    out = capsys.readouterr().out
    assert f"already waiting or running: {run_id}" in out and f"see `loopi-brain eval show {run_id}`" in out

    def finish_soon():  # the worker, elsewhere, gives up on the run
        time.sleep(0.3)
        with psycopg.connect(database_url, autocommit=True) as conn:
            conn.execute("UPDATE eval_run SET status = 'failed', error = 'RuntimeError: x' WHERE id = %s", (run_id,))

    worker = threading.Thread(target=finish_soon)
    worker.start()
    try:
        assert cli.main(["eval", "run"]) == 1
    finally:
        worker.join()
    assert "— failed" in capsys.readouterr().out
    assert cli.main(["eval", "show", run_id]) == 0 and "error: RuntimeError: x" in capsys.readouterr().out
    assert cli.main(["eval", "show", "01NOSUCHRUN000000000000000"]) == 1
    assert cli.main(["eval", "run", "--vad-threshold", "2"]) == 2
    assert cli.main(["eval", "compare", run_id, "01NOSUCHRUN000000000000000"]) == 1
    assert cli.main(["eval", "compare", run_id, run_id]) == 0 and "Both runs must be finished." in capsys.readouterr().out
    assert cli.main(["eval", "rules-impact", "--days", "1"]) == 0
    assert "| lone_phrase |" in capsys.readouterr().out
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("TRUNCATE eval_run CASCADE")
        conn.execute("DELETE FROM procrastinate_jobs")

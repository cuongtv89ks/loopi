import datetime as dt
import io
import re
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.types.json import Jsonb

from helpers import insert_item, insert_keyframe, insert_moment, insert_segment, paired_device
from loopi_brain import acceptance, cli
from loopi_brain.config import Settings
from loopi_brain.evaluation import runner
from loopi_brain.pipeline import dispatch

SEOUL = ZoneInfo("Asia/Seoul")
NOW = dt.datetime(2026, 10, 17, 10, 0, tzinfo=SEOUL)  # the window is 15 and 16 October (complete days)
CPU = """\
worker 2026-10-15T19:28:10Z usage_usec 47672930298 started=2026-10-15T14:51:34.148257075Z
models 2026-10-15T19:28:10Z usage_usec 11584004465 started=2026-10-15T13:42:15.15953307Z
worker memory.peak 4498079744
models memory.peak 7954718720
worker 2026-10-16T19:28:10Z usage_usec 90872930298 started=2026-10-15T14:51:34.148257075Z
models 2026-10-16T19:28:10Z usage_usec 21584004465 started=2026-10-16T01:00:00Z
worker memory.peak 4600000000
"""


def local(day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=SEOUL)


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


async def _captioned(pool, settings, device_id, ended: dt.datetime, delay_s: float) -> None:
    item = await insert_item(pool, device_id, kind="video", started_at=ended - dt.timedelta(seconds=60))
    kf = await insert_keyframe(pool, settings, item, device_id, ended - dt.timedelta(seconds=30))
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption = '{}', captioned_at = %s WHERE id = %s",
                           (ended + dt.timedelta(seconds=delay_s), kf))


async def _transcribed(pool, device_id, ended: dt.datetime, delay_s: float) -> str:
    item = await insert_item(pool, device_id, kind="audio", started_at=ended - dt.timedelta(seconds=60))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done', analyzed_at = %s WHERE id = %s",
                           (ended + dt.timedelta(seconds=delay_s), item))
    return item


LIVE_CONFIG = {"overrides": {}, "max_gain_db": 30.0, "vad_threshold": 0.5, "rules": []}


async def _evaluated(pool, config: dict | None = None) -> None:
    def clip(lang, reference, heard):
        return {"no_speech": False, "lang": lang, "reference": reference, "segments": [
            {"start": 0, "end": 2, "lang": lang, "lang_prob": 0.9, "segments": [
                {"start": 0, "end": 2, "text": heard, "avg_logprob": -0.3, "no_speech_prob": 0.0,
                 "compression_ratio": 1.2}]}]}

    clips = [clip("vi", "một hai ba bốn", "một hai ba"), clip("ko", "네 알겠어요", "네 알겠어요")]
    results = {
        "asr": {"clips": 2, "failed": 0, "recorded": {"first": "2026-10-16", "last": "2026-10-20"}, "prefilled": 1,
                "blind": 1, "variants": {"live": {"rules": [], **runner.asr_numbers(clips, [])}}},
        "captions": {"ollama:minicpm-v4.6:1b": {"scored": 30, "mean": 3.5, "good": 18, "wrong_people": 2,
                                                "invented_objects": 4, "wrong_activity": 1}},
        "recall": {"questions": 25, "found5": 18, "hit5": 0.72, "hit5_ci": [0.5242, 0.8572], "recall5": 0.68,
                   "hit10": 0.8, "mrr": 0.61, "semantic": True, "meets_target": True, "per_question": [], "grid": []},
    }
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO eval_run (id, status, config, results, requested_at) VALUES"
                           " ('R1', 'done', %s, %s, %s)", (Jsonb(config or LIVE_CONFIG), Jsonb(results),
                                                           NOW - dt.timedelta(days=1)))
        await conn.execute("INSERT INTO eval_run (id, status, config, requested_at) VALUES ('R2', 'running', '{}', %s)",
                           (NOW,))  # newer, but not finished: the report uses R1


async def test_the_latency_counts_recordings_that_ended_between_8_and_22(pool, pki_settings, dev):
    d = dev["device_id"]
    await _captioned(pool, pki_settings, d, local(15, 9), 120)
    await _captioned(pool, pki_settings, d, local(15, 12), 240)
    await _captioned(pool, pki_settings, d, local(15, 23), 3600)  # at night: not counted
    await _captioned(pool, pki_settings, d, local(15, 7, 30), 3600)
    await _captioned(pool, pki_settings, d, local(17, 9), 3600)  # today: not a complete day
    await _transcribed(pool, d, local(16, 10), 30)
    await _transcribed(pool, d, local(16, 21, 59), 90)
    found = await acceptance.latency(pool, SEOUL, 2, now=NOW)
    assert found["days"] == [dt.date(2026, 10, 15), dt.date(2026, 10, 16)]
    assert (found["video"]["n"], found["video"]["avg"], found["video"]["p95"]) == (2, 180.0, pytest.approx(234.0))
    assert (found["audio"]["n"], found["audio"]["avg"], found["audio"]["p95"]) == (2, 60.0, pytest.approx(87.0))
    assert set(found["video_by_day"]) == {dt.date(2026, 10, 15)} and set(found["audio_by_day"]) == {dt.date(2026, 10, 16)}
    await _transcribed(pool, d, local(16, 11), 7 * 3600)  # re-transcribed a week later: a reprocess, not latency
    found = await acceptance.latency(pool, SEOUL, 2, now=NOW)
    assert (found["audio"]["n"], found["audio"]["late"]) == (3, 1)
    found = await acceptance.latency(pool, SEOUL, 2, now=NOW, exclude=[dt.date(2026, 10, 16)])
    assert found["audio"]["n"] == 0 and found["video"]["n"] == 2 and found["excluded"] == [dt.date(2026, 10, 16)]
    assert "Left out (reprocessed): 2026-10-16" in acceptance._latency_lines(found)
    with pytest.raises(ValueError):
        acceptance.window(SEOUL, 0)


async def test_every_evening_with_recordings_should_have_its_recap(pool, pki_settings, dev):
    d = dev["device_id"]
    await insert_moment(pool, d, kind="speech", ts=local(15, 9), text="x")
    await insert_moment(pool, d, kind="visual", ts=local(16, 9), text="y")
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version, generated_at) VALUES"
                           " ('2026-10-15', 'x', 'test', %s)", (local(15, 23, 31),))
    rows = await acceptance.recaps(pool, SEOUL, 2, now=NOW)
    assert [(r["day"], r["moments"], r["recap_at"]) for r in rows] == [
        (dt.date(2026, 10, 15), 1, local(15, 23, 31)), (dt.date(2026, 10, 16), 1, None)]
    lines = acceptance._recap_lines(rows)
    assert "| 2026-10-15 | 1 | 2026-10-15 23:31 |" in lines and "| 2026-10-16 | 1 | **none** |" in lines
    assert lines[-1] == "Days with recordings: 2; with a recap: 1 (missing: 2026-10-16)"


def test_cpu_time_per_day_comes_from_two_samples_of_one_container_life():
    samples, peaks = acceptance.parse_cpu(CPU + "garbage line\n")
    assert len(samples) == 4 and peaks == {"worker": 4600000000, "models": 7954718720}
    per_day = acceptance.cpu_per_day(samples)
    assert per_day["worker"]["hours"] == 24.0 and per_day["worker"]["cpu_s"] == pytest.approx(43200.0)
    assert per_day["worker"]["per_day_s"] == pytest.approx(43200.0)
    assert per_day["models"]["restarted"] is True and per_day["models"]["cpu_s"] is None
    one = acceptance.cpu_per_day(acceptance.parse_cpu(CPU.splitlines()[0])[0])
    assert one["worker"]["cpu_s"] is None and one["worker"]["restarted"] is False
    lines = acceptance._cpu_lines(CPU)
    assert any(line.startswith("| worker | 2026-10-15 19:28 → 2026-10-16 19:28 UTC (24.0 h) | 12.0 CPU-h | "
                               "43200 s (12.0 CPU-h) | 4.60 GB |") for line in lines)
    assert any("| models |" in line and "restarted in between" in line for line in lines)
    assert acceptance._cpu_lines(None)[-1] == "No CPU samples given (`--cpu-samples`)."
    assert acceptance._cpu_lines("nothing here")[-1] == "No CPU sample lines found."
    assert "needs two samples" in "".join(acceptance._cpu_lines(CPU.splitlines()[0]))


async def test_the_report_holds_the_numbers_and_never_the_text(pool, pki_settings, dev):
    d = dev["device_id"]
    await _captioned(pool, pki_settings, d, local(16, 9), 60)
    audio = await _transcribed(pool, d, local(16, 10), 30)
    await insert_segment(pool, audio, d, local(16, 9, 59), "bí mật của nhà mình", seconds=120)
    await insert_moment(pool, d, kind="speech", ts=local(16, 9, 59), text="bí mật của nhà mình")
    assert "No finished evaluation run yet" in await acceptance.report(pool, SEOUL, 2, now=NOW)
    await _evaluated(pool)
    text = await acceptance.report(pool, SEOUL, 2, CPU, now=NOW)
    assert text.startswith("## P2 acceptance numbers (spec §14) — 2026-10-15 … 2026-10-16 (2 days, Asia/Seoul)")
    assert ("Evaluation numbers from run R1 (2026-10-16 10:00): gain ≤ 30 dB, VAD threshold 0.5, rules: none,"
            " overrides: none") in text
    assert "WARNING" not in text  # the live settings: nothing to flag
    # n counts captioned keyframes for the video path and recordings for the audio path: the table says which
    assert "| Path | n | Average | p95 | Over 6 h | Target |" in text
    assert "| video → caption (n = captioned keyframes) | 1 | 1.0 min | 1.0 min | 0 | met |" in text
    assert "| audio → transcript (n = recordings) | 1 | 0.5 min | 0.5 min | 0 | met |" in text
    assert "### ASR (evaluation run R1," in text
    assert ("Golden clips: 2 (0 could not be transcribed), recorded 2026-10-16 … 2026-10-20; references edited from "
            "Whisper's text: 1") in text
    assert "| vi | 1 | 4 | 25.0 % (—) | 11 | 27.3 % (—) | yes (WER) |" in text and "| yes (WER) |" in text and "| yes (CER) |" in text
    assert "| en | 0 | — | — | — | — | not measurable (no clip) |" in text
    assert "Baseline measured for: vi, ko; not measurable (no clip): en" in text
    assert "- ollama:minicpm-v4.6:1b: 30 keyframes, mean 3.50/5, ≥ 4: 18" in text
    assert ("hit@5 72.0 %: 18 of 25 questions (95 % 52.4–85.7 %; met); capped recall@5 68.0 %, hit@10 80.0 %,"
            " MRR 0.61. Each question was searched with the owner's days for it") in text
    assert "| 2026-10-16 | 1 | 2 min |" in text  # keyframes and speech of the day
    assert "bí mật" not in text and "một hai" not in text  # numbers only


def _rows(database_url) -> int:
    with psycopg.connect(database_url) as conn:
        return conn.execute("SELECT (SELECT count(*) FROM eval_run) + (SELECT count(*) FROM item)").fetchone()[0]


def test_the_cli_prints_the_report(database_url, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("LOOPI_DATABASE_URL", database_url)
    before = _rows(database_url)
    samples = tmp_path / "cpu.txt"
    samples.write_text(CPU)
    assert cli.main(["acceptance", "--days", "1", "--cpu-samples", str(samples)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("## P2 acceptance numbers") and "| worker |" in out
    monkeypatch.setattr("sys.stdin", io.StringIO(CPU))
    assert cli.main(["acceptance", "--cpu-samples", "-"]) == 0 and "| models |" in capsys.readouterr().out
    today = dt.datetime.now(Settings().zone).date()  # the window is the last two complete days before today
    first, second = today - dt.timedelta(days=2), today - dt.timedelta(days=1)
    assert cli.main(["acceptance", "--days", "2", "--exclude-day", str(first), "--exclude-day", str(second)]) == 0
    assert f"Left out (reprocessed): {first}, {second}" in capsys.readouterr().out
    assert cli.main(["acceptance", "--days", "0"]) == 2
    assert _rows(database_url) == before  # read only


def test_what_the_last_run_could_not_measure_is_said():
    run = {"id": "R", "requested_at": NOW, "results": {
        "asr": {"clips": 0, "variants": {"live": {"rules": ["outro"], "languages": {},
                                                  "no_speech": {"clips": 0, "with_text": 0, "invented": 0, "words": 0,
                                                                "rate": None, "rate_ci": None}}}},
        "captions": {}, "recall": {"questions": 0}}}
    lines = acceptance._evaluation_lines(run, SEOUL)
    assert lines[0] == "### ASR (evaluation run R, 2026-10-17 10:00; rules: outro)"
    assert "- none scored" in lines and lines[-1] == "Not measured: no question with a correct moment"
    assert "No-speech clips: 0 (0 with Whisper's text when labelled), with invented text: 0 (—, 95 % —; 0 words)" in lines
    assert "Baseline measured for: none; not measurable (no clip): vi, ko, en" in lines


async def test_a_run_with_overrides_is_flagged_right_under_the_title(pool, pki_settings, dev):
    await _evaluated(pool, {"overrides": {"max_gain_db": 12.0, "vad_threshold": 0.35}, "max_gain_db": 12.0,
                            "vad_threshold": 0.35, "rules": ["outro"]})
    lines = (await acceptance.report(pool, SEOUL, 2, now=NOW)).splitlines()
    assert lines[0].startswith("## P2 acceptance numbers") and lines[1] == ""
    assert lines[2] == ("> **WARNING: run R1 used overrides (max_gain_db=12, vad_threshold=0.35): its ASR numbers are"
                        " not those of the live settings. Run `loopi-brain eval run` without overrides first.**")
    assert lines[4] == ("Evaluation numbers from run R1 (2026-10-16 10:00): gain ≤ 12 dB, VAD threshold 0.35, "
                        "rules: outro, overrides: max_gain_db=12, vad_threshold=0.35")


def test_a_run_that_recorded_no_settings_says_so():
    run = {"id": "R", "requested_at": NOW, "results": {}}  # an older run: no config at all
    assert acceptance._run_lines(None, SEOUL) == []
    assert acceptance._run_lines(run, SEOUL) == [
        "Evaluation numbers from run R (2026-10-17 10:00): gain not recorded, VAD threshold not recorded, "
        "rules: not recorded, overrides: none"]


TWO_HOURS = """\
worker 2026-10-15T19:28:10Z usage_usec 47672930298 started=2026-10-15T14:51:34.148257075Z
worker 2026-10-15T21:28:10Z usage_usec 54872930298 started=2026-10-15T14:51:34.148257075Z
worker memory.peak 4500000000
"""


def test_a_cpu_window_shorter_than_a_day_is_marked_as_extrapolated():
    lines = acceptance._cpu_lines(TWO_HOURS)
    assert ("| worker | 2026-10-15 19:28 → 2026-10-15 21:28 UTC (2.0 h) | 2.0 CPU-h | "
            "≈ 86400 s (≈ 24.0 CPU-h), extrapolated from 2.0 h | 4.50 GB |") in lines
    # 24 h or more: a measured day, no marker (the same two samples 30 h apart)
    thirty_hours = TWO_HOURS.replace("2026-10-15T21:28:10Z", "2026-10-17T01:28:10Z")
    long = acceptance._cpu_lines(thirty_hours)
    assert any("(30.0 h)" in line for line in long) and not any("extrapolated" in line for line in long)
    assert not any("extrapolated" in line for line in acceptance._cpu_lines(CPU))  # exactly 24.0 h


async def test_every_section_uses_the_window_of_one_clock_reading(pool, monkeypatch):
    # The clock passes local midnight while the report is built: a section that read it again would take another window.
    ticks = iter([local(17, 23, 59), *[local(18, 0, 1)] * 20])
    monkeypatch.setattr(acceptance, "local_now", lambda zone, now=None: (now or next(ticks)).astimezone(zone))
    text = await acceptance.report(pool, SEOUL, 2)
    assert text.startswith("## P2 acceptance numbers (spec §14) — 2026-10-15 … 2026-10-16 (2 days, Asia/Seoul)")
    assert {day for day in re.findall(r"^\| (2026-\d\d-\d\d) \|", text, re.M)} == {"2026-10-15", "2026-10-16"}


async def test_the_per_day_latency_shows_how_many_recordings_each_day_rests_on(pool, pki_settings, dev):
    d = dev["device_id"]
    for hour, delay in ((9, 60), (10, 120), (11, 180)):
        await _captioned(pool, pki_settings, d, local(15, hour), delay)
    await _transcribed(pool, d, local(16, 10), 30)
    lines = acceptance._latency_lines(await acceptance.latency(pool, SEOUL, 2, now=NOW))
    assert "| Day | video keyframes | video avg | video p95 | audio recordings | audio avg | audio p95 |" in lines
    assert "| 2026-10-15 | 3 | 2.0 min | 2.9 min | 0 | — | — |" in lines
    assert "| 2026-10-16 | 0 | — | — | 1 | 0.5 min | 0.5 min |" in lines


async def _fail_job(pool, job_id: int) -> None:
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 4 WHERE id = %s", (job_id,))


async def test_recordings_still_waiting_or_failed_are_counted_apart(pool, pki_settings, dev, jobs_open):
    d = dev["device_id"]
    # audio: one waiting, one dead-lettered; a night one and a done one do not count
    await insert_item(pool, d, kind="audio", started_at=local(16, 9))
    broken = await insert_item(pool, d, kind="audio", started_at=local(16, 10))
    await _fail_job(pool, await dispatch.defer_item(pool, broken))
    queued = await insert_item(pool, d, kind="audio", started_at=local(16, 11))
    assert await dispatch.defer_item(pool, queued) is not None  # a job that is still queued: waiting
    await insert_item(pool, d, kind="audio", started_at=local(16, 23))
    await _transcribed(pool, d, local(16, 12), 30)
    # video: a segment not analysed yet, and keyframes of an analysed one without a caption (one dead-lettered)
    await insert_item(pool, d, kind="video", started_at=local(15, 9))
    seen = await insert_item(pool, d, kind="video", started_at=local(15, 13))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (seen,))
    await insert_keyframe(pool, pki_settings, seen, d, local(15, 13, 0))
    stuck = await insert_keyframe(pool, pki_settings, seen, d, local(15, 13, 1))
    await _fail_job(pool, await dispatch.defer_keyframe(pool, stuck))
    captioned = await insert_keyframe(pool, pki_settings, seen, d, local(15, 13, 2))
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption = '{}', captioned_at = %s WHERE id = %s",
                           (local(15, 14), captioned))  # captioned: not waiting
    found = await acceptance.latency(pool, SEOUL, 2, now=NOW)
    assert found["unanalysed"] == {"video": {"waiting": 2, "failed": 1}, "audio": {"waiting": 2, "failed": 1}}
    lines = acceptance._latency_lines(found)
    assert ("Without a caption or transcript yet, so in none of the numbers above: video 2 waiting, 1 failed "
            "(keyframes without a caption, segments not yet analysed); audio 2 waiting, 1 failed (segments)") in lines
    found = await acceptance.latency(pool, SEOUL, 2, now=NOW, exclude=[dt.date(2026, 10, 16)])
    assert found["unanalysed"]["audio"] == {"waiting": 0, "failed": 0}  # a left-out day leaves its waiting ones out too


async def test_a_left_out_day_outside_the_window_is_said_to_be_outside(pool):
    found = await acceptance.latency(pool, SEOUL, 2, now=NOW,
                                     exclude=[dt.date(2026, 10, 16), dt.date(2026, 10, 20), dt.date(2026, 10, 16)])
    assert found["excluded"] == [dt.date(2026, 10, 16)] and found["outside"] == [dt.date(2026, 10, 20)]
    lines = acceptance._latency_lines(found)
    assert "Left out (reprocessed): 2026-10-16" in lines
    assert "Not in the window, so nothing was left out for it: 2026-10-20" in lines
    lines = acceptance._latency_lines(await acceptance.latency(pool, SEOUL, 2, now=NOW, exclude=[dt.date(2026, 9, 1)]))
    assert not any(line.startswith("Left out") for line in lines)
    assert "Not in the window, so nothing was left out for it: 2026-09-01" in lines

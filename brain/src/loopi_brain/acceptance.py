"""The P2 acceptance numbers (spec §14 P2), from the brain's database and the CPU samples of a measurement window.

- Latency from recording to caption (a video segment's end → its keyframe's caption) and to transcript (an audio
  segment's end → its transcription) for recordings that ended between 08:00 and 22:00 local time, over the last
  N complete local days: average and p95, overall and per day. Target: average ≤ 15 minutes. A day the owner
  reprocessed is left out on request (its captions and transcripts were redone days later), and delays over 6 h are
  counted apart so a reprocess or a backlog shows.
- A recap every evening: for each of those days with recorded moments, whether its recap exists and when it was
  written.
- From the newest finished evaluation run (D22): WER and CER for every language with clip counts and 95 % intervals
  (the 30 % rule reads WER for vi and en, CER for ko), the no-speech check, and hit@5 (target 70 %) with its interval,
  capped recall@5, hit@10 and MRR; a language without a clip is "not measurable", never a pass.
- CPU time per day from two cgroup samples per service, taken on the host as the P2a measurement window did
  (``<service> <UTC time> usage_usec <n> started=<container start>`` and ``<service> memory.peak <bytes>`` lines).
- For context (spec §9.6): keyframes and minutes of transcribed speech per day.
- Which run the evaluation numbers come from, and its gain, VAD threshold and rules; a run with overrides is flagged
  first of all (R-I2).

The report holds numbers only: never a transcript, a caption or a question.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from .days import day_bounds_utc, local_now
from .evaluation import runner

LATENCY_TARGET_S = 15 * 60  # spec §14 P2: average ≤ 15 minutes, 08:00–22:00
DAY_HOURS = (8, 22)
_IN_HOURS = (
    "{ended} >= %(start)s AND {ended} < %(end)s"
    " AND extract(hour FROM {ended} AT TIME ZONE %(tz)s) >= %(first_hour)s"
    " AND extract(hour FROM {ended} AT TIME ZONE %(tz)s) < %(last_hour)s"
    " AND NOT (({ended} AT TIME ZONE %(tz)s)::date = ANY(%(exclude)s::date[]))"
)
LATE_S = 6 * 3600  # a delay this long is a reprocess or a backlog, not live latency: counted apart
VIDEO_DELAYS = (
    "SELECT (i.ended_at AT TIME ZONE %(tz)s)::date AS day, extract(epoch FROM k.captioned_at - i.ended_at)::float8 AS s"
    " FROM keyframe k JOIN item i ON i.id = k.item_id WHERE k.captioned_at IS NOT NULL AND "
    + _IN_HOURS.format(ended="i.ended_at")
)
AUDIO_DELAYS = (
    "SELECT (ended_at AT TIME ZONE %(tz)s)::date AS day, extract(epoch FROM analyzed_at - ended_at)::float8 AS s"
    " FROM item WHERE kind = 'audio' AND analysis_state = 'done' AND analyzed_at IS NOT NULL AND "
    + _IN_HOURS.format(ended="ended_at")
)
_SUMMARY = (
    "SELECT {by} count(*) AS n, avg(s) AS avg, percentile_cont(0.95) WITHIN GROUP (ORDER BY s) AS p95,"
    " count(*) FILTER (WHERE s > %(late)s) AS late FROM ({delays}) t {group}"
)


def window(zone, days: int, now: dt.datetime | None = None) -> tuple[list[dt.date], dt.datetime, dt.datetime]:
    """The last ``days`` complete local days before today, and their UTC bounds."""
    if days < 1:
        raise ValueError("at least one day")
    today = local_now(zone, now).date()
    dates = [today - dt.timedelta(days=n) for n in range(days, 0, -1)]
    return dates, day_bounds_utc(dates[0], zone)[0], day_bounds_utc(dates[-1], zone)[1]


async def latency(pool: AsyncConnectionPool, zone, days: int, now: dt.datetime | None = None,
                  exclude: list[dt.date] | tuple = ()) -> dict:
    """Recording → caption ("video") and recording → transcript ("audio") delays in seconds, overall and per day,
    without the days in ``exclude``; ``late`` counts the delays over 6 h."""
    dates, start, end = window(zone, days, now)
    params = {"tz": str(zone), "start": start, "end": end, "first_hour": DAY_HOURS[0], "last_hour": DAY_HOURS[1],
              "exclude": list(exclude), "late": LATE_S}
    out: dict = {"days": dates, "excluded": sorted(exclude)}
    async with pool.connection() as conn:
        for name, delays in (("video", VIDEO_DELAYS), ("audio", AUDIO_DELAYS)):
            cur = await conn.execute(_SUMMARY.format(by="", delays=delays, group=""), params)
            out[name] = await cur.fetchone()
            cur = await conn.execute(_SUMMARY.format(by="day,", delays=delays, group="GROUP BY day ORDER BY day"),
                                     params)
            out[f"{name}_by_day"] = {r["day"]: r for r in await cur.fetchall()}
    return out


async def recaps(pool: AsyncConnectionPool, zone, days: int, now: dt.datetime | None = None) -> list[dict]:
    """For each day of the window: its moments, and its recap's local writing time (None: no recap)."""
    dates, start, end = window(zone, days, now)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT (ts_start AT TIME ZONE %s)::date AS day, count(*) AS n FROM moment"
            " WHERE ts_start >= %s AND ts_start < %s GROUP BY 1",
            (str(zone), start, end),
        )
        moments = {r["day"]: r["n"] for r in await cur.fetchall()}
        cur = await conn.execute("SELECT day, generated_at, stale FROM day_recap WHERE day = ANY(%s)", (dates,))
        written = {r["day"]: r for r in await cur.fetchall()}
    return [{"day": day, "moments": moments.get(day, 0),
             "recap_at": written[day]["generated_at"].astimezone(zone) if day in written else None,
             "stale": written[day]["stale"] if day in written else False}
            for day in dates]


async def per_day(pool: AsyncConnectionPool, zone, days: int, now: dt.datetime | None = None) -> list[dict]:
    """Keyframes and minutes of transcribed speech of each day of the window (spec §9.6's estimates)."""
    dates, start, end = window(zone, days, now)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT (ts AT TIME ZONE %s)::date AS day, count(*) AS n FROM keyframe WHERE ts >= %s AND ts < %s"
            " GROUP BY 1", (str(zone), start, end))
        keyframes = {r["day"]: r["n"] for r in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT (start_ts AT TIME ZONE %s)::date AS day, sum(extract(epoch FROM end_ts - start_ts))::float8 AS s"
            " FROM transcript_segment WHERE start_ts >= %s AND start_ts < %s GROUP BY 1", (str(zone), start, end))
        speech = {r["day"]: r["s"] for r in await cur.fetchall()}
    return [{"day": day, "keyframes": keyframes.get(day, 0), "speech_min": speech.get(day, 0.0) / 60} for day in dates]


@dataclass(frozen=True)
class CpuSample:
    service: str
    at: dt.datetime
    usage_usec: int
    started: str


def parse_cpu(text: str) -> tuple[list[CpuSample], dict[str, int]]:
    """The samples and the newest ``memory.peak`` (bytes) per service; other lines are ignored."""
    samples, peaks = [], {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[2] == "usage_usec" and parts[4].startswith("started="):
            samples.append(CpuSample(parts[0], dt.datetime.fromisoformat(parts[1]), int(parts[3]),
                                     parts[4].split("=", 1)[1]))
        elif len(parts) == 3 and parts[1] == "memory.peak" and parts[2].isdigit():
            peaks[parts[0]] = int(parts[2])
    return samples, peaks


def cpu_per_day(samples: list[CpuSample]) -> dict[str, dict]:
    """Per service, from its first to its last sample: hours between them, CPU seconds used, CPU seconds per day.
    A container that restarted in between (another start time) has no usable number."""
    out: dict[str, dict] = {}
    for service in dict.fromkeys(s.service for s in samples):
        mine = sorted((s for s in samples if s.service == service), key=lambda s: s.at)
        first, last = mine[0], mine[-1]
        hours = (last.at - first.at).total_seconds() / 3600
        row = {"first": first.at, "last": last.at, "hours": hours, "restarted": first.started != last.started}
        if len(mine) < 2 or hours <= 0 or row["restarted"]:
            out[service] = {**row, "cpu_s": None, "per_day_s": None}
            continue
        cpu_s = (last.usage_usec - first.usage_usec) / 1e6
        out[service] = {**row, "cpu_s": cpu_s, "per_day_s": cpu_s / hours * 24}
    return out


def _minutes(seconds: float | None) -> str:
    return "—" if seconds is None else f"{seconds / 60:.1f} min"


def _latency_lines(found: dict) -> list[str]:
    lines = [f"### Latency, recordings that ended {DAY_HOURS[0]:02d}:00–{DAY_HOURS[1]:02d}:00 "
             "(target: average ≤ 15 min)", "",
             "| Path | Recordings | Average | p95 | Over 6 h | Target |", "|---|---|---|---|---|---|"]
    for name, label in (("video", "video → caption"), ("audio", "audio → transcript")):
        row = found[name]
        verdict = "—" if not row["n"] else ("met" if row["avg"] <= LATENCY_TARGET_S else "not met")
        lines.append(f"| {label} | {row['n']} | {_minutes(row['avg'])} | {_minutes(row['p95'])} | {row['late']} | "
                     f"{verdict} |")
    if found["excluded"]:
        lines += ["", f"Left out (reprocessed): {', '.join(str(day) for day in found['excluded'])}"]
    lines += ["", "| Day | video avg | video p95 | audio avg | audio p95 |", "|---|---|---|---|---|"]
    for day in found["days"]:
        video, audio = found["video_by_day"].get(day), found["audio_by_day"].get(day)
        lines.append(f"| {day} | {_minutes(video and video['avg'])} | {_minutes(video and video['p95'])} | "
                     f"{_minutes(audio and audio['avg'])} | {_minutes(audio and audio['p95'])} |")
    return lines


def _recap_lines(rows: list[dict]) -> list[str]:
    lines = ["### A recap every evening", "", "| Day | Moments | Recap written |", "|---|---|---|"]
    for row in rows:
        written = row["recap_at"].strftime("%Y-%m-%d %H:%M") if row["recap_at"] else "**none**"
        lines.append(f"| {row['day']} | {row['moments']} | {written}{' (stale)' if row['stale'] else ''} |")
    with_data = [r for r in rows if r["moments"]]
    missing = [str(r["day"]) for r in with_data if r["recap_at"] is None]
    lines += ["", f"Days with recordings: {len(with_data)}; with a recap: {len(with_data) - len(missing)}"
                  f" (missing: {', '.join(missing) or 'none'})"]
    return lines


def _run_lines(run: dict | None, zone) -> list[str]:
    """Which finished run the evaluation numbers come from and the ASR settings it ran with. A run with overrides
    (a gain or VAD threshold for that run only) is flagged first of all: its WER and CER are not the live settings'."""
    if run is None:
        return []
    config = run.get("config") or {}
    overrides = ", ".join(f"{key}={value:g}" for key, value in (config.get("overrides") or {}).items())
    when = run["requested_at"].astimezone(zone).strftime("%Y-%m-%d %H:%M")
    gain, vad, rules = config.get("max_gain_db"), config.get("vad_threshold"), config.get("rules")
    lines = [f"Evaluation numbers from run {run['id']} ({when}): "
             f"gain {'not recorded' if gain is None else f'≤ {gain:g} dB'}, "
             f"VAD threshold {'not recorded' if vad is None else f'{vad:g}'}, "
             f"rules: {'not recorded' if rules is None else ', '.join(rules) or 'none'}, "
             f"overrides: {overrides or 'none'}"]
    if overrides:
        lines = [f"> **WARNING: run {run['id']} used overrides ({overrides}): its ASR numbers are not those of the "
                 "live settings. Run `loopi-brain eval run` without overrides first.**", "", *lines]
    return lines


def _evaluation_lines(run: dict | None, zone) -> list[str]:
    if run is None:
        return ["### Evaluation", "", "No finished evaluation run yet (`loopi-brain eval run`)."]
    results = run["results"]
    audio = results["asr"]
    live = audio["variants"]["live"]
    when = run["requested_at"].astimezone(zone).strftime("%Y-%m-%d %H:%M")
    recorded = audio.get("recorded")
    lines = [f"### ASR (evaluation run {run['id']}, {when}; rules: {', '.join(live['rules']) or 'none'})", "",
             f"Golden clips: {audio['clips']}"
             + (f", recorded {recorded['first']} … {recorded['last']}" if recorded else "")
             + f"; references edited from Whisper's text: {audio.get('prefilled', 0)}, typed blind: "
               f"{audio.get('blind', 0)}. The 30 % rule reads WER for vi and en, CER for ko (D22); 95 % intervals "
               "resample whole clips.", "",
             "| Language | Clips | Words | WER (95 %) | Characters | CER (95 %) | ≤ 30 % |",
             "|---|---|---|---|---|---|---|"]
    measured, missing = [], []
    for lang in ("vi", "ko", "en"):
        row = live["languages"].get(lang)
        if row is None:
            missing.append(lang)
            lines.append(f"| {lang} | 0 | — | — | — | — | not measurable (no clip) |")
            continue
        measured.append(lang)
        verdict = "no — change the model or the configuration" if row["over_limit"] else "yes"
        lines.append(f"| {lang} | {row['clips']} | {row['words']} | {runner.pct(row['wer'])} "
                     f"({runner.span(row['wer_ci'])}) | {row['chars']} | {runner.pct(row['cer'])} "
                     f"({runner.span(row['cer_ci'])}) | {verdict} ({row['metric'].upper()}) |")
    silent = live["no_speech"]
    lines += ["", f"Baseline measured for: {', '.join(measured) or 'none'}"
                  + (f"; not measurable (no clip): {', '.join(missing)}" if missing else ""),
              "", f"No-speech clips: {silent['clips']} ({silent['with_text']} with Whisper's text when labelled), "
                  f"with invented text: {silent['invented']} ({runner.pct(silent['rate'])}, 95 % "
                  f"{runner.span(silent['rate_ci'])}; {silent['words']} words)", "", "### Captions (owner's scores)", ""]
    lines += [f"- {model}: {row['scored']} keyframes, mean {row['mean']:.2f}/5, ≥ 4: {row['good']}"
              for model, row in results["captions"].items()] or ["- none scored"]
    recall = results["recall"]
    lines += ["", "### Recall@5 = hit@5 (D22; target ≥ 70 %)", ""]
    if "hit5" in recall:
        lines.append(f"hit@5 {runner.pct(recall['hit5'])}: {recall['found5']} of {recall['questions']} questions "
                     f"(95 % {runner.span(recall['hit5_ci'])}; {'met' if recall['meets_target'] else 'not met'}); "
                     f"capped recall@5 {runner.pct(recall['recall5'])}, hit@10 {runner.pct(recall['hit10'])}, "
                     f"MRR {recall['mrr']:.2f}. Each question was searched with the owner's days for it, not the "
                     "chat's time filter.")
    else:
        lines.append(f"Not measured: {recall.get('error', 'no question with a correct moment')}")
    return lines


def _cpu_lines(samples_text: str | None) -> list[str]:
    lines = ["### CPU time per day", ""]
    if samples_text is None:
        return [*lines, "No CPU samples given (`--cpu-samples`)."]
    samples, peaks = parse_cpu(samples_text)
    if not samples:
        return [*lines, "No CPU sample lines found."]
    lines += ["| Service | Window | CPU time | CPU per day | Memory peak |", "|---|---|---|---|---|"]
    for service, row in cpu_per_day(samples).items():
        span = f"{row['first']:%Y-%m-%d %H:%M} → {row['last']:%Y-%m-%d %H:%M} UTC ({row['hours']:.1f} h)"
        peak = f"{peaks[service] / 1e9:.2f} GB" if service in peaks else "—"
        if row["cpu_s"] is None:
            why = "restarted in between" if row["restarted"] else "needs two samples"
            lines.append(f"| {service} | {span} | — ({why}) | — | {peak} |")
        else:
            lines.append(f"| {service} | {span} | {row['cpu_s'] / 3600:.1f} CPU-h | "
                         f"{row['per_day_s']:.0f} s ({row['per_day_s'] / 3600:.1f} CPU-h) | {peak} |")
    return lines


async def report(pool: AsyncConnectionPool, zone, days: int, samples_text: str | None = None,
                 now: dt.datetime | None = None, exclude: list[dt.date] | tuple = ()) -> str:
    """The P2 acceptance numbers as Markdown (``exclude``: days left out of the latency, e.g. reprocessed ones)."""
    dates, _start, _end = window(zone, days, now)
    run = await runner.latest_run(pool, finished=True)
    lines = [f"## P2 acceptance numbers (spec §14) — {dates[0]} … {dates[-1]} ({days} days, {zone})", ""]
    lines += [*_run_lines(run, zone), ""] if run else []
    lines += _latency_lines(await latency(pool, zone, days, now, exclude)) + [""]
    lines += _recap_lines(await recaps(pool, zone, days, now)) + [""]
    lines += _evaluation_lines(run, zone) + [""]
    lines += _cpu_lines(samples_text) + [""]
    lines += ["### Context (spec §9.6: 100–300 keyframes and 1–3 h of speech a day)", "",
              "| Day | Keyframes | Speech |", "|---|---|---|"]
    lines += [f"| {r['day']} | {r['keyframes']} | {r['speech_min']:.0f} min |" for r in await per_day(pool, zone, days, now)]
    return "\n".join(lines) + "\n"

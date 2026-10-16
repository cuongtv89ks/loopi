"""Evaluation runs (spec §13, D22): the owner's golden set against the pipeline as it is configured now.

A run transcribes every golden clip again through the live ASR path — level normalisation (D19), Silero VAD, Whisper
and the P2a filters, with the run's gain ceiling and VAD threshold — and keeps every Whisper segment before the
switchable rules, so that one transcription scores every rule set: the live one (LOOPI_ASR_RULES), none, each rule
alone and all of them (``VARIANTS``), with the segments each one would drop from real speech. Then it sums the
owner's caption scores per caption model and asks the hybrid search every question that has a correct moment:
hit@5 (the §14 number, D22), capped recall@5, hit@10 and MRR with the live thresholds, and hit@5 over a small grid
of thresholds around them. Every rate comes with its sample size and a 95 % interval.

One clip per job: each clip job holds Whisper (job lock "asr") for one clip and waits behind every live
transcription (lowest priority, ``EVAL_PRIORITY``), so recordings keep their latency target while a run goes on
(spec §14 P2); the last job computes the numbers under its own lock ("eval"), never holding Whisper's, and embeds
each question once (a failing embedding ends the recall section at once instead of waiting per search). A run's
results are numbers only; the segments it heard stay in the database next to the golden clips (deleted with them).
Logs carry ids and counts, never text.
"""
from __future__ import annotations

import datetime as dt
import logging

import anyio
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .. import devices, media
from ..config import ASR_RULES, Settings
from ..days import range_utc
from ..pipeline import asr, jobs
from ..pipeline.context import PipelineContext, get_context
from ..pipeline.jobs import job_app
from ..pipeline.state import error_text
from ..providers.base import ProviderError, ProviderUnavailable
from ..providers.router import Router
from ..search import QUERY_INSTRUCTION, clean_query, search_moments
from . import metrics, questions

log = logging.getLogger(__name__)
EVAL_PRIORITY = -20  # below the chapter and recap jobs (-10): an evaluation never runs ahead of live work
FINAL_LOCK = "eval"  # the numbers job: not Whisper's lock ("asr"), so live transcription never waits for it
WER_LIMIT = 0.30  # spec §14 P2 (D22): above 30 % (WER for vi and en, CER for ko), change the model or configuration
HIT_TARGET = 0.70  # spec §14 P2 (D22): hit@5 ≥ 70 %
MAX_GAIN_DB = 30.0  # D19: the brain never raises a recording by more than 30 dB, in a run either
PRIMARY = {"vi": "wer", "en": "wer", "ko": "cer"}  # the rate the 30 % rule reads, per language (D22)
DISTANCES = (0.5, 0.6, 0.7, 0.8)  # the threshold grid of the recall section
SIMILARITIES = (0.2, 0.3, 0.4)
RESULTS_SHOWN = 10  # hit@10 needs the first 10 results
VARIANTS = ("live", "none", *ASR_RULES, "all")  # the rule sets in the order they are shown (JSONB keeps no order)


def rule_sets(live: list[str]) -> dict[str, list[str]]:
    """The rule sets a run scores: the live one, none, each rule alone, all of them (in VARIANTS order)."""
    return {"live": sorted(live), "none": [], **{rule: [rule] for rule in ASR_RULES}, "all": list(ASR_RULES)}


def run_lock(run_id: str) -> str:
    return f"eval:{run_id}"


async def _defer_step(conn, run_id: str, final: bool) -> int:
    """The next job of a run: a clip job (Whisper's lock) or, once no clip is left, the numbers job (its own)."""
    return await jobs.enqueue(conn, jobs.RUN_EVAL, queue="light", lock=FINAL_LOCK if final else "asr",
                              priority=EVAL_PRIORITY, queueing_lock=run_lock(run_id), run_id=run_id, final=final)


async def start_run(
    pool: AsyncConnectionPool, settings: Settings, *, max_gain_db: float | None = None,
    vad_threshold: float | None = None,
) -> tuple[str, bool]:
    """Ask for a run (``max_gain_db`` and ``vad_threshold`` override the ASR's for this run only); return (run id,
    created). The search thresholds are this process's (the api serves the search); the ASR settings, rules and
    model versions are the worker's, recorded when it starts the run. One run at a time: while one waits or runs,
    its id is returned. A run whose job is gone (the worker died and the job was dead-lettered, say) is marked
    failed first."""
    if max_gain_db is not None and not 0.0 <= max_gain_db <= MAX_GAIN_DB:
        raise ValueError("the gain ceiling is 0 to 30 dB (D19)")
    if vad_threshold is not None and not 0.0 < vad_threshold < 1.0:
        raise ValueError("the VAD threshold lies between 0 and 1")
    config = {
        "max_distance": settings.search_max_distance,
        "min_similarity": settings.search_min_similarity,
        "overrides": {k: v for k, v in (("max_gain_db", max_gain_db), ("vad_threshold", vad_threshold)) if v is not None},
    }
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('eval_run', 0))")
        cur = await conn.execute(
            "SELECT r.id, EXISTS (SELECT 1 FROM procrastinate_jobs j WHERE j.queueing_lock = 'eval:' || r.id"
            " AND j.status IN ('todo', 'doing')) AS alive FROM eval_run r WHERE r.status IN ('waiting', 'running')"
        )
        for row in await cur.fetchall():
            if row["alive"]:
                return row["id"], False
            await conn.execute(
                "UPDATE eval_run SET status = 'failed', finished_at = now(),"
                " error = 'the run stopped (its job is gone: a worker crash or restart); start a new one' WHERE id = %s",
                (row["id"],),
            )
        run_id = devices.new_id()
        await conn.execute("INSERT INTO eval_run (id, config) VALUES (%s, %s)", (run_id, Jsonb(config)))
        cur = await conn.execute("SELECT EXISTS (SELECT 1 FROM eval_audio) AS clips")
        await _defer_step(conn, run_id, final=not (await cur.fetchone())["clips"])
    log.info("evaluation run %s requested", run_id)
    return run_id, True


def _regions(results: list[tuple[asr.Region, asr.RegionResult]]) -> list[dict]:
    return [
        {"start": region.start, "end": region.end, "lang": result.lang, "lang_prob": result.lang_prob,
         "segments": [{"start": s.start, "end": s.end, "text": s.text, "avg_logprob": s.avg_logprob,
                       "no_speech_prob": s.no_speech_prob, "compression_ratio": s.compression_ratio}
                      for s in result.segments]}
        for region, result in results
    ]


def _kept(regions: list[dict], rules: list[str]) -> list[str]:
    """What the live stage would store for a clip under ``rules``: the kept segments' text, in order."""
    kept = []
    for region in regions:
        result = asr.RegionResult(region["lang"], region["lang_prob"],
                                  [asr.SegmentResult(**s) for s in region["segments"]])
        kept.extend(s.text.strip() for s in asr.kept_segments(result, frozenset(rules)))
    return kept


def _interval(value: tuple[float, float] | None) -> list[float] | None:
    return list(value) if value else None


def asr_numbers(clips: list[dict], rules: list[str]) -> dict:
    """For one rule set, over the clips of a run: WER and CER per language with their clip counts and 95 % intervals
    (resampling clips), the no-speech check (clips with invented text, Wilson interval), and what the rules drop from
    the clips with speech beyond the P2a filters (0 segments there means the rules were not tested on real speech)."""
    word: dict[str, list[metrics.Errors]] = {}
    char: dict[str, list[metrics.Errors]] = {}
    silent = with_text = invented = invented_words = dropped_segments = dropped_words = 0
    for clip in clips:
        kept = _kept(clip["segments"], rules)
        heard = " ".join(kept)
        if clip["no_speech"]:
            silent += 1
            with_text += bool(clip.get("had_text"))
            invented += bool(metrics.words(heard))
            invented_words += len(metrics.words(heard))
            continue
        before = _kept(clip["segments"], [])
        dropped_segments += len(before) - len(kept)
        dropped_words += len(metrics.words(" ".join(before))) - len(metrics.words(heard))
        word.setdefault(clip["lang"], []).append(metrics.wer(clip["reference"], heard))
        char.setdefault(clip["lang"], []).append(metrics.cer(clip["reference"], heard))
    languages = {}
    for lang in sorted(word):
        words_total, chars_total = sum(word[lang], metrics.Errors()), sum(char[lang], metrics.Errors())
        rates = {"wer": words_total.rate, "cer": chars_total.rate}
        cis = {"wer_ci": _interval(metrics.bootstrap(word[lang])), "cer_ci": _interval(metrics.bootstrap(char[lang]))}
        metric = PRIMARY.get(lang, "wer")
        score = rates[metric]
        languages[lang] = {"clips": len(word[lang]), "words": words_total.reference, "chars": chars_total.reference,
                           **rates, **cis, "metric": metric, "score": score, "score_ci": cis[f"{metric}_ci"],
                           "over_limit": score is not None and score > WER_LIMIT}
    return {"languages": languages,
            "no_speech": {"clips": silent, "with_text": with_text, "invented": invented, "words": invented_words,
                          "rate": invented / silent if silent else None,
                          "rate_ci": _interval(metrics.wilson(invented, silent))},
            "dropped": {"segments": dropped_segments, "words": dropped_words}}


async def caption_numbers(pool: AsyncConnectionPool) -> dict:
    """The owner's caption scores per caption model."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT model_version, count(*) AS scored, avg(score)::float8 AS mean,"
            " count(*) FILTER (WHERE score >= 4) AS good, count(*) FILTER (WHERE wrong_people) AS wrong_people,"
            " count(*) FILTER (WHERE invented_objects) AS invented_objects,"
            " count(*) FILTER (WHERE wrong_activity) AS wrong_activity"
            " FROM eval_caption_score GROUP BY model_version ORDER BY model_version"
        )
        return {r["model_version"]: {k: v for k, v in r.items() if k != "model_version"} for r in await cur.fetchall()}


class _CachedEmbeddings:
    """The router as the search sees it: each question is embedded once, before any search (``prefetch``); after that
    a question that is not in the cache fails at once (the search then reads words only), never a second wait."""

    def __init__(self, router: Router):
        self._router = router
        self._cache: dict[tuple[str, ...], object] = {}
        self._frozen = False

    async def prefetch(self, task: str, queries: list[str]) -> None:
        for query in dict.fromkeys(q for q in (clean_query(q) for q in queries) if q):
            await self.embed(task, [QUERY_INSTRUCTION + query])
        self._frozen = True

    async def embed(self, task: str, texts: list[str]):
        key = (task, *texts)
        if key not in self._cache:
            if self._frozen:
                raise ProviderUnavailable("question not embedded before the searches")
            self._cache[key] = await self._router.embed(task, texts)
        return self._cache[key]


async def _recall(pool, router, asked: list[dict], zone, max_distance: float, min_similarity: float) -> dict:
    found5 = found10 = 0
    recall5 = mrr = 0.0
    semantic = True
    per_question = []
    for question in asked:
        start, end = range_utc(question["first_day"], question["last_day"], zone)
        result = await search_moments(pool, router, question["text"], start=start, end=end, limit=RESULTS_SHOWN,
                                      max_distance=max_distance, min_similarity=min_similarity)
        semantic = semantic and result.semantic
        ranks = questions.ranks(question["answers"], result.hits)
        relevant = len(question["answers"])
        found5 += int(metrics.hit_at(ranks, 5))
        found10 += int(metrics.hit_at(ranks, 10))
        recall5 += metrics.recall_at(ranks, relevant, 5)
        mrr += metrics.reciprocal_rank(ranks)
        per_question.append({"id": question["id"], "relevant": relevant, "ranks": ranks})
    n = len(asked)
    return {"found5": found5, "hit5": found5 / n, "hit5_ci": _interval(metrics.wilson(found5, n)),
            "recall5": recall5 / n, "hit10": found10 / n, "mrr": mrr / n, "semantic": semantic,
            "per_question": per_question}


async def recall_numbers(pool: AsyncConnectionPool, router: Router, config: dict, zone) -> dict:
    """Retrieval over the answered questions, each searched with the owner's days for it (not the chat's time
    filter): hit@5 (D22) with its interval, capped recall@5, hit@10 and MRR with the live thresholds, and hit@5 over
    the grid. Not measured while the embedding model cannot run or fails on a question (words alone would understate
    it): each question is embedded once, before any search."""
    asked = await questions.answered(pool)
    if not asked:
        return {"questions": 0}
    if not router.task_ready("embed", await router.health()):
        return {"questions": len(asked), "error": "the embedding model is not available"}
    cached = _CachedEmbeddings(router)
    try:
        await cached.prefetch("embed", [q["text"] for q in asked])
    except ProviderError as exc:
        log.warning("evaluation: recall not measured, a question could not be embedded: %s", error_text(exc))
        return {"questions": len(asked), "error": "a question could not be embedded (the embedding model failed)"}
    live = await _recall(pool, cached, asked, zone, config["max_distance"], config["min_similarity"])
    grid = []
    for distance in DISTANCES:
        for similarity in SIMILARITIES:
            found = await _recall(pool, cached, asked, zone, distance, similarity)
            grid.append({"max_distance": distance, "min_similarity": similarity, "found5": found["found5"],
                         "hit5": found["hit5"]})
    return {"questions": len(asked), **live, "meets_target": live["hit5"] >= HIT_TARGET, "grid": grid}


async def _clips(pool: AsyncConnectionPool, run_id: str) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT e.id, e.lang, e.no_speech, e.reference, e.had_text, e.prefilled, i.started_at, r.segments, r.error"
            " FROM eval_run_audio r JOIN eval_audio e ON e.id = r.sample_id JOIN item i ON i.id = e.item_id"
            " WHERE r.run_id = %s ORDER BY e.id",
            (run_id,),
        )
        return await cur.fetchall()


def _score_variants(heard: list[dict], live: list[str]) -> dict:
    """Every rule set's ASR numbers over the clips a run heard. Blocking: plain-Python alignments (tens of seconds for
    a full golden set), so ``_finish`` runs it in a thread, never on the worker's event loop."""
    return {name: {"rules": rules, **asr_numbers(heard, rules)} for name, rules in rule_sets(live).items()}


async def _finish(ctx: PipelineContext, run_id: str, config: dict) -> None:
    zone = ctx.settings.zone
    clips = await _clips(ctx.pool, run_id)
    heard = [c for c in clips if c["segments"] is not None]
    days = sorted(c["started_at"].astimezone(zone).date() for c in clips)
    variants = await anyio.to_thread.run_sync(_score_variants, heard, config["rules"])
    results = {
        "asr": {"clips": len(clips), "failed": len(clips) - len(heard),
                "recorded": {"first": days[0].isoformat(), "last": days[-1].isoformat()} if days else None,
                "prefilled": sum(1 for c in clips if c["prefilled"] and not c["no_speech"]),
                "blind": sum(1 for c in clips if not c["prefilled"] and not c["no_speech"]),
                "variants": variants},
        "captions": await caption_numbers(ctx.pool),
        "recall": await recall_numbers(ctx.pool, ctx.router, config, zone),
    }
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE eval_run SET status = 'done', finished_at = now(), results = %s WHERE id = %s",
            (Jsonb(results), run_id),
        )
    log.info("evaluation run %s done: %d clips (%d failed), %d questions", run_id, len(clips),
             len(clips) - len(heard), results["recall"]["questions"])


async def _step(ctx: PipelineContext, run_id: str, final: bool) -> str:
    pool = ctx.pool
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT status, config FROM eval_run WHERE id = %s FOR UPDATE", (run_id,))
        run = await cur.fetchone()
        if run is None or run["status"] in ("done", "failed"):
            return "nothing"
        config = run["config"]
        if run["status"] == "waiting":  # the live ASR settings are the worker's
            asr_settings = ctx.settings
            config = {
                **config,
                "max_gain_db": config["overrides"].get("max_gain_db", asr_settings.asr_max_gain_db),
                "vad_threshold": config["overrides"].get("vad_threshold", asr_settings.asr_vad_threshold),
                "rules": sorted(asr_settings.asr_rule_set),
                "models": {task: ctx.router.model_version(task) for task in ("asr", "caption", "embed")},
            }
            await conn.execute("UPDATE eval_run SET status = 'running', started_at = now(), config = %s WHERE id = %s",
                               (Jsonb(config), run_id))
        if final:
            clip = None
        else:
            cur = await conn.execute(
                "SELECT e.id, e.path FROM eval_audio e WHERE NOT EXISTS (SELECT 1 FROM eval_run_audio r"
                " WHERE r.run_id = %s AND r.sample_id = e.id) ORDER BY e.id LIMIT 1",
                (run_id,),
            )
            clip = await cur.fetchone()
            if clip is None:  # the clip it was meant for is gone: the numbers job comes next
                await _defer_step(conn, run_id, final=True)
                return "clip"
    if final:  # never transcribes: it does not hold Whisper's lock
        await _finish(ctx, run_id, config)
        return "done"
    segments = error = None
    try:
        path = media.inside(ctx.settings.eval_dir, clip["path"])
        heard = await anyio.to_thread.run_sync(
            asr.transcribe_file, path, ctx.transcriber, config["max_gain_db"], config["vad_threshold"]
        )
        segments = _regions(heard)
    except Exception as exc:  # an unreadable or misplaced copy is counted as failed; the run goes on
        error = error_text(exc)
        log.warning("evaluation run %s: golden clip %s could not be transcribed: %s", run_id, clip["id"], error)
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO eval_run_audio (run_id, sample_id, segments, error)"
            " SELECT %s, id, %s, %s FROM eval_audio WHERE id = %s ON CONFLICT DO NOTHING",
            (run_id, None if segments is None else Jsonb(segments), error, clip["id"]),
        )
        cur = await conn.execute(
            "SELECT EXISTS (SELECT 1 FROM eval_audio e WHERE NOT EXISTS (SELECT 1 FROM eval_run_audio r"
            " WHERE r.run_id = %s AND r.sample_id = e.id)) AS more",
            (run_id,),
        )
        await _defer_step(conn, run_id, final=not (await cur.fetchone())["more"])
    return "clip"


async def run_step(ctx: PipelineContext, run_id: str, final: bool = False) -> str:
    """One job of a run: a clip job transcribes the next golden clip ("clip"); the final job computes the numbers
    ("done"). An unexpected error marks the run failed (with the error's type and message, never text from the golden
    set)."""
    try:
        return await _step(ctx, run_id, final)
    except Exception as exc:
        log.exception("evaluation run %s failed", run_id)
        async with ctx.pool.connection() as conn:
            await conn.execute(
                "UPDATE eval_run SET status = 'failed', finished_at = now(), error = %s WHERE id = %s",
                (error_text(exc), run_id),
            )
        return "failed"


@job_app.task(name=jobs.RUN_EVAL, queue="light", lock="asr", priority=EVAL_PRIORITY)
async def run_eval_job(run_id: str, final: bool = False) -> None:
    await run_step(get_context(), run_id, final)


async def latest_run(pool: AsyncConnectionPool, run_id: str | None = None, finished: bool = False) -> dict | None:
    """``run_id``'s run, or the newest one (the newest finished one when ``finished``), with its progress."""
    where = "WHERE r.id = %(id)s" if run_id else ("WHERE r.status = 'done'" if finished else "")
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT r.*, (SELECT count(*) FROM eval_run_audio a WHERE a.run_id = r.id) AS heard,"
            f" (SELECT count(*) FROM eval_audio) AS clips FROM eval_run r {where}"
            " ORDER BY r.requested_at DESC, r.id DESC LIMIT 1",
            {"id": run_id},
        )
        return await cur.fetchone()


async def list_runs(pool: AsyncConnectionPool, limit: int = 20) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT r.*, (SELECT count(*) FROM eval_run_audio a WHERE a.run_id = r.id) AS heard,"
            " (SELECT count(*) FROM eval_audio) AS clips FROM eval_run r ORDER BY r.requested_at DESC, r.id DESC"
            " LIMIT %s",
            (limit,),
        )
        return await cur.fetchall()


def pct(value: float | None) -> str:
    """A rate as a percentage with one decimal; — when there is none."""
    return "—" if value is None else f"{100 * value:.1f} %"


def span(interval: list[float] | None) -> str:
    """A 95 % interval as "a–b %"; — when there is none (fewer than two clips, no sample)."""
    return "—" if not interval else f"{100 * interval[0]:.1f}–{100 * interval[1]:.1f} %"


def _audio_lines(audio: dict) -> list[str]:
    recorded = audio.get("recorded")
    lines = [f"Clips: {audio['clips']} ({audio['failed']} could not be transcribed)"
             + (f", recorded {recorded['first']} … {recorded['last']}" if recorded else "")
             + f"; references edited from Whisper's text: {audio['prefilled']}, typed blind: {audio['blind']}", "",
             "| Rules | vi WER | ko CER | en WER | no speech: invented | dropped from speech |",
             "|---|---|---|---|---|---|"]
    for name in (v for v in VARIANTS if v in audio["variants"]):
        variant = audio["variants"][name]
        cells = []
        for lang in ("vi", "ko", "en"):
            row = variant["languages"].get(lang)
            cells.append("—" if row is None else
                         f"{pct(row['score'])} ({row['clips']} clips){' > 30 %' if row['over_limit'] else ''}")
        silent, dropped = variant["no_speech"], variant["dropped"]
        label = f"{name} ({', '.join(variant['rules']) or 'none'})" if name == "live" else name
        lines.append(f"| {label} | {' | '.join(cells)} | {silent['invented']}/{silent['clips']} "
                     f"({silent['words']} words) | {dropped['segments']} segments ({dropped['words']} words) |")
    live = audio["variants"]["live"]
    lines += ["", "Live rules, per language (95 % intervals over clips; the 30 % rule reads WER for vi and en, CER "
                  "for ko, D22):", "", "| Language | Clips | Words | WER | Characters | CER | ≤ 30 % |",
              "|---|---|---|---|---|---|---|"]
    for lang in ("vi", "ko", "en"):
        row = live["languages"].get(lang)
        if row is None:
            lines.append(f"| {lang} | 0 | — | — | — | — | not measurable (no clip) |")
            continue
        lines.append(f"| {lang} | {row['clips']} | {row['words']} | {pct(row['wer'])} ({span(row['wer_ci'])}) | "
                     f"{row['chars']} | {pct(row['cer'])} ({span(row['cer_ci'])}) | "
                     f"{'no' if row['over_limit'] else 'yes'} ({row['metric'].upper()}) |")
    silent = live["no_speech"]
    lines += ["", f"No speech: {silent['clips']} clips ({silent['with_text']} with Whisper's text when labelled), "
                  f"invented text in {silent['invented']} ({pct(silent['rate'])}, 95 % {span(silent['rate_ci'])})"]
    return lines


def _recall_lines(recall: dict, config: dict) -> list[str]:
    if "hit5" not in recall:
        return [f"Recall: not measured ({recall.get('error', 'no question has a correct moment yet')})"]
    n = recall["questions"]
    lines = [
        f"Retrieval ({n} questions, each searched with the owner's days for it, not the chat's time filter; max "
        f"distance {config['max_distance']:g}, min similarity {config['min_similarity']:g}):",
        f"hit@5 {pct(recall['hit5'])} = {recall['found5']}/{n} (95 % {span(recall['hit5_ci'])}; target 70 %, D22: "
        f"{'met' if recall['meets_target'] else 'not met'}), capped recall@5 {pct(recall['recall5'])}, hit@10 "
        f"{pct(recall['hit10'])}, MRR {recall['mrr']:.2f}"
        + ("" if recall["semantic"] else " — words only (the embedding model failed)"),
    ]
    best = max(recall["grid"], key=lambda g: (g["found5"], -abs(g["max_distance"] - config["max_distance"])))
    lines.append(f"Grid: best {best['found5']}/{n} found in the first 5 at max distance {best['max_distance']:g}, "
                 f"min similarity {best['min_similarity']:g} (picked on these same questions)")
    return lines


def format_run(run: dict, zone) -> str:
    """A run's numbers as Markdown (no text from the golden set: the report and the CLI print this)."""
    when = run["requested_at"].astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")
    config = run["config"]
    models = config.get("models", {})
    lines = [f"Evaluation run {run['id']} — {run['status']}, requested {when}", ""]
    if models:
        lines.append(f"ASR: {models['asr']}, gain ≤ {config['max_gain_db']:g} dB, VAD threshold "
                     f"{config['vad_threshold']:g}, live rules: {', '.join(config['rules']) or 'none'}")
    else:
        lines.append("ASR: not started yet (the worker records its settings when it starts the run)")
    if run["status"] != "done":
        lines.append(f"Progress: {run.get('heard', 0)}/{run.get('clips', 0)} clips"
                     + (f"; error: {run['error']}" if run.get("error") else ""))
        return "\n".join(lines)
    results = run["results"]
    lines += ["", *_audio_lines(results["asr"]), "", "Captions (owner's scores, 1–5):"]
    for model, row in results["captions"].items():
        lines.append(f"- {model}: {row['scored']} scored, mean {row['mean']:.2f}, ≥ 4: {row['good']}; sai số người "
                     f"{row['wrong_people']}, bịa đồ vật {row['invented_objects']}, sai hoạt động "
                     f"{row['wrong_activity']}")
    if not results["captions"]:
        lines.append("- none scored")
    lines += ["", *_recall_lines(results["recall"], config)]
    return "\n".join(lines)


def _points(a: float | None, b: float | None) -> str:
    return "—" if a is None or b is None else f"{100 * (b - a):+.1f} points"


def format_compare(first: dict, second: dict) -> str:
    """Two finished runs side by side (live rules): per language, the no-speech check and hit@5 — the difference
    between two runs of one configuration is the noise floor a tuning change must beat (Task 13)."""
    if first["status"] != "done" or second["status"] != "done":
        return "Both runs must be finished."
    a, b = first["results"], second["results"]
    lines = [f"Run {first['id']} → run {second['id']} (live rules)", "", "| | first | second | difference |",
             "|---|---|---|---|"]
    for lang in ("vi", "ko", "en"):
        x = a["asr"]["variants"]["live"]["languages"].get(lang)
        y = b["asr"]["variants"]["live"]["languages"].get(lang)
        if x is None and y is None:
            continue
        metric = (x or y)["metric"].upper()
        lines.append(f"| {lang} {metric} | {pct(x and x['score'])} | {pct(y and y['score'])} | "
                     f"{_points(x and x['score'], y and y['score'])} |")
    x, y = a["asr"]["variants"]["live"]["no_speech"], b["asr"]["variants"]["live"]["no_speech"]
    lines.append(f"| no speech: clips with invented text | {x['invented']}/{x['clips']} | {y['invented']}/{y['clips']} "
                 f"| {y['invented'] - x['invented']:+d} |")
    x, y = a["recall"], b["recall"]
    if "found5" in x and "found5" in y:
        lines.append(f"| hit@5: questions found | {x['found5']}/{x['questions']} | {y['found5']}/{y['questions']} | "
                     f"{y['found5'] - x['found5']:+d} |")
    return "\n".join(lines)


async def rules_impact(pool: AsyncConnectionPool, since: dt.datetime) -> dict:
    """What each rule would drop from the live transcripts written since ``since`` (read only, numbers only). The
    stored segments already passed the P2a filters; they are grouped back into speech regions — consecutive segments
    of one recording with one language and language probability — and each rule is applied as the live stage would.
    Segments stored before P2c have no language probability: ``short_foreign`` cannot judge them (counted apart)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT item_id, lang, lang_prob, text, avg_logprob, no_speech_prob FROM transcript_segment"
            " WHERE created_at >= %s ORDER BY item_id, start_ts, id",
            (since,),
        )
        rows = await cur.fetchall()
    regions: list[tuple[str, asr.RegionResult]] = []
    for row in rows:
        segment = asr.SegmentResult(0.0, 0.0, row["text"], row["avg_logprob"] or 0.0, row["no_speech_prob"] or 0.0,
                                    1.0)
        last = regions[-1] if regions else None
        if last and last[0] == row["item_id"] and last[1].lang == row["lang"] \
                and last[1].lang_prob == (1.0 if row["lang_prob"] is None else row["lang_prob"]):
            last[1].segments.append(segment)
        else:
            prob = 1.0 if row["lang_prob"] is None else row["lang_prob"]  # unknown: short_foreign never fires
            regions.append((row["item_id"], asr.RegionResult(row["lang"], prob, [segment])))
    out = {"since": since.isoformat(), "segments": len(rows), "recordings": len({r["item_id"] for r in rows}),
           "no_probability": sum(1 for r in rows if r["lang_prob"] is None), "rules": {}}
    for name, rules in (*((rule, [rule]) for rule in ASR_RULES), ("all", list(ASR_RULES))):
        segments = words = 0
        recordings: set[str] = set()
        by_lang: dict[str, int] = {}
        for item_id, region in regions:
            kept = {id(s) for s in asr.kept_segments(region, frozenset(rules))}
            gone = [s for s in region.segments if id(s) not in kept]
            if gone:
                segments += len(gone)
                words += sum(len(s.text.split()) for s in gone)
                recordings.add(item_id)
                by_lang[region.lang] = by_lang.get(region.lang, 0) + len(gone)
        out["rules"][name] = {"segments": segments, "words": words, "recordings": len(recordings),
                              "by_lang": dict(sorted(by_lang.items()))}
    return out


def format_rules_impact(found: dict) -> str:
    lines = [f"Live transcripts since {found['since']}: {found['segments']} segments in {found['recordings']} "
             f"recordings ({found['no_probability']} without a language probability)", "",
             "| Rule | Segments it would drop | Words | Recordings | By language |", "|---|---|---|---|---|"]
    for name, row in found["rules"].items():
        langs = ", ".join(f"{lang} {n}" for lang, n in row["by_lang"].items()) or "—"
        lines.append(f"| {name} | {row['segments']} | {row['words']} | {row['recordings']} | {langs} |")
    return "\n".join(lines)

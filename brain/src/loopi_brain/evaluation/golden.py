"""The owner's golden set of clips and keyframes (spec §13, D21): what to propose, the copies, the labels.

A golden clip is one whole audio recording (the agent cuts them at 60 s) that the owner listened to on the Đánh giá
page and wrote down by hand — the reference transcript — or marked as having no speech at all (silence, TV, room
noise: the check for invented text; useful only where Whisper wrote something, so such recordings are proposed in a
group of their own). Only recordings made since ``Settings.eval_audio_since`` (the device's +20 dB gain, D19) are
proposed, so the set holds one capture condition. A golden keyframe carries the owner's 1–5 score of its caption, per caption
model, and three ticks ("sai số người", "bịa đồ vật", "sai hoạt động").

Each sample gets its own copy under LOOPI_EVAL_DIR, so it outlives retention (D20 deletes raw audio after 7 days):
``audio/<id>.opus`` (the raw recording, byte for byte: the runner hears what the live stage heard),
``audio/<id>.view.opus`` (the same at a level you can hear, D19) and ``keyframes/<id>.jpg``. The row is committed
before its files are written, so ``sweep`` — which deletes every file whose sample has no row — never takes a copy
being made; "Quên khoảng thời gian" deletes the samples of its range (``forget_rows``) and then sweeps. Logs carry
ids and counts, never a reference or a caption.
"""
from __future__ import annotations

import datetime as dt
import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import anyio
import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .. import devices, media
from ..config import Settings

log = logging.getLogger(__name__)
LANGUAGES = ("vi", "ko", "en")
NO_SPEECH = "none"  # clips marked "không có lời nói" whose live transcript had text (they test the rules)
SILENT = "silent"  # clips marked "không có lời nói" where Whisper wrote nothing either (they test nothing)
NOISE = "noise"  # the proposal group "Whisper wrote something, but it may be noise"
# A recording is proposed as maybe-noise when its whole transcript has at most this many words, or its segments'
# average log probability is below this (P2a: Whisper's inventions on room noise sit at about -0.8 to -0.9).
NOISE_WORDS = 4
NOISE_LOGPROB = -0.7
# The page's target: §13's 20 clips with speech or without, about this many of each kind; the no-speech clips need
# text from Whisper to show what a rule removes (a scarce language is reported, never faked).
AUDIO_TARGET = 22
AUDIO_SUGGESTED = {"vi": 6, "ko": 6, "en": 4, NO_SPEECH: 6}
CAPTION_TARGET = 30
PER_PART = 2  # proposals per detected language and part of the day
DAY_PARTS = ("đêm (0–6 giờ)", "sáng (6–12 giờ)", "chiều (12–18 giờ)", "tối (18–24 giờ)")
CAPTIONS_SHOWN = 12
REFERENCE_MAX = 5000  # characters of a reference transcript
AUDIO_DIR = "audio"
KEYFRAME_DIR = "keyframes"


class GoldenError(Exception):
    """A sample cannot be made or changed: its recording or keyframe is gone, expired, not analysed, or already a
    sample; or the table refused it. The message is fixed text: never a reference, a caption or PostgreSQL's
    "Failing row contains (...)"."""


LABEL_REFUSED = "the label could not be saved"


@dataclass(frozen=True)
class AudioLabel:
    """What the owner says of a clip. It holds what the table's ``eval_audio_label`` CHECK accepts (and nothing a
    database error could repeat): no speech means no language and no reference, speech a language of the household
    and a reference of at most REFERENCE_MAX characters. ValueError, with a fixed message, otherwise."""

    lang: str | None
    no_speech: bool
    reference: str

    def __post_init__(self) -> None:
        if self.no_speech:
            if self.lang is not None or self.reference:
                raise ValueError("a clip without speech has no language and no reference")
        elif self.lang not in LANGUAGES:
            raise ValueError(f"the language must be one of {', '.join(LANGUAGES)}")
        elif not self.reference.strip():
            raise ValueError("a clip with speech needs its reference transcript")
        if len(self.reference) > REFERENCE_MAX:
            raise ValueError(f"a reference is at most {REFERENCE_MAX} characters")


def clean_text(text: str, limit: int = REFERENCE_MAX) -> str:
    """Text as the owner typed it, without control characters (PostgreSQL text cannot hold NUL) and with its
    whitespace collapsed; at most ``limit`` characters."""
    return " ".join("".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in text).split())[:limit]


def audio_label(lang: str, no_speech: bool, reference: str) -> AudioLabel:
    """The label of a clip: "không có lời nói", or a language of the household and a non-empty reference.
    ValueError otherwise."""
    if no_speech:
        return AudioLabel(None, True, "")
    if lang not in LANGUAGES:
        raise ValueError(f"the language must be one of {', '.join(LANGUAGES)}")
    reference = clean_text(reference)
    if not reference:
        raise ValueError("a clip with speech needs its reference transcript")
    return AudioLabel(lang, False, reference)


def check_score(score: int) -> int:
    if not 1 <= score <= 5:
        raise ValueError("a caption score is 1 to 5")
    return score


def audio_paths(sample_id: str) -> tuple[str, str]:
    """(byte copy, playback copy) of a clip, relative to the eval dir."""
    return f"{AUDIO_DIR}/{sample_id}.opus", f"{AUDIO_DIR}/{sample_id}.view.opus"


def keyframe_path(sample_id: str) -> str:
    return f"{KEYFRAME_DIR}/{sample_id}.jpg"


def _copy(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` through a temporary name (the copy appears whole or not at all)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.name.split('.')[0]}.{uuid.uuid4().hex}.part")
    try:
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


async def sample_exists(pool: AsyncConnectionPool, table: str, sample_id: str) -> bool:
    """Is there a golden clip (``table`` "eval_audio") or keyframe ("eval_caption") with this id?"""
    if table not in ("eval_audio", "eval_caption"):
        raise ValueError(f"not a golden-set table: {table}")
    async with pool.connection() as conn:
        cur = await conn.execute(f"SELECT 1 FROM {table} WHERE id = %s", (sample_id,))
        return await cur.fetchone() is not None


async def _drop(pool: AsyncConnectionPool, settings: Settings, table: str, sample_id: str, files: list[str]) -> None:
    async with pool.connection() as conn:
        await conn.execute(f"DELETE FROM {table} WHERE id = %s", (sample_id,))
    await anyio.to_thread.run_sync(media.remove_files, settings.eval_dir, files)


# ---- clips -------------------------------------------------------------------------------------------------------

# The language Whisper heard most (by duration) in recording ``i``; the same expression proposes a recording and
# shows it again when the owner's form comes back with an error.
DOMINANT_LANG = (
    "(SELECT s.lang FROM transcript_segment s WHERE s.item_id = i.id GROUP BY s.lang"
    " ORDER BY sum(extract(epoch FROM s.end_ts - s.start_ts)) DESC, s.lang LIMIT 1)"
)
_AUDIO_CANDIDATES = f"""
WITH c AS (
    SELECT i.id, i.started_at,
           {DOMINANT_LANG} AS lang,
           t.words, t.logprob,
           extract(hour FROM i.started_at AT TIME ZONE %(tz)s)::int / 6 AS part
      FROM item i
      LEFT JOIN LATERAL (
          SELECT sum(array_length(regexp_split_to_array(btrim(s.text), '\\s+'), 1)) AS words,
                 avg(s.avg_logprob) AS logprob
            FROM transcript_segment s WHERE s.item_id = i.id
      ) t ON true
     WHERE i.kind = 'audio' AND i.analysis_state = 'done' AND i.raw_deleted_at IS NULL
       AND i.started_at >= %(since)s
       AND NOT EXISTS (SELECT 1 FROM eval_audio e WHERE e.item_id = i.id)
), s AS (
    SELECT *, CASE WHEN lang IS NULL THEN ''
                   WHEN words <= %(noise_words)s OR logprob < %(noise_logprob)s THEN 'noise'
                   ELSE lang END AS stratum
      FROM c
), r AS (
    SELECT *, count(*) OVER (PARTITION BY stratum) AS available,
           row_number() OVER (PARTITION BY stratum, part ORDER BY md5(id || %(seed)s), id) AS n
      FROM s
)
SELECT id, started_at, lang, stratum, part, available FROM r WHERE n <= %(per_part)s ORDER BY stratum, started_at
"""


def day_part(started_at: dt.datetime, zone) -> str:
    """The part of the local day (``DAY_PARTS``) a recording started in."""
    return DAY_PARTS[started_at.astimezone(zone).hour // 6]


async def transcripts(pool: AsyncConnectionPool, item_ids: list[str]) -> dict[str, str]:
    """The current transcript of each recording: its segments in time order, joined by spaces."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT item_id, string_agg(text, ' ' ORDER BY start_ts) AS text FROM transcript_segment"
            " WHERE item_id = ANY(%s) GROUP BY item_id",
            (item_ids,),
        )
        return {r["item_id"]: r["text"] for r in await cur.fetchall()}


async def audio_candidates(
    pool: AsyncConnectionPool, zone, seed: int = 0, since: dt.datetime | None = None
) -> dict[str, dict]:
    """Recordings to propose, in groups: by the language Whisper heard most in them; NOISE when what it wrote is
    short or unsure (maybe invented on noise: the no-speech check needs these); '' when it heard nothing (maybe
    speech it missed). Up to PER_PART per group and part of the day (local time), spread over the days by a stable
    pseudo-random order that ``seed`` changes. Only finished recordings made since ``since``, whose raw audio is still
    kept and that are not golden clips yet; each with its current transcript and detected language. Every group is
    listed, with how many recordings are available."""
    params = {"tz": str(zone), "seed": str(seed), "per_part": PER_PART, "noise_words": NOISE_WORDS,
              "noise_logprob": NOISE_LOGPROB, "since": since or dt.datetime.min.replace(tzinfo=dt.UTC)}
    async with pool.connection() as conn:
        cur = await conn.execute(_AUDIO_CANDIDATES, params)
        rows = await cur.fetchall()
    texts = await transcripts(pool, [r["id"] for r in rows])
    strata: dict[str, dict] = {group: {"available": 0, "items": []} for group in (*LANGUAGES, NOISE, "")}
    for row in rows:
        stratum = strata.setdefault(row["stratum"], {"available": 0, "items": []})
        stratum["available"] = row["available"]
        stratum["items"].append({"id": row["id"], "started_at": row["started_at"], "part": DAY_PARTS[row["part"]],
                                 "lang": row["lang"], "transcript": texts.get(row["id"], "")})
    return strata


async def add_audio(
    pool: AsyncConnectionPool, settings: Settings, item_id: str, label: AudioLabel, prefilled: bool = True
) -> str:
    """Make a finished recording whose raw audio is kept a golden clip with ``label``; return the sample id.
    ``prefilled``: the owner edited Whisper's text (False: typed after listening, without seeing it). The clip also
    keeps whether its live transcript had text. GoldenError when it cannot be (gone, expired, not analysed, already a
    clip, or its file cannot be copied)."""
    sample_id = devices.new_id()
    copy, view = audio_paths(sample_id)
    had_text = bool((await transcripts(pool, [item_id])).get(item_id))
    try:
        async with pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT raw_path FROM item WHERE id = %s AND kind = 'audio' AND analysis_state = 'done'"
                " AND raw_deleted_at IS NULL FOR SHARE",
                (item_id,),
            )
            item = await cur.fetchone()
            if item is None:
                raise GoldenError("this recording is gone, expired or not analysed yet")
            await conn.execute(
                "INSERT INTO eval_audio (id, item_id, path, lang, no_speech, reference, had_text, prefilled)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (sample_id, item_id, copy, label.lang, label.no_speech, label.reference, had_text, prefilled),
            )
    except psycopg.errors.UniqueViolation as exc:
        raise GoldenError("this recording is already a golden clip") from exc
    except psycopg.IntegrityError:  # its message would hold the failing row, the reference included
        raise GoldenError(LABEL_REFUSED) from None
    try:
        src = media.inside(settings.media_root, item["raw_path"])
        await anyio.to_thread.run_sync(_copy, src, settings.eval_dir / copy)
    except (OSError, ValueError) as exc:
        log.warning("golden clip %s: the recording %s could not be copied: %s", sample_id, item_id, exc)
        await _drop(pool, settings, "eval_audio", sample_id, [copy])
        raise GoldenError("the recording's file could not be copied") from exc
    try:
        await media.normalise_audio(settings.eval_dir / copy, settings.eval_dir / view)
    except Exception:  # the page plays the byte copy instead
        log.warning("golden clip %s: no playback copy; the page plays it at its raw level", sample_id, exc_info=True)
    if not await sample_exists(pool, "eval_audio", sample_id):  # forgotten while the files were written
        await anyio.to_thread.run_sync(media.remove_files, settings.eval_dir, [copy, view])
        raise GoldenError("this recording was forgotten meanwhile")
    log.info("golden clip %s added from recording %s", sample_id, item_id)
    return sample_id


async def update_audio(pool: AsyncConnectionPool, sample_id: str, label: AudioLabel) -> bool:
    """Replace a clip's label; False when there is no such clip. GoldenError when the table refuses the label."""
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE eval_audio SET lang = %s, no_speech = %s, reference = %s, updated_at = now() WHERE id = %s",
                (label.lang, label.no_speech, label.reference, sample_id),
            )
            return cur.rowcount == 1
    except psycopg.IntegrityError:  # its message would hold the failing row, the reference included
        raise GoldenError(LABEL_REFUSED) from None


async def audio_samples(pool: AsyncConnectionPool) -> list[dict]:
    """Every golden clip, oldest recording first: its label and when it was recorded."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT e.id, e.item_id, e.lang, e.no_speech, e.reference, e.had_text, e.prefilled, i.started_at"
            " FROM eval_audio e"
            " JOIN item i ON i.id = e.item_id ORDER BY i.started_at, e.id"
        )
        return await cur.fetchall()


async def delete_audio(pool: AsyncConnectionPool, settings: Settings, sample_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("DELETE FROM eval_audio WHERE id = %s", (sample_id,))
        found = cur.rowcount == 1
    await anyio.to_thread.run_sync(media.remove_files, settings.eval_dir, list(audio_paths(sample_id)))
    return found


# ---- keyframes ---------------------------------------------------------------------------------------------------

_CAPTION_CANDIDATES = """
WITH c AS (
    SELECT k.id, k.ts, k.caption, k.caption_model,
           (k.ts AT TIME ZONE %(tz)s)::date AS day, extract(hour FROM k.ts AT TIME ZONE %(tz)s)::int / 3 AS block
      FROM keyframe k
     WHERE k.caption IS NOT NULL AND NOT EXISTS (SELECT 1 FROM eval_caption e WHERE e.keyframe_id = k.id)
), r AS (
    SELECT *, row_number() OVER (PARTITION BY day, block ORDER BY md5(id || %(seed)s), id) AS n FROM c
)
SELECT id, ts, caption, caption_model FROM r WHERE n = 1 ORDER BY md5(id || %(seed)s), id LIMIT %(limit)s
"""


async def caption_candidates(pool: AsyncConnectionPool, zone, seed: int = 0, limit: int = CAPTIONS_SHOWN) -> list[dict]:
    """Captioned keyframes that are not golden yet, spread over days and hours: at most one per local day and
    3-hour block, ``limit`` of those in a stable pseudo-random order (``seed``), shown in time order."""
    async with pool.connection() as conn:
        cur = await conn.execute(_CAPTION_CANDIDATES, {"tz": str(zone), "seed": str(seed), "limit": limit})
        rows = await cur.fetchall()
    return sorted(rows, key=lambda r: (r["ts"], r["id"]))


async def add_caption(
    pool: AsyncConnectionPool, settings: Settings, keyframe_id: str, score: int, flags: set[str]
) -> str:
    """Make a captioned keyframe a golden one with the owner's score of its current caption; return the sample id.
    ``flags``: any of ``wrong_people``, ``invented_objects``, ``wrong_activity``. GoldenError when it cannot be."""
    check_score(score)
    sample_id = devices.new_id()
    copy = keyframe_path(sample_id)
    try:
        async with pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT path, caption, caption_model FROM keyframe WHERE id = %s AND caption IS NOT NULL FOR SHARE",
                (keyframe_id,),
            )
            kf = await cur.fetchone()
            if kf is None:
                raise GoldenError("this keyframe is gone or has no caption yet")
            await conn.execute("INSERT INTO eval_caption (id, keyframe_id, path) VALUES (%s, %s, %s)",
                               (sample_id, keyframe_id, copy))
            await _store_score(conn, sample_id, kf["caption_model"], kf["caption"], score, flags)
    except psycopg.errors.UniqueViolation as exc:
        raise GoldenError("this keyframe is already a golden one") from exc
    try:
        src = media.inside(settings.media_root, kf["path"])
        await anyio.to_thread.run_sync(_copy, src, settings.eval_dir / copy)
    except (OSError, ValueError) as exc:
        log.warning("golden keyframe %s: keyframe %s could not be copied: %s", sample_id, keyframe_id, exc)
        await _drop(pool, settings, "eval_caption", sample_id, [copy])
        raise GoldenError("the keyframe's picture could not be copied") from exc
    if not await sample_exists(pool, "eval_caption", sample_id):
        await anyio.to_thread.run_sync(media.remove_files, settings.eval_dir, [copy])
        raise GoldenError("this keyframe was forgotten meanwhile")
    log.info("golden keyframe %s added from keyframe %s", sample_id, keyframe_id)
    return sample_id


FLAGS = ("wrong_people", "invented_objects", "wrong_activity")


async def _store_score(conn: AsyncConnection, sample_id: str, model: str, caption: dict, score: int,
                       flags: set[str]) -> None:
    unknown = set(flags) - set(FLAGS)
    if unknown:
        raise ValueError(f"unknown caption flags: {', '.join(sorted(unknown))}")
    await conn.execute(
        "INSERT INTO eval_caption_score (sample_id, model_version, caption, score, wrong_people, invented_objects,"
        " wrong_activity) VALUES (%s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (sample_id, model_version) DO UPDATE SET caption = EXCLUDED.caption, score = EXCLUDED.score,"
        " wrong_people = EXCLUDED.wrong_people, invented_objects = EXCLUDED.invented_objects,"
        " wrong_activity = EXCLUDED.wrong_activity, scored_at = now()",
        (sample_id, model, Jsonb(caption), score, *(f in flags for f in FLAGS)),
    )


async def score_caption(pool: AsyncConnectionPool, sample_id: str, score: int, flags: set[str]) -> bool:
    """Score the golden keyframe's current caption (its model's score is replaced); False when the sample is gone
    or its keyframe has no caption right now (being captioned again)."""
    check_score(score)
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "SELECT k.caption, k.caption_model FROM eval_caption e JOIN keyframe k ON k.id = e.keyframe_id"
            " WHERE e.id = %s AND k.caption IS NOT NULL FOR SHARE OF k",
            (sample_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        await _store_score(conn, sample_id, row["caption_model"], row["caption"], score, flags)
    return True


async def caption_samples(pool: AsyncConnectionPool) -> list[dict]:
    """Every golden keyframe in time order: its current caption and model, its scores (one per caption model) and
    whether the current caption still waits for a score."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT e.id, e.keyframe_id, k.ts, k.caption, k.caption_model FROM eval_caption e"
            " JOIN keyframe k ON k.id = e.keyframe_id ORDER BY k.ts, e.id"
        )
        samples = await cur.fetchall()
        cur = await conn.execute("SELECT * FROM eval_caption_score ORDER BY scored_at")
        scores = await cur.fetchall()
    by_sample: dict[str, list[dict]] = {}
    for score in scores:
        by_sample.setdefault(score["sample_id"], []).append(score)
    return [
        {**s, "scores": by_sample.get(s["id"], []),
         "needs_score": s["caption"] is not None
         and all(x["model_version"] != s["caption_model"] for x in by_sample.get(s["id"], []))}
        for s in samples
    ]


async def delete_caption(pool: AsyncConnectionPool, settings: Settings, sample_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("DELETE FROM eval_caption WHERE id = %s", (sample_id,))
        found = cur.rowcount == 1
    await anyio.to_thread.run_sync(media.remove_files, settings.eval_dir, [keyframe_path(sample_id)])
    return found


# ---- the whole set ------------------------------------------------------------------------------------------------

async def progress(pool: AsyncConnectionPool) -> dict:
    """How far the labelling is: clips per language, NO_SPEECH (no speech, but Whisper wrote text) and SILENT (no
    speech and no text), golden keyframes and how many of them have a score for their current caption."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT CASE WHEN NOT no_speech THEN lang WHEN had_text THEN %s ELSE %s END AS lang, count(*) AS n"
            " FROM eval_audio GROUP BY 1",
            (NO_SPEECH, SILENT),
        )
        audio = {lang: 0 for lang in (*AUDIO_SUGGESTED, SILENT)} | {r["lang"]: r["n"] for r in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT count(*) AS n, count(*) FILTER (WHERE EXISTS (SELECT 1 FROM eval_caption_score s"
            " WHERE s.sample_id = e.id AND s.model_version = k.caption_model)) AS scored"
            " FROM eval_caption e JOIN keyframe k ON k.id = e.keyframe_id"
        )
        captions = await cur.fetchone()
    return {"audio": audio, "audio_total": sum(audio.values()), "captions": captions["n"],
            "captions_scored": captions["scored"]}


async def forget_rows(conn: AsyncConnection, item_ids: list[str], keyframe_ids: list[str], moment_ids: list[str],
                      delete: bool) -> int:
    """The golden samples "Quên khoảng thời gian" takes with it: clips of its recordings, golden keyframes of its
    keyframes, and the correct moments of questions that point into it. Counted, and deleted when ``delete`` (inside
    the caller's transaction); the copies go afterwards, by ``sweep``."""
    params = {"items": item_ids, "keyframes": keyframe_ids, "moments": moment_ids}
    statements = (
        ("eval_audio", "item_id = ANY(%(items)s)"),
        ("eval_caption", "keyframe_id = ANY(%(keyframes)s)"),
        ("eval_answer", "item_id = ANY(%(items)s) OR keyframe_id = ANY(%(keyframes)s) OR moment_id = ANY(%(moments)s)"),
    )
    total = 0
    for table, where in statements:
        if delete:
            cur = await conn.execute(f"DELETE FROM {table} WHERE {where}", params)
            total += cur.rowcount
        else:
            cur = await conn.execute(f"SELECT count(*) AS n FROM {table} WHERE {where}", params)
            total += (await cur.fetchone())["n"]
    return total


def _files(eval_dir: Path) -> list[tuple[str, str, str]]:
    """(folder, sample id, path relative to the eval dir) of every file in the copy folders."""
    out = []
    for folder in (AUDIO_DIR, KEYFRAME_DIR):
        directory = eval_dir / folder
        if directory.is_dir():
            out.extend((folder, entry.name.split(".")[0], str(PurePosixPath(folder, entry.name)))
                       for entry in directory.iterdir() if entry.is_file() or entry.is_symlink())
    return out


async def sweep(pool: AsyncConnectionPool, eval_dir: Path) -> tuple[int, list[str]]:
    """Delete every copy whose sample has no row (forgotten or deleted); return (how many went, the copies that
    could not be deleted, relative to the eval dir: they hold sample ids only). A half-made copy (``<id>.<random>.part``,
    left by a crash) goes only once its row is gone: while the row exists the copy may still be being written.
    Runs after "Quên khoảng thời gian" (which reports the failures) and every night at 03:00 (which retries them)."""
    files = await anyio.to_thread.run_sync(_files, eval_dir)
    if not files:
        return 0, []
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT id FROM eval_audio")
        kept = {(AUDIO_DIR, r["id"]) for r in await cur.fetchall()}
        cur = await conn.execute("SELECT id FROM eval_caption")
        kept |= {(KEYFRAME_DIR, r["id"]) for r in await cur.fetchall()}
    stale = [rel for folder, sample_id, rel in files if (folder, sample_id) not in kept]
    deleted, failed = await anyio.to_thread.run_sync(media.remove_files, eval_dir, stale)
    if stale:
        log.info("golden set: %d copies without a sample deleted, %d could not be", deleted, len(failed))
    return deleted, failed

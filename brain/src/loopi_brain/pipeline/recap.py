"""The end-of-day recap and its commitments (spec §10.2; §9.2 "23:30: recap cuối ngày, trích việc cần làm").

The recap model reads the day's chapters, marked moments and the most substantial speech lines, and answers
with JSON: the main course of the day, memorable moments, and the things someone said they must do or
promised, each citing the reference of the line it comes from. The brain writes the Vietnamese markdown
(three fixed sections) and stores each commitment whose reference is a real moment of that day.

A recap is written again when the day changed after it: today's at 23:30 if it was written earlier (e.g. by
"Tạo lại recap"), an earlier day's once its chapters are settled again after a change (late uploads, a
backlog, summaries that came later) — at most one more run per change.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from procrastinate import JobContext
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, field_validator

from .. import devices
from ..days import day_bounds_utc, hm, local_now
from ..llm import ASR_CAVEAT, clip, clipped, marker_line, parse_answer, spread
from ..providers.base import ProviderUnavailable
from ..providers.router import Router
from . import jobs
from .context import PipelineContext, get_context
from .jobs import job_app
from .moments import marker_note
from .state import clear_day_error, forgotten, lock_day, record_day_error

log = logging.getLogger(__name__)
RECAP_TIME = dt.time(23, 30)  # local time today's recap is written (spec §9.2)
# Output stays small: on the 4-core CPU qwen3:8b writes ≈ 5–7 tokens/s, so 1000 tokens are ≈ 3 minutes.
ITEM_MAX = 200
MAX_EVENTS = 8
MAX_MEMORABLE = 5
MAX_COMMITMENTS = 8
RECAP_MAX_TOKENS = 1000
RETRY_TEMPERATURE = 0.3
MAX_SPEECH_LINES = 60  # the longest speech lines of the day ("Thank you." and other short noise go first)
CHAPTER_BUDGET = 3500
MARKER_BUDGET = 1000
PROMPT_BUDGET = 9000
MIN_SUMMARY = 40
REF_ROOM = "[S99] "  # the longest reference a line can carry (at most MAX_SPEECH_LINES lines)
STATUSES = ("open", "done", "dismissed")
HEADINGS = ("Diễn biến chính", "Khoảnh khắc đáng nhớ", "Việc cần làm hoặc đã hứa")
NOTHING = "Không có."
RECAP_SYSTEM = (
    "You write the end-of-day recap of a personal diary, in Vietnamese, from what a still camera and a microphone "
    "in a home recorded during one day. Chapter lines are earlier Vietnamese summaries of parts of the day. "
    f"{ASR_CAVEAT} Use only what the material supports; never invent people, names or events. Answer with JSON: "
    "main_events = 3 to 8 short Vietnamese bullet points on the main course of the day, in time order; "
    "memorable = up to 5 short Vietnamese bullet points on memorable moments (marked moments, notable events, "
    "important or funny things said); commitments = the things someone said they must do or promised to do "
    "(a task, an appointment, a purchase), each with text = the task in Vietnamese and source = the reference "
    "in square brackets of the speech or marked line it comes from, such as \"S3\" or \"M1\". Leave commitments "
    "empty when nobody said such a thing."
)
RECAP_SCHEMA = {
    "type": "object",
    "properties": {
        "main_events": {"type": "array", "items": {"type": "string", "maxLength": ITEM_MAX}, "maxItems": MAX_EVENTS},
        "memorable": {"type": "array", "items": {"type": "string", "maxLength": ITEM_MAX}, "maxItems": MAX_MEMORABLE},
        "commitments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string", "maxLength": ITEM_MAX}, "source": {"type": "string"}},
                "required": ["text", "source"],
            },
            "maxItems": MAX_COMMITMENTS,
        },
    },
    "required": ["main_events", "memorable", "commitments"],
}


class CommitmentAnswer(BaseModel):
    text: clipped(ITEM_MAX)
    source: str


class RecapAnswer(BaseModel):
    """Over-long text and lists are cut, not rejected; blank entries are dropped."""

    main_events: list[clipped(ITEM_MAX)]
    memorable: list[clipped(ITEM_MAX)]
    commitments: list[CommitmentAnswer]

    @field_validator("main_events", "memorable")
    @classmethod
    def _non_blank(cls, value: list[str]) -> list[str]:
        return [v for v in value if v.strip()]

    @field_validator("main_events")
    @classmethod
    def _cap_events(cls, value: list[str]) -> list[str]:
        return value[:MAX_EVENTS]

    @field_validator("memorable")
    @classmethod
    def _cap_memorable(cls, value: list[str]) -> list[str]:
        return value[:MAX_MEMORABLE]

    @field_validator("commitments")
    @classmethod
    def _cap_commitments(cls, value: list[CommitmentAnswer]) -> list[CommitmentAnswer]:
        return [c for c in value if c.text.strip()][:MAX_COMMITMENTS]


@dataclass
class Material:
    """What the recap model reads; ``refs`` maps "S3"/"M1" to (moment id, its time)."""

    chapters: list[str] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    speech: list[str] = field(default_factory=list)
    refs: dict[str, tuple[str, dt.datetime]] = field(default_factory=dict)


def _chapter_lines(rows: list[dict], zone) -> list[str]:
    """Every chapter of the day, one line each; summaries are clipped to share CHAPTER_BUDGET evenly, so a day
    with many chapters still shows its last one (at least MIN_SUMMARY characters of each summary stay)."""
    each = CHAPTER_BUDGET // max(len(rows), 1) - 1  # characters per line, its newline apart
    lines = []
    for c in rows:
        head = f"{hm(c['ts_start'], zone)}–{hm(c['ts_end'], zone)}"
        if not c["title"]:
            lines.append(f"{head} (no summary yet)")
            continue
        head = f"{head} {clip(c['title'], 80)}: "
        lines.append(head + clip(c["summary"], min(600, max(each - len(head), MIN_SUMMARY))))
    return lines


async def recap_material(pool: AsyncConnectionPool, day: dt.date, zone) -> Material | None:
    """What the model reads, within PROMPT_BUDGET characters, whatever the hour: every chapter (summaries clipped to
    share CHAPTER_BUDGET), marked moments spread over the day, and the longest speech lines of the whole day
    (picked longest first, shown in time order); None when the day has no moment at all."""
    start, end = day_bounds_utc(day, zone)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT EXISTS (SELECT 1 FROM moment WHERE ts_start >= %s AND ts_start < %s) AS any",
                                 (start, end))
        if not (await cur.fetchone())["any"]:
            return None
        cur = await conn.execute(
            "SELECT ts_start, ts_end, title, summary FROM chapter WHERE day = %s ORDER BY ts_start", (day,)
        )
        chapter_rows = await cur.fetchall()
        cur = await conn.execute(
            "SELECT m.id, m.ts_start, i.meta AS item_meta FROM moment m LEFT JOIN item i ON i.id = m.item_id"
            " WHERE m.kind = 'marker' AND m.ts_start >= %s AND m.ts_start < %s ORDER BY m.ts_start, m.id",
            (start, end),
        )
        marker_rows = await cur.fetchall()
        cur = await conn.execute(
            "SELECT id, ts_start, lang, text FROM moment"
            " WHERE kind = 'speech' AND ts_start >= %s AND ts_start < %s ORDER BY ts_start, id",
            (start, end),
        )
        speech_rows = await cur.fetchall()
    material = Material()
    material.chapters = _chapter_lines(chapter_rows, zone)
    bodies = [marker_line(hm(r["ts_start"], zone), marker_note(r["item_meta"] or {})) for r in marker_rows]
    # As many marked moments as fit MARKER_BUDGET, spread over the whole day (not the first ones of the morning).
    fit = max(1, MARKER_BUDGET // (max(map(len, bodies), default=0) + len(REF_ROOM) + 1))
    for n, (row, body) in enumerate(spread(list(zip(marker_rows, bodies, strict=True)), fit), start=1):
        material.markers.append(f"[M{n}] {body}")
        material.refs[f"M{n}"] = (row["id"], row["ts_start"])
    # The longest speech lines of the whole day, whatever the hour, while the budget lasts; shown in time order.
    budget = PROMPT_BUDGET - sum(len(line) + 1 for line in material.chapters + material.markers)
    chosen = []
    for row in sorted(speech_rows, key=lambda r: (-len(r["text"].split()), r["ts_start"], r["id"])):
        body = f"{hm(row['ts_start'], zone)} ({row['lang'] or '?'}) {clip(row['text'], 250)}"
        budget -= len(REF_ROOM) + len(body) + 1
        if len(chosen) == MAX_SPEECH_LINES or budget < 0:
            break
        chosen.append((row, body))
    for n, (row, body) in enumerate(sorted(chosen, key=lambda pair: (pair[0]["ts_start"], pair[0]["id"])), start=1):
        material.speech.append(f"[S{n}] {body}")
        material.refs[f"S{n}"] = (row["id"], row["ts_start"])
    return material


def recap_messages(material: Material, day: dt.date) -> list[dict]:
    parts = [
        f"Day: {day.isoformat()}",
        "Chapters:\n" + ("\n".join(material.chapters) or "(none yet)"),
        "Marked moments:\n" + ("\n".join(material.markers) or "(none)"),
        "Speech lines:\n" + ("\n".join(material.speech) or "(none)"),
    ]
    return [{"role": "system", "content": RECAP_SYSTEM}, {"role": "user", "content": "\n\n".join(parts)}]


def compose_markdown(answer: RecapAnswer, commitments: list[tuple[str, dt.datetime]], zone) -> str:
    """The recap in markdown: the three sections of spec §10.2, always present, in a fixed order."""

    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) if items else NOTHING

    todo = [f"{text} ({hm(ts, zone)})" for text, ts in commitments]
    bodies = (bullets(answer.main_events), bullets(answer.memorable), bullets(todo))
    return "\n\n".join(f"## {heading}\n{body}" for heading, body in zip(HEADINGS, bodies, strict=True))


def sourced(
    answer: RecapAnswer, refs: dict[str, tuple[str, dt.datetime]], day: dt.date | None = None
) -> list[tuple[str, str, dt.datetime]]:
    """(text, moment id, time) of each commitment whose source is a line of the material; others are dropped
    (logged by day and source only: what the model wrote is private speech and never goes to the logs)."""
    out, seen = [], set()
    for c in answer.commitments:
        ref = c.source.strip().strip("[]").upper()
        if ref not in refs:
            log.info("recap of %s: a commitment was dropped, unknown source %r", day, clip(c.source, 20))
            continue
        moment_id, ts = refs[ref]
        key = (moment_id, c.text.strip().lower())
        if key not in seen:
            seen.add(key)
            out.append((c.text.strip(), moment_id, ts))
    return out


async def write_recap(ctx: PipelineContext, day: dt.date, attempt: int = 0) -> bool:
    """Write (or rewrite) the recap of ``day`` and its open commitments; False if nothing was written (no data;
    the model server is down; or part of the day was forgotten while the model wrote, so its text may quote
    forgotten moments — in the last two cases the planner asks again)."""
    pool, zone = ctx.pool, ctx.settings.zone
    start, end = day_bounds_utc(day, zone)
    try:
        async with pool.connection() as conn:
            generation = await forgotten(conn, start, end)
        material = await recap_material(pool, day, zone)
        if material is None:
            async with pool.connection() as conn:
                await conn.execute("DELETE FROM day_recap WHERE day = %s", (day,))
            await clear_day_error(pool, jobs.WRITE_RECAP, day)
            return False
        try:
            result = await ctx.router.chat(
                "recap", recap_messages(material, day), json_schema=RECAP_SCHEMA,
                temperature=0.0 if attempt == 0 else RETRY_TEMPERATURE, max_tokens=RECAP_MAX_TOKENS,
            )
        except ProviderUnavailable as exc:
            log.warning("recap of %s waits: model server unavailable: %s", day, exc)
            return False
        answer = parse_answer(result.text, RecapAnswer)
        commitments = sourced(answer, material.refs, day)
        markdown = compose_markdown(answer, [(text, ts) for text, _, ts in commitments], zone)
        async with pool.connection() as conn, conn.transaction():
            await lock_day(conn, day)
            if await forgotten(conn, start, end) != generation:
                log.info("recap of %s: part of the day was forgotten while it was written; not stored", day)
                return False
            await conn.execute(
                "INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, %s, %s)"
                " ON CONFLICT (day) DO UPDATE SET markdown = EXCLUDED.markdown,"
                " model_version = EXCLUDED.model_version, generated_at = now(), stale = false",
                (day, markdown, result.model_version),
            )
            # The user's decisions stay; open commitments are replaced by the new ones.
            cur = await conn.execute(
                "SELECT lower(text) AS text, source_moment_id FROM commitment WHERE day = %s AND status <> 'open'",
                (day,),
            )
            decided = await cur.fetchall()
            await conn.execute("DELETE FROM commitment WHERE day = %s AND status = 'open'", (day,))
            for text, moment_id, _ts in commitments:
                if any(d["source_moment_id"] == moment_id or d["text"] == text.lower() for d in decided):
                    continue
                await conn.execute(
                    "INSERT INTO commitment (id, day, text, source_moment_id, model_version)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (devices.new_id(), day, text, moment_id, result.model_version),
                )
    except Exception as exc:
        await record_day_error(pool, jobs.WRITE_RECAP, day, exc)
        raise
    await clear_day_error(pool, jobs.WRITE_RECAP, day)
    log.info("recap of %s written with %d commitments", day, len(commitments))
    return True


def recap_lock(day: dt.date) -> str:
    """The queueing lock of the day's ``write_recap`` jobs."""
    return f"recap:{day.isoformat()}"


async def defer_recap(pool: AsyncConnectionPool, day: dt.date) -> int:
    """Ask for the recap of ``day`` (one waiting request per day)."""
    async with pool.connection() as conn, conn.transaction():
        return await jobs.enqueue(
            conn, jobs.WRITE_RECAP, queue="understand", lock=jobs.LLM_LOCK, priority=jobs.LLM_PRIORITY,
            queueing_lock=recap_lock(day), day=day.isoformat(),
        )


async def drop_failed_recaps(conn: AsyncConnection, days: list[dt.date]) -> None:
    """In the caller's transaction: delete the failed recap runs of ``days`` (their events go with them, cascade) and
    their stored error. A failed run left behind keeps the "Viết recap lỗi" banner up and the planner away."""
    await conn.execute(
        "DELETE FROM procrastinate_jobs WHERE status = 'failed' AND queueing_lock = ANY(%s)",
        ([recap_lock(day) for day in days],),
    )
    await conn.execute("DELETE FROM day_job_error WHERE task = %s AND day = ANY(%s)", (jobs.WRITE_RECAP, days))


async def request_rewrite(pool: AsyncConnectionPool, day: dt.date) -> int | None:
    """Ask for a new recap of ``day`` now ("Tạo lại recap", reprocessing the recap): drop the day's failed recap runs
    and their error, mark its recap out of date, then queue the request. Returns its job id, or None when it could
    not be queued: the reset is committed by then, the error is logged, and the planner writes the out-of-date recap
    by its own rules (an earlier day once settled, today at 23:30)."""
    async with pool.connection() as conn, conn.transaction():
        await drop_failed_recaps(conn, [day])
        await conn.execute("UPDATE day_recap SET stale = true WHERE day = %s", (day,))
    try:
        return await defer_recap(pool, day)  # never .defer directly: priority -10 and lock llm are set there
    except Exception:
        log.exception("could not ask for the recap of %s", day)
        return None


async def plan_recaps(pool: AsyncConnectionPool, router: Router, zone, now: dt.datetime | None = None) -> list[dt.date]:
    """Ask for the recap of every day with chapters that needs one, and return those days:
    - today from 23:30 local time, unless its recap was written at or after 23:30;
    - an earlier day once all its chapters are settled, if it has no recap, or its recap is older than the
      day's last chapter change;
    - a stale recap (reprocessing) by the same rules.
    Never a day whose recap job waits, runs or failed (the failure is on the dead-letter list, with a retry
    button), and nothing while the recap model cannot run (no job is created just to fail)."""
    local = local_now(zone, now)
    today = local.date()
    if not router.task_ready("recap", await router.health()):
        log.warning("recaps wait: the recap model is not available")
        return []
    tonight = dt.datetime.combine(today, RECAP_TIME, tzinfo=zone)
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT c.day, bool_and(c.closed) AS settled, max(c.updated_at) AS changed,
                      max(r.generated_at) AS written, bool_or(r.stale) AS stale
                 FROM chapter c LEFT JOIN day_recap r ON r.day = c.day
                WHERE c.day <= %(today)s
                  AND NOT EXISTS (SELECT 1 FROM procrastinate_jobs j
                                   WHERE j.queueing_lock = 'recap:' || c.day::text
                                     AND j.status IN ('todo', 'doing', 'failed'))
                GROUP BY c.day ORDER BY c.day""",
            {"today": today},
        )
        rows = await cur.fetchall()
    due = []
    for r in rows:
        fresh = r["written"] is not None and not r["stale"]
        if r["day"] == today:
            if local >= tonight and not (fresh and r["written"] >= tonight):
                due.append(r["day"])
        elif r["settled"] and not (fresh and r["written"] >= r["changed"]):
            due.append(r["day"])
    for day in due:
        await defer_recap(pool, day)
    return due


async def set_commitment_status(pool: AsyncConnectionPool, commitment_id: str, status: str) -> dict | None:
    """Mark a commitment open, done or dismissed; the updated row, or None if there is no such commitment."""
    if status not in STATUSES:
        raise ValueError(f"unknown commitment status {status!r}")
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE commitment SET status = %s, updated_at = now() WHERE id = %s RETURNING *", (status, commitment_id)
        )
        return await cur.fetchone()


@job_app.periodic(cron="*/15 * * * *")
@job_app.task(name=jobs.PLAN_RECAPS, queue="light", queueing_lock="plan_recaps")
async def plan_recaps_job(timestamp: int | None = None) -> None:
    ctx = get_context()
    await plan_recaps(ctx.pool, ctx.router, ctx.settings.zone)


# The task's own priority is the same as the one ``defer_recap`` passes, so a direct ``.defer`` never runs a recap
# ahead of live work either.
@job_app.task(
    name=jobs.WRITE_RECAP, queue="understand", lock=jobs.LLM_LOCK, priority=jobs.LLM_PRIORITY, retry=jobs.RETRY,
    pass_context=True,
)
async def write_recap_job(context: JobContext, day: str) -> None:
    await write_recap(get_context(), dt.date.fromisoformat(day), attempt=context.job.attempts)

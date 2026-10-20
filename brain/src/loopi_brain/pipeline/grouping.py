"""group_speech (D33, P3c design §4.3; plan rulings R2–R4, R6): decide which speech regions Whisper transcribes.

A region waits until every minute its group touches is ready: every microphone that records audio has delivered that
minute (``minute_ready``), or WAIT has passed since the minute's first audio arrived. Then ``speech.decide`` keeps the
clearest region of each group; an item whose regions are all decided gets its Whisper job (lock "asr") when it has a
chosen region, and is done otherwise. Runs at the end of every detection and every minute."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from . import jobs, speech
from .context import get_context
from .jobs import job_app
from .state import lock_pending_item, mark_item_done

log = logging.getLogger(__name__)
EXPECTED_WINDOW = dt.timedelta(seconds=45)  # a microphone heard from this recently and recording is waited for
END_SLACK = dt.timedelta(seconds=2)  # an item ending this close to a minute's end has delivered that minute
GROUP_LOCK = 330_033  # pg advisory lock: one grouping at a time (detections end concurrently)
MINUTE = dt.timedelta(minutes=1)


@dataclass
class Grouped:
    decided: int = 0
    transcribing: list[str] = field(default_factory=list)
    finished: list[str] = field(default_factory=list)


def _minute(t: dt.datetime) -> dt.datetime:
    return t.replace(second=0, microsecond=0)


def minutes_touched(span: speech.Span, slack: dt.timedelta = speech.GROUP_SLACK) -> list[dt.datetime]:
    """Every wall-clock minute the span reaches, widened by ``slack`` (speech near a minute's end needs the next)."""
    out, m = [], _minute(span.start - slack)
    while m <= span.end + slack:
        out.append(m)
        m += MINUTE
    return out


async def _expected(conn: AsyncConnection, now: dt.datetime) -> set[str]:
    cur = await conn.execute(
        "SELECT id FROM device WHERE NOT revoked AND last_heartbeat_at > %s"
        " AND last_heartbeat @> '{\"recording_audio\": true}'",
        (now - EXPECTED_WINDOW,),
    )
    return {r["id"] for r in await cur.fetchall()}


async def minute_ready(conn: AsyncConnection, minute: dt.datetime, now: dt.datetime, expected: set[str]) -> bool:
    """R3: every microphone waited for has delivered ``minute``, or WAIT has passed since its first audio arrived."""
    end = minute + MINUTE
    cur = await conn.execute(
        "SELECT device_id, ended_at, received_at, speech_detected_at, analysis_state FROM item"
        " WHERE kind = 'audio' AND started_at < %s AND ended_at > %s",
        (end, minute),
    )
    rows = await cur.fetchall()
    if not rows:
        return not expected or now >= end + speech.WAIT
    if now >= min(r["received_at"] for r in rows) + speech.WAIT:
        return True

    def detected(r) -> bool:
        return r["speech_detected_at"] is not None or r["analysis_state"] != "pending"

    for device in expected | {r["device_id"] for r in rows}:
        mine = [r for r in rows if r["device_id"] == device]
        if any(r["ended_at"] >= end - END_SLACK and detected(r) for r in mine):
            continue
        if device not in expected and mine and all(detected(r) for r in mine):
            continue
        return False
    return True


_SPANS = ("SELECT id, device_id, start_ts, end_ts, snr_db, state FROM speech_region"
          " WHERE {} ORDER BY start_ts, id")


def _span(r) -> speech.Span:
    return speech.Span(r["id"], r["device_id"], r["start_ts"], r["end_ts"], r["snr_db"], r["state"])


async def _decide(conn: AsyncConnection, now: dt.datetime) -> int:
    cur = await conn.execute(_SPANS.format("state = 'waiting'"))
    waiting = [_span(r) for r in await cur.fetchall()]
    if not waiting:
        return 0
    reach = 2 * speech.GROUP_SLACK
    cur = await conn.execute(
        _SPANS.format("state <> 'waiting' AND start_ts < %s AND end_ts > %s"),
        (max(s.end for s in waiting) + reach, min(s.start for s in waiting) - reach),
    )
    decided = [_span(r) for r in await cur.fetchall()]
    expected = await _expected(conn, now)
    ready: dict[dt.datetime, bool] = {}
    count = 0
    for group in speech.groups(waiting + decided):
        if not any(s.state == "waiting" for s in group):
            continue
        minutes = sorted({m for s in group for m in minutes_touched(s)})
        for m in minutes:
            if m not in ready:
                ready[m] = await minute_ready(conn, m, now, expected)
        if not all(ready[m] for m in minutes):
            continue
        for d in speech.decide(group):
            await conn.execute(
                "UPDATE speech_region SET state = %s, covered_by = %s, decided_at = %s WHERE id = %s AND state = 'waiting'",
                (d.state, d.covered_by, now, d.id),
            )
            count += 1
    return count


async def _settle_items(conn: AsyncConnection, result: Grouped) -> None:
    cur = await conn.execute(
        "SELECT i.id, j.task_name, j.status, bool_or(r.state = 'waiting') AS waiting,"
        " bool_or(r.state = 'chosen') AS chosen"
        " FROM item i JOIN speech_region r ON r.item_id = i.id LEFT JOIN procrastinate_jobs j ON j.id = i.analysis_job_id"
        " WHERE i.kind = 'audio' AND i.analysis_state = 'pending' AND i.speech_detected_at IS NOT NULL"
        " GROUP BY i.id, j.task_name, j.status ORDER BY i.id"
    )
    for row in await cur.fetchall():
        if row["waiting"]:
            continue
        if row["chosen"]:
            if row["task_name"] == jobs.TRANSCRIBE_AUDIO and row["status"] in ("todo", "doing", "failed"):
                continue  # queued, running, or on the dead-letter list (retried from Xử lý)
            if not await lock_pending_item(conn, row["id"]):
                continue
            job_id = await jobs.enqueue(conn, jobs.TRANSCRIBE_AUDIO, queue="audio", lock="asr",
                                        queueing_lock=f"asr:{row['id']}", item_id=row["id"])
            await conn.execute("UPDATE item SET analysis_job_id = %s WHERE id = %s", (job_id, row["id"]))
            result.transcribing.append(row["id"])
        elif await lock_pending_item(conn, row["id"]):
            await mark_item_done(conn, row["id"])  # every region was heard better elsewhere
            result.finished.append(row["id"])


async def group_speech(pool: AsyncConnectionPool, now: dt.datetime | None = None) -> Grouped:
    """One grouping pass (see the module docstring)."""
    now = now or dt.datetime.now(dt.UTC)
    result = Grouped()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (GROUP_LOCK,))
        result.decided = await _decide(conn, now)
        await _settle_items(conn, result)
    if result.decided or result.transcribing or result.finished:
        log.info("speech grouping: %d regions decided, %d items to transcribe, %d items covered elsewhere",
                 result.decided, len(result.transcribing), len(result.finished))
    return result


@job_app.periodic(cron="* * * * *")
@job_app.task(name=jobs.GROUP_SPEECH, queue="light", queueing_lock="group_speech")
async def group_speech_job(timestamp: int | None = None) -> None:
    await group_speech(get_context().pool)

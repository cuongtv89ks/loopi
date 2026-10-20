"""group_speech (D33, P3c design §4.3; plan rulings R2–R4, R6): decide which speech regions Whisper transcribes.

A region waits until every minute it touches is ready (``minute_ready``): all the audio that has arrived for it is
detected, and every microphone that records has delivered it — WAIT, counted from the minute's first arrival, excuses
only audio that has not arrived. Then ``speech.decide`` keeps the clearest region of each group. A group still running
into a minute that is not ready (a long conversation) has its regions before that minute decided now and the rest on a
later pass, against those (R4). An item whose regions are all decided gets its Whisper job (lock "asr") when it has a
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
# One grouping at a time (detections end concurrently); forget and reprocess take the same lock before they rewrite
# regions. Namespaced by name, like the other advisory locks (state.py, evaluation/runner.py).
LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('loopi:group_speech', 0))"
MINUTE = dt.timedelta(minutes=1)
MAX_ITEM = dt.timedelta(hours=1)  # an uploaded audio item is at most 3600 s: the lookups need not reach further back
MAX_REGION = dt.timedelta(seconds=60)  # a region lasts at most asr.MAX_REGION_S (20 s; asr imports this module)
# Procrastinate runs a todo job of a higher priority before older ones holding the same lock ("asr"): an item that
# ended this long ago (a day processed again, a brain catching up) is transcribed after the live ones, which keep
# their latency; it still runs before the chapter and recap jobs (-10) and the evaluation (-20).
LIVE_WINDOW = dt.timedelta(minutes=30)
BACKLOG_PRIORITY = -5


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
        " AND last_heartbeat @> '{\"recording_audio\": true}'"
        " AND NOT last_heartbeat @> '{\"mic_missing\": true}'",  # its sound keeps failing: recording_audio toggles
        (now - EXPECTED_WINDOW,),
    )
    return {r["id"] for r in await cur.fetchall()}


async def minute_ready(conn: AsyncConnection, minute: dt.datetime, now: dt.datetime, expected: set[str]) -> bool:
    """R3: ``minute`` is ready when every audio item that has arrived for it is detected (or its detection job is on
    the dead-letter list) and every microphone waited for has delivered it. WAIT after the first arrival excuses only
    a microphone that has not delivered: audio that has arrived is always waited for, however late its detection."""
    end = minute + MINUTE
    cur = await conn.execute(
        "SELECT i.device_id, i.ended_at, i.received_at, i.speech_detected_at, i.analysis_state, j.status AS job_status"
        " FROM item i LEFT JOIN procrastinate_jobs j ON j.id = i.analysis_job_id"
        " WHERE i.kind = 'audio' AND i.started_at > %s AND i.started_at < %s AND i.ended_at > %s",
        (minute - MAX_ITEM, end - END_SLACK, minute + END_SLACK),  # a segment's tail past the boundary is no overlap
    )
    rows = await cur.fetchall()
    if not rows:
        return not expected or now >= end + speech.WAIT

    def settled(r) -> bool:
        return r["speech_detected_at"] is not None or r["analysis_state"] != "pending" or r["job_status"] == "failed"

    if not all(settled(r) for r in rows):
        return False
    if now >= min(r["received_at"] for r in rows) + speech.WAIT:
        return True
    return all(any(r["device_id"] == device and r["ended_at"] >= end - END_SLACK for r in rows) for device in expected)


_SPANS = ("SELECT id, device_id, start_ts, end_ts, snr_db, state, envelope FROM speech_region"
          " WHERE {} ORDER BY start_ts, id")


def _span(r) -> speech.Span:
    envelope = tuple(r["envelope"]) if r["envelope"] else None  # NULL (stored before 014) and [] alike: no envelope
    return speech.Span(r["id"], r["device_id"], r["start_ts"], r["end_ts"], r["snr_db"], r["state"], envelope)


async def _decide(conn: AsyncConnection, now: dt.datetime) -> int:
    cur = await conn.execute(_SPANS.format("state = 'waiting'"))
    waiting = [_span(r) for r in await cur.fetchall()]
    if not waiting:
        return 0
    reach = 2 * speech.GROUP_SLACK
    first, last = min(s.start for s in waiting) - reach, max(s.end for s in waiting) + reach
    cur = await conn.execute(
        _SPANS.format("state <> 'waiting' AND start_ts > %s AND start_ts < %s AND end_ts > %s"),
        (first - MAX_REGION, last, first),
    )
    decided = [_span(r) for r in await cur.fetchall()]
    expected, known = await _expected(conn, now), {s.id for s in waiting}
    ready: dict[dt.datetime, bool] = {}
    count = 0
    for group in speech.groups(waiting + decided):
        if not any(s.state == "waiting" for s in group):
            continue
        blocked = await _first_unready(conn, group, ready, now, expected)
        if blocked is None:
            parts = [group]
        else:  # C1: the spans before the first minute not ready are decided now, the rest later against them (R4)
            before = [s for s in group if s.state != "waiting" or s.end + speech.GROUP_SLACK <= blocked]
            parts = [g for g in speech.groups(before) if any(s.state == "waiting" for s in g)]
        for part in parts:
            if not await _arrived_meanwhile(conn, part, known):
                count += await _apply(conn, part, now)
    return count


async def _arrived_meanwhile(conn: AsyncConnection, part: list[speech.Span], known: set[str]) -> bool:
    """M1: a detection committing after the pass read the waiting spans can make a minute ready while its regions are
    missing here; a part near such a region is left to the next pass, which decides it with them."""
    spans = [s for s in part if s.state == "waiting"]
    reach = 2 * speech.GROUP_SLACK
    lo, hi = min(s.start for s in spans) - reach, max(s.end for s in spans) + reach
    cur = await conn.execute(
        "SELECT id FROM speech_region WHERE state = 'waiting' AND start_ts > %s AND start_ts < %s AND end_ts > %s",
        (lo - MAX_REGION, hi, lo),
    )
    return any(r["id"] not in known for r in await cur.fetchall())


async def _first_unready(conn: AsyncConnection, group: list[speech.Span], ready: dict[dt.datetime, bool],
                         now: dt.datetime, expected: set[str]) -> dt.datetime | None:
    """The first minute the group touches that is not ready (``ready`` caches the pass's answers); None if all are."""
    for m in sorted({m for s in group for m in minutes_touched(s)}):
        if m not in ready:
            ready[m] = await minute_ready(conn, m, now, expected)
        if not ready[m]:
            return m
    return None


async def _apply(conn: AsyncConnection, part: list[speech.Span], now: dt.datetime) -> int:
    """Store ``speech.decide``'s decisions for the waiting spans of ``part``; return how many."""
    count = 0
    for d in speech.decide(part):
        await conn.execute(
            "UPDATE speech_region SET state = %s, covered_by = %s, decided_at = %s"
            " WHERE id = %s AND state = 'waiting'",
            (d.state, d.covered_by, now, d.id),
        )
        count += 1
    return count


async def _settle_items(conn: AsyncConnection, now: dt.datetime, result: Grouped) -> None:
    cur = await conn.execute(
        "SELECT i.id, i.ended_at, j.task_name, j.status, bool_or(r.state = 'waiting') AS waiting,"
        " bool_or(r.state = 'chosen') AS chosen"
        " FROM item i JOIN speech_region r ON r.item_id = i.id"
        " LEFT JOIN procrastinate_jobs j ON j.id = i.analysis_job_id"
        " WHERE i.kind = 'audio' AND i.analysis_state = 'pending' AND i.speech_detected_at IS NOT NULL"
        " GROUP BY i.id, i.ended_at, j.task_name, j.status ORDER BY i.id"
    )
    for row in await cur.fetchall():
        if row["waiting"]:
            continue
        if row["chosen"]:
            if row["task_name"] == jobs.TRANSCRIBE_AUDIO and row["status"] in ("todo", "doing", "failed"):
                continue  # queued, running, or on the dead-letter list (retried from Xử lý)
            if not await lock_pending_item(conn, row["id"]):
                continue
            priority = 0 if row["ended_at"] >= now - LIVE_WINDOW else BACKLOG_PRIORITY
            job_id = await jobs.enqueue(conn, jobs.TRANSCRIBE_AUDIO, queue="audio", lock="asr", priority=priority,
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
        await conn.execute(LOCK_SQL)
        result.decided = await _decide(conn, now)
        await _settle_items(conn, now, result)
    if result.decided or result.transcribing or result.finished:
        log.info("speech grouping: %d regions decided, %d items to transcribe, %d items covered elsewhere",
                 result.decided, len(result.transcribing), len(result.finished))
    return result


@job_app.periodic(cron="* * * * *")
@job_app.task(name=jobs.GROUP_SPEECH, queue="light", queueing_lock="group_speech")
async def group_speech_job(timestamp: int | None = None) -> None:
    await group_speech(get_context().pool)

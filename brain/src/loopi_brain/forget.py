""""Quên khoảng thời gian" (spec §9.5, §11): forget everything recorded in a time range, for good.

In one transaction: the recordings (items) that touch the range, with their keyframes, transcript segments
and moments (embeddings included), every other moment of the range, the commitments sourced in it, and the
recaps of the days it touches; the chapters of those days are reopened and the ones that overlap what goes lose
their title, summary and summary error (they quoted it); the days' stored job errors and failed recap runs go too
(an error may quote a model's answer; a failed run would keep the planner from writing the recap again), and so do
the golden-set samples taken from it (spec D21: clips, keyframes, correct moments of questions; their copies under
LOOPI_EVAL_DIR are swept after the commit). A
recording is deleted whole even when only part of it lies in the range, so "what goes" is the range widened to
those recordings (the span): chapters, recaps and the audit row follow the span, not just the range, and may reach
into the neighbouring day.

The transaction takes every touched day's lock (``state.lock_day``) before anything else, in day order, so a
summary or recap being stored waits for it — and then sees the new audit row and stores nothing (deliberate
deviation from §9.5, which only marks the recap out of date: here it is deleted and written again from what
remains, ruling R24). An audit row records the span, the counts and the media paths still to delete, never any
content. After the commit the media files go: raw files, playback/view copies, keyframes and their thumbnails,
each checked to lie inside the media root; a file that could not be deleted stays listed on the audit row and
the nightly retention run tries again. The vlogs of the days touched go the same way, but by their fixed names
(whatever their rows named) and without a listing: the planner makes the file again, so the 03:00 run must never get
it; a vlog file that stayed is replaced by the next render. A killed render's temp folders for those days go too.
The affected days' chapters are rebuilt.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

import anyio
from psycopg import AsyncConnection
from psycopg.errors import DeadlockDetected
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from . import devices, media, thumbs, volume
from .config import Settings
from .days import local_day
from .evaluation import golden
from .pipeline import chapters as chapter_rules
from .pipeline import recap as recap_rules
from .pipeline import vlogs as vlog_rules
from .pipeline.state import lock_day

log = logging.getLogger(__name__)
MAX_RANGE = dt.timedelta(days=7)  # one request stays a few seconds: ≈ 20 000 files and their rows at most
# Passes to lock the days and delete: one is enough unless a recording arrives meanwhile, or another writer
# (a caption, an embedding batch) wins a deadlock against this transaction, which then changed nothing.
LOCK_ATTEMPTS = 3
# An item touches [start, end) if it overlaps it, or is a zero-length item (a marker) inside it.
_ITEMS = "started_at < %(end)s AND (ended_at > %(start)s OR started_at >= %(start)s)"


@dataclass(frozen=True)
class ForgetCounts:
    items: int = 0
    keyframes: int = 0
    transcript_segments: int = 0
    moments: int = 0
    commitments: int = 0
    chapters: int = 0  # chapters that lose their title and summary
    recaps: int = 0
    vlogs: int = 0  # vlogs deleted (made again by the planner)
    golden: int = 0  # golden clips, keyframes and correct moments of questions
    files: int = 0
    files_failed: int = 0  # media files and golden copies that could not be deleted (the 03:00 run retries them)


class _MoreDays(Exception):
    """The days to lock, found under the locks, are not all locked: lock them all from the start, in order."""

    def __init__(self, days: list[dt.date]):
        super().__init__(days)
        self.days = days


def check_range(start: dt.datetime, end: dt.datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("the range needs time zones")
    if end <= start:
        raise ValueError("the end of the range must be after its start")
    if end - start > MAX_RANGE:
        raise ValueError(f"at most {MAX_RANGE.days} days at a time")


def days_touched(start: dt.datetime, end: dt.datetime, zone) -> list[dt.date]:
    """The local days that [start, end) touches."""
    first, last = local_day(start, zone), local_day(end - dt.timedelta(microseconds=1), zone)
    return [first + dt.timedelta(days=n) for n in range((last - first).days + 1)]


async def _targets(conn: AsyncConnection, start: dt.datetime, end: dt.datetime, lock: bool) -> dict:
    window = {"start": start, "end": end}
    cur = await conn.execute(
        "SELECT id, kind, raw_path, view_path, started_at, ended_at FROM item"
        f" WHERE {_ITEMS}{' FOR UPDATE' if lock else ''}",
        window,
    )
    items = await cur.fetchall()
    item_ids = [r["id"] for r in items]
    cur = await conn.execute(
        "SELECT id, path FROM keyframe WHERE item_id = ANY(%(items)s) OR (ts >= %(start)s AND ts < %(end)s)",
        {**window, "items": item_ids},
    )
    keyframes = await cur.fetchall()
    cur = await conn.execute(
        "SELECT id, ts_start FROM moment WHERE item_id = ANY(%(items)s) OR keyframe_id = ANY(%(keyframes)s)"
        " OR (ts_start >= %(start)s AND ts_start < %(end)s)",
        {**window, "items": item_ids, "keyframes": [k["id"] for k in keyframes]},
    )
    moments = await cur.fetchall()
    return {"items": items, "keyframes": keyframes, "moments": moments}


def _span(start: dt.datetime, end: dt.datetime, targets: dict) -> tuple[dt.datetime, dt.datetime]:
    """[start, end) widened to the recordings that go whole (a file cannot be cut at the edge of the range)."""
    items = targets["items"]
    return min([start, *(i["started_at"] for i in items)]), max([end, *(i["ended_at"] for i in items)])


def _affected_days(targets: dict, span: tuple[dt.datetime, dt.datetime], zone) -> list[dt.date]:
    """The local days whose chapters, recap and commitments lose material, in day order."""
    days = set(days_touched(*span, zone))
    return sorted(days | {local_day(m["ts_start"], zone) for m in targets["moments"]})


async def _derived(conn: AsyncConnection, targets: dict, days: list[dt.date], span) -> dict:
    moment_ids = [m["id"] for m in targets["moments"]]
    cur = await conn.execute("SELECT count(*) AS n FROM commitment WHERE source_moment_id = ANY(%s)", (moment_ids,))
    commitments = (await cur.fetchone())["n"]
    cur = await conn.execute(
        "SELECT count(*) AS n FROM chapter WHERE day = ANY(%s) AND ts_start < %s AND ts_end >= %s",
        (days, span[1], span[0]),
    )
    chapters = (await cur.fetchone())["n"]
    cur = await conn.execute("SELECT count(*) AS n FROM day_recap WHERE day = ANY(%s)", (days,))
    recaps = (await cur.fetchone())["n"]
    cur = await conn.execute("SELECT count(*) AS n FROM vlog WHERE day = ANY(%s)", (days,))
    vlogs = (await cur.fetchone())["n"]
    golden_count = await golden.forget_rows(conn, [i["id"] for i in targets["items"]],
                                            [k["id"] for k in targets["keyframes"]], moment_ids, delete=False)
    return {"commitments": commitments, "chapters": chapters, "recaps": recaps, "vlogs": vlogs,
            "golden": golden_count}


async def preview_span(
    pool: AsyncConnectionPool, start: dt.datetime, end: dt.datetime, zone
) -> tuple[ForgetCounts, tuple[dt.datetime, dt.datetime]]:
    """What ``forget_range`` would delete, and the span it would cover (the range widened to the recordings that
    go whole); changes nothing."""
    check_range(start, end)
    async with pool.connection() as conn:
        targets = await _targets(conn, start, end, lock=False)
        span = _span(start, end, targets)
        derived = await _derived(conn, targets, _affected_days(targets, span, zone), span)
        item_ids = [r["id"] for r in targets["items"]]
        cur = await conn.execute("SELECT count(*) AS n FROM transcript_segment WHERE item_id = ANY(%s)", (item_ids,))
        segments = (await cur.fetchone())["n"]
    counts = ForgetCounts(items=len(targets["items"]), keyframes=len(targets["keyframes"]),
                          transcript_segments=segments, moments=len(targets["moments"]), **derived)
    return counts, span


async def preview(pool: AsyncConnectionPool, start: dt.datetime, end: dt.datetime, zone) -> ForgetCounts:
    """What ``forget_range`` would delete; changes nothing."""
    return (await preview_span(pool, start, end, zone))[0]


def _files(targets: dict) -> tuple[list[str], dict[str, str]]:
    """Every media file of the forgotten rows (relative to the media root), and which row each one belongs to
    (for the log). No path is resolved here: ``remove_files`` checks each one against the media root."""
    paths: list[str] = []
    owners: dict[str, str] = {}
    for item in targets["items"]:
        for rel in media.item_files(item["kind"], item["raw_path"], item["view_path"]):
            paths.append(rel)
            owners[rel] = f"item {item['id']}"
    for keyframe in targets["keyframes"]:
        owner = f"keyframe {keyframe['id']}"
        paths.append(keyframe["path"])
        owners[keyframe["path"]] = owner
        try:
            thumb = str(thumbs.thumb_rel(PurePosixPath(keyframe["path"])))
        except ValueError:
            log.warning("%s: %s is not a keyframe path; its thumbnail is not deleted", owner, keyframe["path"])
            continue
        paths.append(thumb)
        owners[thumb] = owner
    return paths, owners


def retryable(media_root: Path, rels: list[str], owners: dict[str, str] | None = None) -> list[str]:
    """The paths worth trying again later: inside the media root (the others are refused for ever; each one is
    logged, with the row it belongs to when ``owners`` knows it)."""
    out = []
    for rel in rels:
        try:
            media.inside(media_root, rel)
        except ValueError as exc:
            log.warning("%s: %s; it is not deleted and not retried", (owners or {}).get(rel, "forgotten file"), exc)
            continue
        out.append(rel)
    return out


async def _delete_rows(
    pool: AsyncConnectionPool, start: dt.datetime, end: dt.datetime, zone, days: list[dt.date]
) -> tuple[ForgetCounts, str, list[str], dict[str, str], list[dt.date]]:
    """One transaction: take the locks of ``days`` (in day order, before anything else), delete, write the audit
    row. Returns (counts, audit row id, files to delete, their owners, the days that were affected). ``_MoreDays``
    (nothing changed) when the locked days turn out not to be all the affected days."""
    async with pool.connection() as conn, conn.transaction():
        for day in days:
            await lock_day(conn, day)
        targets = await _targets(conn, start, end, lock=True)
        span = _span(start, end, targets)
        affected = _affected_days(targets, span, zone)
        if not set(affected) <= set(days):
            raise _MoreDays(affected)
        derived = await _derived(conn, targets, affected, span)
        item_ids = [r["id"] for r in targets["items"]]
        moment_ids = [m["id"] for m in targets["moments"]]
        await golden.forget_rows(conn, item_ids, [k["id"] for k in targets["keyframes"]], moment_ids, delete=True)
        await conn.execute("DELETE FROM commitment WHERE source_moment_id = ANY(%s)", (moment_ids,))
        await conn.execute("DELETE FROM moment WHERE id = ANY(%s)", (moment_ids,))
        cur = await conn.execute("DELETE FROM transcript_segment WHERE item_id = ANY(%s)", (item_ids,))
        segments = cur.rowcount
        await conn.execute("DELETE FROM keyframe WHERE id = ANY(%s)", ([k["id"] for k in targets["keyframes"]],))
        await conn.execute("DELETE FROM item WHERE id = ANY(%s)", (item_ids,))
        await conn.execute(
            "UPDATE chapter SET title = NULL, summary = NULL, summary_signature = NULL, model_version = NULL,"
            " summary_error = NULL, summary_error_at = NULL, updated_at = now()"
            " WHERE day = ANY(%s) AND ts_start < %s AND ts_end >= %s",
            (affected, span[1], span[0]),
        )
        await conn.execute("UPDATE chapter SET closed = false WHERE day = ANY(%s)", (affected,))
        await conn.execute("DELETE FROM day_recap WHERE day = ANY(%s)", (affected,))
        # Error texts of these days may quote what goes (written before model answers stopped being quoted), and a
        # failed recap run left behind would keep the planner from writing the deleted recaps again.
        await recap_rules.drop_failed_recaps(conn, affected)
        # Vlogs show and say what is forgotten: they go, and the planner makes them again. Their files go after the
        # commit by their fixed names (see forget_range), not as listed paths.
        await conn.execute("DELETE FROM vlog WHERE day = ANY(%s)", (affected,))
        await vlog_rules.drop_failed_vlogs(conn, affected)
        await conn.execute("DELETE FROM day_job_error WHERE day = ANY(%s)", (affected,))
        counts = ForgetCounts(items=len(item_ids), keyframes=len(targets["keyframes"]), transcript_segments=segments,
                              moments=len(moment_ids), **derived)
        files, owners = _files(targets)
        log_id = devices.new_id()
        # Same transaction as the deletions: whoever stores a summary or recap of these days afterwards sees it.
        await conn.execute(
            "INSERT INTO forget_log (id, range_start, range_end, counts, pending_files) VALUES (%s, %s, %s, %s, %s)",
            (log_id, span[0], span[1], Jsonb(asdict(counts)), Jsonb(files)),
        )
    return counts, log_id, files, owners, affected


async def forget_range(pool: AsyncConnectionPool, settings: Settings, start: dt.datetime, end: dt.datetime) -> ForgetCounts:
    """Forget [start, end) for good; return what went. MediaVolumeMissing, before anything changes, while the media
    disk is away: its files would count as gone and stay on the disk."""
    check_range(start, end)
    volume.check(settings.media_root, settings.media_require_marker)
    zone = settings.zone
    async with pool.connection() as conn:  # a first look, only to learn which days to lock
        first = await _targets(conn, start, end, lock=False)
    days = _affected_days(first, _span(start, end, first), zone)
    for _ in range(LOCK_ATTEMPTS):
        try:
            counts, log_id, files, owners, days = await _delete_rows(pool, start, end, zone, days)
            break
        except _MoreDays as more:  # a recording arrived since the first look: lock all the days, again in order
            days = sorted(set(days) | set(more.days))
        except DeadlockDetected:  # this transaction was the victim: it rolled back, nothing changed
            log.warning("forget %s .. %s lost a deadlock; trying again", start.isoformat(), end.isoformat())
    else:
        raise RuntimeError(
            "this range could not be locked (its recordings keep changing, or the database kept refusing);"
            " nothing was forgotten, try again"
        )
    for day in days:
        try:
            await chapter_rules.defer_build(pool, day, zone)
        except Exception:  # the rows are gone for good; the hourly planner rebuilds the day anyway
            log.exception("could not ask for the chapters of %s", day)
    try:
        deleted, failed = await anyio.to_thread.run_sync(media.remove_files, settings.media_root, files)
    except Exception:  # the audit row still lists every path: the nightly run deletes them
        log.exception("deleting the media files failed; they stay listed on the audit row %s", log_id)
        deleted, failed = 0, files
    vlog_deleted, vlog_failed = 0, 0
    try:  # by fixed names, whatever the vlog rows named; not on the audit row: the planner remakes these files
        vlog_deleted, left = await anyio.to_thread.run_sync(vlog_rules.remove_forgotten, settings.media_root, days)
        vlog_failed = len(left)
    except Exception:
        log.exception("deleting the vlog files of the forgotten days failed; the next vlog of each day replaces them")
        vlog_failed = 1  # how many files stay is unknown: say that at least one may
    golden_left = 0
    try:  # the golden copies of the forgotten samples; anything left is swept again at 03:00
        _, left = await golden.sweep(pool, settings.eval_dir)
        golden_left = len(left)
    except Exception:
        log.exception("could not sweep the golden copies after the forget %s; the 03:00 run tries again", log_id)
        golden_left = 1  # how many copies stay is unknown: say that at least one may
    counts = ForgetCounts(**{**asdict(counts), "files": deleted + vlog_deleted,
                             "files_failed": len(failed) + golden_left + vlog_failed})
    keep = await anyio.to_thread.run_sync(retryable, settings.media_root, failed, owners)
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE forget_log SET counts = %s, pending_files = %s WHERE id = %s",
                (Jsonb(asdict(counts)), Jsonb(keep), log_id),
            )
    except Exception:  # the row keeps every path: the nightly run finds the deleted ones gone and the rest again
        log.exception("could not record the deleted files on the audit row %s", log_id)
    log.info("forgot %s .. %s: %s", start.isoformat(), end.isoformat(), asdict(counts))
    return counts

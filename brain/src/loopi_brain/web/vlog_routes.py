"""The Vlog pages (spec §10.6 "Vlog: xem và tải từng ngày"; P3 design §4.6; ruling R10): the list, one day's player
(the MP4 with a WebVTT track: browsers do not show mov_text), the files (Range requests for seeking; a download)
and "Dựng lại vlog". The file routes are declared before the page route: ``/vlog/{day}`` would take
``2026-10-14.mp4`` otherwise."""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from psycopg_pool import AsyncConnectionPool

from .. import media
from ..days import local_now
from ..pipeline import vlogs as vlog_rules
from .routes import TEMPLATES, require_htmx

router = APIRouter()
LIST_MAX = 120
# "Dựng lại vlog" replaces the file under the same URL: a browser must ask again before it plays or shows a copy it
# kept. FileResponse sends an ETag and Last-Modified and answers a ranged request whose ``If-Range`` no longer matches
# with the whole new file; it does not answer ``If-None-Match`` with 304.
REVALIDATE = {"Cache-Control": "private, no-cache"}


def _duration(seconds: float | None) -> str | None:
    if not seconds:
        return None
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}:{rest:02d}"


async def vlog_views(pool: AsyncConnectionPool, days: Sequence[dt.date], zone: ZoneInfo) -> dict[dt.date, dict]:
    """What the pages show of the vlogs of ``days``, in three queries however many days there are: the vlog rows, the
    failed vlog jobs (with the error stored for the latest of them) and the waiting ones. A day with no vlog row and no
    vlog job, waiting or failed, has no entry."""
    if not days:
        return {}
    locks = [vlog_rules.vlog_lock(day) for day in days]
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT day, state, script->>'title' AS title, duration_s, generated_at, path, stale, error, source,"
            " model_version FROM vlog WHERE day = ANY(%s)",
            (list(days),),
        )
        rows = {row["day"]: row for row in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT DISTINCT ON (j.queueing_lock) j.queueing_lock, e.error FROM procrastinate_jobs j"
            " LEFT JOIN day_job_error e ON e.task = j.task_name AND e.day = ANY(%s)"
            " AND j.queueing_lock = 'vlog:' || to_char(e.day, 'YYYY-MM-DD')"
            " WHERE j.queueing_lock = ANY(%s) AND j.status = 'failed' ORDER BY j.queueing_lock, j.id DESC",
            (list(days), locks),
        )
        failed = {row["queueing_lock"]: row["error"] for row in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT DISTINCT queueing_lock FROM procrastinate_jobs"
            " WHERE queueing_lock = ANY(%s) AND status IN ('todo', 'doing')",
            (locks,),
        )
        waiting = {row["queueing_lock"] for row in await cur.fetchall()}
    views = {}
    for day in days:
        lock, row = vlog_rules.vlog_lock(day), rows.get(day)
        if row is None and lock not in failed and lock not in waiting:
            continue
        row = row or {}
        views[day] = {
            "day": day,
            "state": row.get("state"),
            "title": row.get("title"),
            "duration": _duration(row.get("duration_s")),
            "at": row["generated_at"].astimezone(zone).strftime("%d/%m %H:%M") if row.get("generated_at") else None,
            "has_file": bool(row.get("path")) and row.get("state") != "empty",
            # Only a live job is work: a scripted or stale row alone may be a failed run the planner leaves alone.
            "working": lock in waiting,
            "error": row.get("error")
            or failed.get(lock)
            or ("lần dựng gần nhất bị lỗi" if lock in failed else None),
            "source": row.get("source"),
            "model": row.get("model_version"),
        }
    return views


async def vlog_view(pool: AsyncConnectionPool, day: dt.date, zone: ZoneInfo) -> dict | None:
    """What the pages show of one day's vlog; None when there is no vlog row and no vlog job, waiting or failed."""
    return (await vlog_views(pool, [day], zone)).get(day)


async def _file(request: Request, day: dt.date, suffix: str) -> Path:
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute("SELECT path FROM vlog WHERE day = %s AND path IS NOT NULL", (day,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no vlog")
    rel = row["path"] if suffix == ".mp4" else vlog_rules.vtt_rel(row["path"])
    try:
        path = media.inside(request.app.state.settings.media_root, rel)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="no vlog") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no vlog")
    return path


@router.get("/vlog", response_class=HTMLResponse)
async def vlog_list(request: Request):
    state = request.app.state
    async with state.pool.connection() as conn:
        cur = await conn.execute("SELECT day FROM vlog ORDER BY day DESC LIMIT %s", (LIST_MAX,))
        days = [r["day"] for r in await cur.fetchall()]
    views = await vlog_views(state.pool, days, state.settings.zone)
    shown = [views[day] for day in days if day in views]
    return TEMPLATES.TemplateResponse(request, "vlog_list.html", {"vlogs": shown})


@router.get("/vlog/{day}.mp4")
async def vlog_mp4(request: Request, day: dt.date, download: bool = Query(default=False)):
    path = await _file(request, day, ".mp4")
    if download:
        return FileResponse(
            path, media_type="video/mp4", filename=f"loopi-vlog-{day.isoformat()}.mp4", headers=REVALIDATE
        )
    return FileResponse(path, media_type="video/mp4", headers=REVALIDATE)


@router.get("/vlog/{day}.vtt")
async def vlog_vtt(request: Request, day: dt.date):
    return FileResponse(await _file(request, day, ".vtt"), media_type="text/vtt; charset=utf-8", headers=REVALIDATE)


@router.get("/vlog/{day}", response_class=HTMLResponse)
async def vlog_page(request: Request, day: dt.date):
    state = request.app.state
    zone = state.settings.zone
    return TEMPLATES.TemplateResponse(
        request,
        "vlog_day.html",
        {"day": day, "vlog": await vlog_view(state.pool, day, zone), "is_future": day > local_now(zone).date()},
    )


@router.post("/vlog/{day}/rebuild", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def vlog_rebuild(request: Request, day: dt.date):
    if day > local_now(request.app.state.settings.zone).date():
        return HTMLResponse('<span class="err">Ngày này chưa tới.</span>')
    if await vlog_rules.request_rebuild(request.app.state.pool, day) is None:
        return HTMLResponse('<span class="err">Chưa yêu cầu được, hãy thử lại sau.</span>')
    return HTMLResponse("<span>Đã yêu cầu dựng lại vlog; vài phút nữa xem lại trang này.</span>")

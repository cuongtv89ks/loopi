"""The Vlog pages (spec §10.6 "Vlog: xem và tải từng ngày"; P3 design §4.6; ruling R10): the list, one day's player
(the MP4 with a WebVTT track: browsers do not show mov_text), the files (Range requests for seeking; a download)
and "Dựng lại vlog". The file routes are declared before the page route: ``/vlog/{day}`` would take
``2026-10-14.mp4`` otherwise."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from .. import media
from ..pipeline import vlogs as vlog_rules
from .routes import TEMPLATES, require_htmx

router = APIRouter()
LIST_MAX = 120


def _duration(seconds: float | None) -> str | None:
    if not seconds:
        return None
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}:{rest:02d}"


async def vlog_view(pool, day: dt.date, zone) -> dict | None:
    """What the pages show of one day's vlog; None when there is no vlog row and no vlog job, waiting or failed."""
    lock = vlog_rules.vlog_lock(day)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM vlog WHERE day = %s", (day,))
        row = await cur.fetchone()
        cur = await conn.execute(
            "SELECT e.error FROM procrastinate_jobs j LEFT JOIN day_job_error e ON e.task = j.task_name AND e.day = %s"
            " WHERE j.queueing_lock = %s AND j.status = 'failed' ORDER BY j.id DESC LIMIT 1",
            (day, lock),
        )
        failed = await cur.fetchone()
        cur = await conn.execute(
            "SELECT 1 FROM procrastinate_jobs WHERE queueing_lock = %s AND status IN ('todo', 'doing')", (lock,)
        )
        waiting = await cur.fetchone()
    if row is None and failed is None and waiting is None:
        return None
    row = row or {}
    script = row.get("script") or {}
    return {
        "day": day,
        "state": row.get("state"),
        "title": script.get("title"),
        "duration": _duration(row.get("duration_s")),
        "at": row["generated_at"].astimezone(zone).strftime("%d/%m %H:%M") if row.get("generated_at") else None,
        "has_file": bool(row.get("path")) and row.get("state") != "empty",
        "working": waiting is not None or row.get("state") == "scripted" or bool(row.get("stale")),
        "error": row.get("error") or (failed["error"] if failed and failed["error"] else None)
        or ("lần dựng gần nhất bị lỗi" if failed else None),
        "source": row.get("source"),
        "model": row.get("model_version"),
    }


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
    views = [v for day in days if (v := await vlog_view(state.pool, day, state.settings.zone)) is not None]
    return TEMPLATES.TemplateResponse(request, "vlog_list.html", {"vlogs": views})


@router.get("/vlog/{day}.mp4")
async def vlog_mp4(request: Request, day: dt.date, download: bool = Query(default=False)):
    path = await _file(request, day, ".mp4")
    if download:
        return FileResponse(path, media_type="video/mp4", filename=f"loopi-vlog-{day.isoformat()}.mp4")
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/vlog/{day}.vtt")
async def vlog_vtt(request: Request, day: dt.date):
    return FileResponse(await _file(request, day, ".vtt"), media_type="text/vtt; charset=utf-8")


@router.get("/vlog/{day}", response_class=HTMLResponse)
async def vlog_page(request: Request, day: dt.date):
    state = request.app.state
    return TEMPLATES.TemplateResponse(
        request, "vlog_day.html", {"day": day, "vlog": await vlog_view(state.pool, day, state.settings.zone)}
    )


@router.post("/vlog/{day}/rebuild", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def vlog_rebuild(request: Request, day: dt.date):
    if await vlog_rules.request_rebuild(request.app.state.pool, day) is None:
        return HTMLResponse('<span class="err">Chưa yêu cầu được, hãy thử lại sau.</span>')
    return HTMLResponse("<span>Đã yêu cầu dựng lại vlog; vài phút nữa xem lại trang này.</span>")

"""The Day page ("Hôm nay" / "Ngày", spec §10.6): chapters in order with their pictures, speech and marked
moments, the recap and the commitments of one local day; keyframe thumbnails; the recap and commitment buttons."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from .. import thumbs
from ..days import day_bounds_utc, local_now
from ..pipeline import moments as moment_rules
from ..pipeline import recap as recap_rules
from .markdown import render_markdown
from .routes import MARKER_LABEL, TEMPLATES, require_htmx
from .vlog_routes import vlog_view

WEEKDAYS = ("Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật")
STATUS_LABELS = {"open": "Chưa xong", "done": "Đã xong", "dismissed": "Đã bỏ qua"}
FRAMES_SHOWN = 6  # pictures per chapter shown at once; the others are behind "Tất cả khung hình"
OPEN_DAYS_BACK = 7  # today's page also lists the open commitments of the previous days
TEMPLATES.env.globals["STATUS_LABELS"] = STATUS_LABELS

router = APIRouter()


def day_title(day: dt.date) -> str:
    return f"{WEEKDAYS[day.weekday()]}, {day:%d/%m/%Y}"


def _moment_view(row: dict, zone) -> dict:
    view = {**row, "time": row["ts_start"].astimezone(zone).strftime("%H:%M:%S")}
    if row["kind"] == "marker":
        view["label"] = moment_rules.marker_note(row["item_meta"] or {}) or MARKER_LABEL
    if row["kind"] == "speech" and row["item_id"] and row["item_kind"] == "audio":
        offset = max(0, int((row["ts_start"] - row["item_start"]).total_seconds()))
        view["audio"] = None if row["raw_deleted_at"] else f"/audio/{row['item_id']}.opus#t={offset}"
        view["expired"] = row["raw_deleted_at"] is not None
    return view


def _group(title: str | None, rows: list[dict]) -> dict:
    frames = [r for r in rows if r["kind"] == "visual" and r["keyframe_id"]]
    return {
        "frames": frames[:FRAMES_SHOWN],
        "more_frames": frames[FRAMES_SHOWN:],
        "speech": [r for r in rows if r["kind"] == "speech"],
        "markers": [r for r in rows if r["kind"] == "marker"],
        "title": title,
    }


_COMMITMENTS = (
    "SELECT c.*, m.ts_start AS source_ts FROM commitment c LEFT JOIN moment m ON m.id = c.source_moment_id"
)


async def day_view(pool, day: dt.date, zone, today: dt.date | None = None) -> dict:
    """Everything the Day page shows for local ``day`` (on ``today``'s page: also the commitments still open
    from the previous 7 days)."""
    start, end = day_bounds_utc(day, zone)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM chapter WHERE day = %s ORDER BY ts_start", (day,))
        chapter_rows = await cur.fetchall()
        cur = await conn.execute(
            "SELECT m.id, m.kind, m.ts_start, m.text, m.lang, m.keyframe_id, m.item_id, k.path AS keyframe_path,"
            " i.kind AS item_kind, i.meta AS item_meta, i.started_at AS item_start, i.raw_deleted_at"
            " FROM moment m LEFT JOIN item i ON i.id = m.item_id LEFT JOIN keyframe k ON k.id = m.keyframe_id"
            " WHERE m.ts_start >= %s AND m.ts_start < %s ORDER BY m.ts_start, m.id",
            (start, end),
        )
        moment_rows = [_moment_view(r, zone) for r in await cur.fetchall()]
        cur = await conn.execute("SELECT * FROM day_recap WHERE day = %s", (day,))
        recap = await cur.fetchone()
        cur = await conn.execute(f"{_COMMITMENTS} WHERE c.day = %s ORDER BY c.created_at, c.id", (day,))
        commitments = [_commitment_view(r, zone) for r in await cur.fetchall()]
        earlier = []
        if day == today:
            cur = await conn.execute(
                f"{_COMMITMENTS} WHERE c.status = 'open' AND c.day < %s AND c.day >= %s"
                " ORDER BY c.day DESC, c.created_at, c.id",
                (day, day - dt.timedelta(days=OPEN_DAYS_BACK)),
            )
            earlier = [_commitment_view(r, zone) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT e.error FROM procrastinate_jobs j"
            " LEFT JOIN day_job_error e ON e.task = j.task_name AND e.day = %s"
            " WHERE j.queueing_lock = %s AND j.status = 'failed' ORDER BY j.id DESC LIMIT 1",
            (day, recap_rules.recap_lock(day)),
        )
        recap_failed = await cur.fetchone()
    # Chapters and the moments outside any chapter (not built yet, or arrived since), in time order.
    sections, loose = [], []
    k = 0

    def flush_loose() -> None:
        if loose:
            sections.append({**_group(None, list(loose)), "kind": "loose", "id": loose[0]["id"], "count": len(loose),
                             "start": loose[0]["ts_start"].astimezone(zone).strftime("%H:%M")})
            loose.clear()

    for row in chapter_rows:
        members = []
        while k < len(moment_rows) and moment_rows[k]["ts_start"] <= row["last_moment_at"]:
            if moment_rows[k]["ts_start"] >= row["ts_start"]:
                members.append(moment_rows[k])
            else:
                loose.append(moment_rows[k])
            k += 1
        flush_loose()
        sections.append({
            **_group(row["title"], members),
            "kind": "chapter",
            "id": row["id"],
            "summary": row["summary"],
            "outdated": row["summary"] is not None and row["summary_signature"] != row["signature"],
            "start": row["ts_start"].astimezone(zone).strftime("%H:%M"),
            "end": row["ts_end"].astimezone(zone).strftime("%H:%M"),
        })
    loose.extend(moment_rows[k:])
    flush_loose()
    return {
        "sections": sections,
        "earlier_open": earlier,
        "recap_failed": recap_failed,
        "recap": recap,
        "recap_html": render_markdown(recap["markdown"]) if recap else None,
        "recap_at": recap["generated_at"].astimezone(zone).strftime("%d/%m %H:%M") if recap else None,
        "commitments": commitments,
        "has_data": bool(moment_rows),
    }


def _commitment_view(row: dict, zone) -> dict:
    return {**row, "source_time": row["source_ts"].astimezone(zone).strftime("%H:%M") if row["source_ts"] else None}


@router.get("/", response_class=HTMLResponse)
async def day_page(request: Request, day: dt.date | None = Query(default=None)):
    zone = request.app.state.settings.zone
    today = local_now(zone).date()
    day = day or today
    try:
        day_bounds_utc(day, zone)
        prev_day, next_day = day - dt.timedelta(days=1), day + dt.timedelta(days=1)
    except OverflowError as exc:  # the first and last days of the calendar have no UTC bounds or no neighbour
        raise HTTPException(status_code=422, detail=f"day {day} is out of range") from exc
    return TEMPLATES.TemplateResponse(
        request,
        "day.html",
        {
            **await day_view(request.app.state.pool, day, zone, today),
            "day": day,
            "title": day_title(day),
            "is_today": day == today,
            "prev_day": prev_day,
            "next_day": next_day,
            "vlog": await vlog_view(request.app.state.pool, day, zone),
        },
    )


@router.get("/thumb/{keyframe_id}.jpg")
async def thumbnail(request: Request, keyframe_id: str):
    """A keyframe 320 px wide, made on the first request and kept under ``thumbs/``."""
    state = request.app.state
    try:
        dst = await thumbs.keyframe_thumb(state.pool, state.settings.media_root, keyframe_id)
    except thumbs.ThumbUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(dst, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/day/{day}/recap", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def rewrite_recap(request: Request, day: dt.date):
    """"Tạo lại recap": drop the day's failed recap runs and their error, mark the current recap out of date and
    ask for a new one now (``recap.request_rewrite``)."""
    if await recap_rules.request_rewrite(request.app.state.pool, day) is None:
        return HTMLResponse(
            "<span>Chưa gửi được yêu cầu viết lại recap; recap sẽ được viết lại theo lịch.</span>"
        )
    return HTMLResponse("<span>Đã yêu cầu viết lại recap. Tải lại trang sau vài phút để xem.</span>")


@router.post("/commitments/{commitment_id}/{status}", response_class=HTMLResponse,
             dependencies=[Depends(require_htmx)])
async def commitment_status(request: Request, commitment_id: str, status: str):
    if status not in recap_rules.STATUSES:
        raise HTTPException(status_code=404, detail="unknown status")
    pool = request.app.state.pool
    if await recap_rules.set_commitment_status(pool, commitment_id, status) is None:
        raise HTTPException(status_code=404, detail="unknown commitment")
    async with pool.connection() as conn:
        cur = await conn.execute(f"{_COMMITMENTS} WHERE c.id = %s", (commitment_id,))
        row = await cur.fetchone()
    return TEMPLATES.TemplateResponse(
        request, "_commitment.html", {"c": _commitment_view(row, request.app.state.settings.zone)}
    )

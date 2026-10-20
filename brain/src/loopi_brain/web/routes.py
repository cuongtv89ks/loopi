"""Server-rendered web UI (Jinja + HTMX). Protected by Caddy basic auth."""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import audio_level, bundles, cameras, devices, media, ota, pki, places
from ..days import day_bounds_utc
from ..pipeline import chapters as chapter_rules
from ..pipeline import moments as moment_rules
from ..pipeline import status
from ..pipeline.state import raw_kept

log = logging.getLogger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["duration"] = status.fmt_duration
ONLINE_WINDOW = dt.timedelta(seconds=45)
MARKER_LABEL = "Đánh dấu"  # a marker without a note
COMMAND_LABELS = {
    "pause": "Tạm dừng ghi",
    "resume": "Tiếp tục ghi",
    "audio-on": "Bật ghi âm",
    "audio-off": "Tắt ghi âm",
}
COMMANDS: dict[str, tuple[str, dict]] = {
    "pause": ("pause", {}),
    "resume": ("resume", {}),
    "audio-on": ("set_config", {"audio_enabled": True}),
    "audio-off": ("set_config", {"audio_enabled": False}),
}
# OTA update states (P4a design §8), owner-facing.
OTA_STATE_LABELS = {
    "requested": "Đã gửi lệnh, chờ thiết bị nhận",
    "downloading": "Đang tải",
    "installing": "Đang cài",
    "rebooting": "Đang khởi động lại",
    "trying": "Đang thử bản mới",
    "succeeded": "Đã cập nhật",
    "rolled_back": "Đã quay về bản cũ",
    "failed": "Lỗi",
}
TEMPLATES.env.filters["ota_state"] = lambda state: OTA_STATE_LABELS.get(state, state)

router = APIRouter()


def require_htmx(hx_request: str | None = Header(default=None)) -> None:
    """CSRF guard: cross-site forms cannot set custom headers."""
    if hx_request != "true":
        raise HTTPException(status_code=403, detail="missing HX-Request header")


def _hour() -> dict[str, list]:
    return {"keyframes": [], "speech": [], "markers": [], "segments": []}


async def _day_by_hour(
    pool, day: dt.date, zone: ZoneInfo, device: str | None = None, labels: dict[str, str] | None = None
) -> dict[int, dict[str, list]]:
    """Keyframes, speech, markers and raw segments of one local day, grouped by local hour. With ``device``, only
    that camera's; with ``labels`` (device id -> location), each entry carries its place."""
    start, end = day_bounds_utc(day, zone)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT i.id, i.device_id, i.kind, i.started_at, i.ended_at, i.status, i.view_path, i.meta,"
            " i.analysis_state, i.raw_deleted_at,"
            " j.status IS NOT DISTINCT FROM 'failed' AS analysis_failed"
            " FROM item i LEFT JOIN procrastinate_jobs j ON j.id = i.analysis_job_id"
            " WHERE i.started_at >= %s AND i.started_at < %s AND (%s::text IS NULL OR i.device_id = %s)"
            " ORDER BY i.started_at",
            (start, end, device, device),
        )
        items = await cur.fetchall()
        cur = await conn.execute(
            "SELECT k.id, k.device_id, k.ts, k.path, k.caption,"
            " j.status IS NOT DISTINCT FROM 'failed' AS caption_failed"
            " FROM keyframe k LEFT JOIN procrastinate_jobs j ON j.id = k.caption_job_id"
            " WHERE k.ts >= %s AND k.ts < %s AND (%s::text IS NULL OR k.device_id = %s) ORDER BY k.ts",
            (start, end, device, device),
        )
        keyframes = await cur.fetchall()
        cur = await conn.execute(
            "SELECT m.id, m.device_id, m.kind, m.ts_start, m.lang, m.text, i.meta AS item_meta"
            " FROM moment m LEFT JOIN item i ON i.id = m.item_id"
            " WHERE m.kind IN ('speech', 'marker') AND m.ts_start >= %s AND m.ts_start < %s"
            " AND (%s::text IS NULL OR m.device_id = %s) ORDER BY m.ts_start",
            (start, end, device, device),
        )
        moments = await cur.fetchall()

    def place(row: dict) -> str | None:
        return (labels or {}).get(row["device_id"])

    hours: dict[int, dict[str, list]] = defaultdict(_hour)
    for row in items:
        local_start = row["started_at"].astimezone(zone)
        hours[local_start.hour]["segments"].append(
            {**row, "local_start": local_start, "local_end": row["ended_at"].astimezone(zone), "place": place(row)}
        )
    for row in keyframes:
        local_ts = row["ts"].astimezone(zone)
        hours[local_ts.hour]["keyframes"].append({**row, "local_ts": local_ts, "place": place(row)})
    for row in moments:
        local_ts = row["ts_start"].astimezone(zone)
        if row["kind"] == "speech":
            hours[local_ts.hour]["speech"].append({**row, "local_ts": local_ts, "place": place(row)})
        else:  # the marker's note; the moment's text is the English fallback that gets embedded
            label = moment_rules.marker_note(row["item_meta"] or {}) or MARKER_LABEL
            hours[local_ts.hour]["markers"].append({**row, "local_ts": local_ts, "label": label, "place": place(row)})
    return dict(sorted(hours.items()))


def _ca_fingerprint(request: Request) -> str:
    return pki.fingerprint(pki.ca_cert_pem(request.app.state.settings.pki_dir))


@router.get("/hours", response_class=HTMLResponse)
async def timeline(
    request: Request, day: dt.date | None = Query(default=None), device: str | None = Query(default=None, max_length=64)
):
    """"Chi tiết theo giờ": every keyframe, speech line, marker and segment of a day, by local hour (P2a), of every
    camera or of the one named by ``device``; with several cameras each entry names its place."""
    zone = request.app.state.settings.zone
    pool = request.app.state.pool
    day = day or dt.datetime.now(zone).date()
    device = device or None
    if device and not await places.known_device(pool, device):
        raise HTTPException(status_code=422, detail="unknown device")
    day_cams = await places.day_places(pool, *day_bounds_utc(day, zone), include=device)
    labels = {p["id"]: p["location"] for p in day_cams} if device is None and len(day_cams) >= 2 else None
    hours = await _day_by_hour(pool, day, zone, device, labels)
    return TEMPLATES.TemplateResponse(
        request,
        "timeline.html",
        {
            "day": day,
            "prev_day": day - dt.timedelta(days=1),
            "next_day": day + dt.timedelta(days=1),
            "hours": hours,
            "places": day_cams,
            "device": device,
            "base": "/hours",
        },
    )


@router.get("/audio/{item_id}.opus")
async def audio_playback(request: Request, item_id: str):
    """An audio segment at a level you can hear (spec D19): a normalised copy under ``view/``, made on the
    first request; the raw file when that fails. Raw files are never changed."""
    settings = request.app.state.settings
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT raw_path, raw_deleted_at FROM item WHERE id = %s AND kind = 'audio'", (item_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown audio item")
    if row["raw_deleted_at"] is not None:  # retention expired it: its files are going or gone, no copy is made
        raise HTTPException(status_code=404, detail="audio expired")
    raw_rel = PurePosixPath(row["raw_path"])
    raw = settings.media_root / raw_rel
    if not raw.is_file():
        raise HTTPException(status_code=404, detail="audio file missing")
    view = settings.media_root / media.audio_view_path(raw_rel)
    if not view.is_file():
        try:
            gain = await media.normalise_audio(raw, view)
        except Exception as exc:
            if isinstance(exc, (media.MediaError, OSError)):
                log.warning("audio %s plays at its raw level: %s", item_id, exc)
            else:
                log.exception("audio %s plays at its raw level after an unexpected failure", item_id)
            if not await raw_kept(request.app.state.pool, item_id):  # forgotten or expired meanwhile
                raise HTTPException(status_code=404, detail="unknown audio item") from exc
            # no-store: once the copy exists, the browser must not keep playing this quiet file from its cache
            return FileResponse(raw, media_type="audio/ogg", headers={"Cache-Control": "no-store"})
        log.info("audio %s normalised: %+.1f dB", item_id, audio_level.dbfs(gain))
        if not await raw_kept(request.app.state.pool, item_id):  # forgotten or expired while the copy was made
            media.remove_files(settings.media_root, [str(media.audio_view_path(raw_rel))])
            raise HTTPException(status_code=404, detail="unknown audio item")
    return FileResponse(view, media_type="audio/ogg")


@router.get("/status/recording", response_class=HTMLResponse)
async def recording_now(request: Request):
    """The header line on every page (htmx, every 15 s)."""
    count = await devices.recording_count(request.app.state.pool)
    return TEMPLATES.TemplateResponse(request, "_recording_now.html", {"count": count})


@router.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request):
    settings = request.app.state.settings
    pool = request.app.state.pool
    zone = settings.zone
    now = dt.datetime.now(dt.UTC)
    await ota.expire_stale(pool)
    rows = []
    for row in await devices.list_devices(pool):
        seen = row["last_heartbeat_at"]
        rows.append({
            **row,
            "online": seen is not None and now - seen < ONLINE_WINDOW,
            "hb": row["last_heartbeat"] or {},
            "seen_local": seen.astimezone(zone) if seen else None,
        })
    offered, _ = bundles.scan(settings.ota_dir)
    from .camera_routes import form_context  # camera_routes imports this module

    cams = await cameras.list_cameras(pool)
    return TEMPLATES.TemplateResponse(
        request,
        "devices.html",
        {
            **await form_context(request),
            "cameras_by_device": {c["device_id"]: c for c in cams if c["device_id"]},
            "waiting_cameras": [c for c in cams if not c["device_id"]],
            "devices": rows,
            "ca_fingerprint": _ca_fingerprint(request),
            "bundles": offered,
            "updates": await ota.recent_updates(pool, [row["id"] for row in rows]),
            "zone": zone,
        },
    )


@router.post("/devices/pair-token", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def new_pair_token(request: Request):
    settings = request.app.state.settings
    token = await devices.create_pair_token(
        request.app.state.pool, dt.timedelta(minutes=settings.pair_token_ttl_minutes)
    )
    return TEMPLATES.TemplateResponse(
        request,
        "_pair_token.html",
        {
            "token": token["token"],
            "expires_at": token["expires_at"].astimezone(settings.zone),
            "ca_fingerprint": _ca_fingerprint(request),
            "brain_url": f"https://{request.headers.get('host', request.url.netloc)}",
        },
    )


@router.post(
    "/devices/{device_id}/commands/{action}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_htmx)],
)
async def send_command(request: Request, device_id: str, action: str):
    if action not in COMMANDS:
        raise HTTPException(status_code=404, detail="unknown action")
    type_, payload = COMMANDS[action]
    try:
        await devices.enqueue_command(request.app.state.pool, device_id, type_, payload)
    except psycopg.errors.ForeignKeyViolation as exc:
        raise HTTPException(status_code=404, detail="unknown device") from exc
    return TEMPLATES.TemplateResponse(request, "_queued.html", {"label": COMMAND_LABELS[action], "revoked": False})


@router.post("/devices/{device_id}/revoke", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def revoke(request: Request, device_id: str):
    if not await devices.revoke_device(request.app.state.pool, device_id):
        raise HTTPException(status_code=404, detail="unknown device")
    return TEMPLATES.TemplateResponse(request, "_queued.html", {"label": None, "revoked": True})


@router.post("/devices/{device_id}/location", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def save_location(request: Request, device_id: str, location: str = Form("")):
    """Rename where a device stands (D34). A refused name is answered with the form and the reason (200, so htmx
    swaps it in); an unknown or revoked device is a 404, whatever the name."""
    pool = request.app.state.pool
    if not await places.is_active_device(pool, device_id):
        raise HTTPException(status_code=404, detail="unknown device")
    try:
        cleaned = places.clean_location(location)
    except places.LocationError as exc:
        return TEMPLATES.TemplateResponse(
            request, "_location.html", {"d": {"id": device_id, "location": location}, "error": str(exc)}
        )
    if not await places.set_location(pool, device_id, cleaned):
        raise HTTPException(status_code=404, detail="unknown device")
    try:
        await chapter_rules.refresh_places(pool, device_id, request.app.state.settings.zone)
    except Exception:  # the new name is saved; summaries refresh by hand (Xử lý lại) if this fails
        log.exception("could not refresh the summaries after renaming %s", device_id)
    return TEMPLATES.TemplateResponse(request, "_location.html", {"d": {"id": device_id, "location": cleaned},
                                                                   "saved": True})


@router.get("/pipeline", response_class=HTMLResponse)
async def pipeline_page(request: Request):
    state = request.app.state
    return TEMPLATES.TemplateResponse(
        request,
        "pipeline.html",
        {
            **await status.pipeline_status(state.pool),
            "health": await status.model_health(state.settings, state.http),
            "understanding": await status.understanding_status(state.pool, state.settings.zone),
        },
    )


@router.post("/pipeline/jobs/retry-failed", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def retry_all_jobs(request: Request):
    count = await status.retry_all_failed(request.app.state.pool)
    return TEMPLATES.TemplateResponse(request, "_retried.html", {"count": count})


@router.post("/pipeline/jobs/{job_id}/retry", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def retry_job(request: Request, job_id: int):
    if not await status.retry_failed_job(request.app.state.pool, job_id):
        raise HTTPException(status_code=409, detail="job is not in the failed state")
    return TEMPLATES.TemplateResponse(request, "_retried.html", {"job_id": job_id})

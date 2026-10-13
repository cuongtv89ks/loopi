"""Server-rendered web UI (Jinja + HTMX). Protected by Caddy basic auth."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import devices, pki

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
ONLINE_WINDOW = dt.timedelta(seconds=45)
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

router = APIRouter()


def require_htmx(hx_request: str | None = Header(default=None)) -> None:
    """CSRF guard: cross-site forms cannot set custom headers."""
    if hx_request != "true":
        raise HTTPException(status_code=403, detail="missing HX-Request header")


def day_bounds_utc(day: dt.date, zone: ZoneInfo) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, dt.time.min, tzinfo=zone)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=zone)
    return start.astimezone(dt.UTC), end.astimezone(dt.UTC)


async def _items_by_hour(pool, day: dt.date, zone: ZoneInfo) -> dict[int, list[dict]]:
    start, end = day_bounds_utc(day, zone)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, kind, started_at, ended_at, status, view_path, meta FROM item"
            " WHERE started_at >= %s AND started_at < %s ORDER BY started_at",
            (start, end),
        )
        rows = await cur.fetchall()
    by_hour: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        local_start = row["started_at"].astimezone(zone)
        by_hour[local_start.hour].append(
            {**row, "local_start": local_start, "local_end": row["ended_at"].astimezone(zone)}
        )
    return dict(sorted(by_hour.items()))


def _ca_fingerprint(request: Request) -> str:
    return pki.fingerprint(pki.ca_cert_pem(request.app.state.settings.pki_dir))


@router.get("/", response_class=HTMLResponse)
async def timeline(request: Request, day: dt.date | None = Query(default=None)):
    zone = request.app.state.settings.zone
    day = day or dt.datetime.now(zone).date()
    hours = await _items_by_hour(request.app.state.pool, day, zone)
    return TEMPLATES.TemplateResponse(
        request,
        "timeline.html",
        {"day": day, "prev_day": day - dt.timedelta(days=1), "next_day": day + dt.timedelta(days=1), "hours": hours},
    )


@router.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request):
    zone = request.app.state.settings.zone
    now = dt.datetime.now(dt.UTC)
    rows = []
    for row in await devices.list_devices(request.app.state.pool):
        seen = row["last_heartbeat_at"]
        rows.append({
            **row,
            "online": seen is not None and now - seen < ONLINE_WINDOW,
            "hb": row["last_heartbeat"] or {},
            "seen_local": seen.astimezone(zone) if seen else None,
        })
    return TEMPLATES.TemplateResponse(
        request, "devices.html", {"devices": rows, "ca_fingerprint": _ca_fingerprint(request)}
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

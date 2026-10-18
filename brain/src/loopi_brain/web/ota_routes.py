"""OTA on the web (P4a design §8, §8.1; spec D27, D29): the update controls of Thiết bị and the Phiên bản page."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import bundles, devices, ota
from .routes import TEMPLATES, require_htmx

router = APIRouter()


def _running(device_rows: list[dict]) -> dict[tuple[str, str], list[str]]:
    """(version, variant) -> names of the unrevoked devices whose latest heartbeat reports it."""
    running: dict[tuple[str, str], list[str]] = {}
    for row in device_rows:
        heartbeat = row["last_heartbeat"] or {}
        if row["revoked"] or not heartbeat.get("os_version"):
            continue
        key = (heartbeat["os_version"], heartbeat.get("os_variant") or "")
        running.setdefault(key, []).append(row["name"])
    return running


@router.get("/versions", response_class=HTMLResponse)
async def versions_page(request: Request):
    state = request.app.state
    await ota.expire_stale(state.pool)
    offered, unusable = bundles.scan(state.settings.ota_dir)
    return TEMPLATES.TemplateResponse(
        request,
        "versions.html",
        {
            "bundles": offered,
            "unusable": unusable,
            "running": _running(await devices.list_devices(state.pool)),
            "zone": state.settings.zone,
        },
    )


@router.post("/versions/{stem}/delete", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def delete_version(request: Request, stem: str):
    state = request.app.state
    try:
        await ota.delete_bundle(state.pool, state.settings.ota_dir, stem)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="unknown bundle") from exc
    except ota.DeleteRefused as exc:
        found = bundles.find(state.settings.ota_dir, f"{stem}.raucb")
        return TEMPLATES.TemplateResponse(
            request,
            "_bundle_card.html",
            {
                "b": found,
                "stem": stem,
                "error": str(exc),
                "running": _running(await devices.list_devices(state.pool)),
                "zone": state.settings.zone,
            },
        )
    return TEMPLATES.TemplateResponse(request, "_bundle_deleted.html", {"stem": stem})


@router.post("/devices/{device_id}/update", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def update_device(request: Request, device_id: str, bundle: str = Form("")):
    state = request.app.state
    try:
        await ota.request_update(state.pool, state.settings.ota_dir, device_id, bundle)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="unknown device") from exc
    except ota.UpdateRefused as exc:
        return TEMPLATES.TemplateResponse(request, "_update_result.html", {"error": str(exc)})
    return TEMPLATES.TemplateResponse(request, "_update_result.html", {"error": None, "bundle": bundle})


@router.get("/devices/update-notes", response_class=HTMLResponse)
async def update_notes(request: Request, bundle: str = ""):
    found = bundles.find(request.app.state.settings.ota_dir, bundle) if bundle else None
    return TEMPLATES.TemplateResponse(request, "_update_notes.html", {"b": found})

"""Network cameras on the Thiết bị page (P3c design §4.1, §4.5, D36)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from .. import camera_probe, cameras, capacity
from .routes import TEMPLATES, require_htmx

router = APIRouter()


async def form_context(request: Request, kind: str = "ip", values: dict | None = None, error: str | None = None) -> dict:
    kind = kind if kind in cameras.KINDS else "ip"
    meta = cameras.KINDS[kind]
    est = await capacity.estimate(request.app.state.pool, request.app.state.settings)
    defaults = {"kind": kind, "location": "", "video_url": meta.video_template, "audio_url": "",
                "username": meta.username, "has_audio": meta.has_audio}
    return {"kinds": cameras.KINDS, "meta": meta, "values": defaults | (values or {}), "error": error,
            "capacity": est, "gb": capacity.gb}


def _fields(kind, location, video_url, audio_url, username, has_audio) -> dict:
    return {"kind": kind, "location": location, "video_url": video_url, "audio_url": audio_url, "username": username,
            "has_audio": has_audio == "on"}


@router.get("/cameras/form", response_class=HTMLResponse)
async def camera_form(request: Request, kind: str = Query("ip")):
    return TEMPLATES.TemplateResponse(request, "_camera_form.html", await form_context(request, kind))


@router.post("/cameras/probe", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def probe(request: Request, video_url: str = Form(""), audio_url: str = Form(""), username: str = Form(""),
                password: str = Form("")):
    # The same checks as a save (clean_input): what it would refuse is never handed to ffprobe.
    try:
        video = cameras.clean_video_url(video_url)
        audio = cameras.clean_audio_url(audio_url)
        user, secret = cameras.clean_credentials(username, password)
    except cameras.CameraError as exc:
        return TEMPLATES.TemplateResponse(request, "_camera_probe.html",
                                          {"results": [camera_probe.ProbeResult(False, str(exc))]})
    results = [await camera_probe.probe(video, user, secret)]
    if audio:
        sound = await camera_probe.probe(audio, user, secret)
        message = "Âm thanh: có." if sound.has_audio else "Âm thanh: " + (sound.message if not sound.ok else "không có.")
        results.append(camera_probe.ProbeResult(sound.has_audio is True, message))
    return TEMPLATES.TemplateResponse(request, "_camera_probe.html", {"results": results})


@router.post("/cameras", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def add(request: Request, kind: str = Form("ip"), location: str = Form(""), video_url: str = Form(""),
              audio_url: str = Form(""), username: str = Form(""), password: str = Form(""), has_audio: str = Form("")):
    values = _fields(kind, location, video_url, audio_url, username, has_audio)
    try:
        cam = cameras.clean_input(**values, password=password)
    except cameras.CameraError as exc:
        return TEMPLATES.TemplateResponse(request, "_camera_form.html", await form_context(request, kind, values, str(exc)))
    await cameras.add_camera(request.app.state.pool, request.app.state.settings, cam)
    message = f"Đã thêm camera {cam.location}. Camera bắt đầu ghi trong khoảng 2 phút sau khi ghép cặp."
    return TEMPLATES.TemplateResponse(request, "_camera_message.html", {"message": message},
                                      headers={"HX-Refresh": "true"})


@router.post("/cameras/{camera_id}/repair", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def repair(request: Request, camera_id: str):
    try:
        repaired = await cameras.repair_camera(request.app.state.pool, request.app.state.settings, camera_id)
    except cameras.CameraError as exc:  # the stored password cannot be read: nothing was written
        return TEMPLATES.TemplateResponse(request, "_camera_message.html", {"message": str(exc)})
    if not repaired:
        raise HTTPException(status_code=404, detail="unknown or already paired camera")
    return TEMPLATES.TemplateResponse(request, "_camera_message.html",
                                      {"message": "Đã tạo token ghép cặp mới; camera thử ghép cặp lại trong vài giây."})


@router.post("/cameras/{camera_id}/remove", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def remove(request: Request, camera_id: str):
    pool = request.app.state.pool
    listed = {c["id"]: c for c in await cameras.list_cameras(pool)}
    if camera_id not in listed or not await cameras.remove_camera(pool, request.app.state.settings, camera_id):
        raise HTTPException(status_code=404, detail="unknown camera")
    message = f"Đã gỡ camera {listed[camera_id]['location']}. Dữ liệu đã ghi vẫn còn cho tới khi hết hạn hoặc bị Quên."
    return TEMPLATES.TemplateResponse(request, "_camera_message.html", {"message": message},
                                      headers={"HX-Refresh": "true"})

"""The Cài đặt page (spec §10.6): providers (read only), retention, "Quên khoảng thời gian", reprocessing a day,
failed jobs."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from .. import forget, reprocess, store, volume
from ..mcp import tokens as mcp_tokens
from ..pipeline import retention, status
from ..providers.config import TASKS, ProvidersConfigError, load_providers
from ..providers.router import model_version
from .routes import TEMPLATES, require_htmx

TASK_LABELS = {
    "caption": "Mô tả khung hình",
    "asr": "Nhận dạng giọng nói",
    "embed": "Embedding",
    "chapter_summary": "Tóm tắt chương",
    "recap": "Recap cuối ngày",
    "chat": "Hỏi đáp",
}
CHOICE_LABELS = {"captions": "mô tả khung hình", "transcripts": "transcript", "chapters": "chương", "recap": "recap"}

router = APIRouter()


def providers_view(settings) -> dict:
    """Each task's route chain ("ollama:qwen3:8b → …"), the privacy flags, or the configuration error."""
    key_set = settings.anthropic_api_key is not None
    try:
        config = load_providers(settings.providers_file)
    except ProvidersConfigError as exc:
        return {"error": str(exc)[:500], "tasks": [], "privacy": None, "key_set": key_set, "key_missing": False}
    tasks = [
        {"task": task, "label": TASK_LABELS[task],
         "chain": " → ".join(model_version(step) for step in config.tasks[task].chain())}
        for task in TASKS
    ]
    cloud_text = [TASK_LABELS[task] for task in TASKS
                  if key_set and any(step.provider == "anthropic" for step in config.tasks[task].chain())]
    return {"error": None, "tasks": tasks, "privacy": config.privacy, "key_set": key_set,
            "key_missing": "anthropic" in config.providers_used() and not key_set, "cloud_text": cloud_text,
            "timeout_minutes": max(1, round(settings.llm_timeout_s / 60))}


async def settings_context(request: Request) -> dict:
    state = request.app.state
    return {
        "providers": providers_view(state.settings),
        "health": await status.model_health(state.settings, state.http),
        "tz": state.settings.tz,
        "retention": await store.get_retention(state.pool),
        "would_delete": await retention.preview_retention(state.pool),
        "max_days": store.MAX_DAYS,
        "choices": CHOICE_LABELS,
        "mcp": await store.get_mcp(state.pool),
        "mcp_tokens": await mcp_tokens.list_tokens(state.pool),
        "zone": state.settings.zone,
    }


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "settings.html", await settings_context(request))


@router.post("/settings/retention", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def save_retention(
    request: Request, video_days: str = Form(...), audio_days: str = Form(...), enabled: str = Form("")
):
    try:
        chosen = store.Retention(video_days=video_days, audio_days=audio_days, enabled=enabled == "on")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"days must be whole numbers from 1 to {store.MAX_DAYS}") from exc
    await store.set_retention(request.app.state.pool, chosen)
    state = "đang bật: chạy lúc 03:00" if chosen.enabled else "đang tắt: không xoá gì"
    return HTMLResponse(
        f"<span>Đã lưu: giữ video thô {chosen.video_days} ngày, audio thô {chosen.audio_days} ngày."
        f" Dọn dữ liệu cũ {state}.</span>"
    )


@router.post("/settings/reprocess", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def reprocess_day(request: Request, day: dt.date = Form(...), what: list[str] = Form(default=[])):
    try:
        result = await reprocess.reprocess_day(request.app.state.pool, day, request.app.state.settings.zone, set(what))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return TEMPLATES.TemplateResponse(
        request, "_reprocessed.html", {"day": day, "r": result, "what": [CHOICE_LABELS[c] for c in reprocess.CHOICES
                                                                       if c in what]}
    )


def _local_range(start: str, end: str, zone) -> tuple[dt.datetime, dt.datetime]:
    """Two ``datetime-local`` form values (local time, no zone) as an aware range; 422 when unusable."""
    try:
        values = [dt.datetime.fromisoformat(value) for value in (start, end)]
        if any(value.tzinfo is not None for value in values):  # never read an offset as local time
            raise ValueError("the range is in local time, without an offset")
        first, last = (value.replace(tzinfo=zone) for value in values)
        forget.check_range(first, last)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return first, last


@router.post("/settings/forget/preview", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def forget_preview(request: Request, start: str = Form(...), end: str = Form(...)):
    zone = request.app.state.settings.zone
    first, last = _local_range(start, end, zone)
    counts, span = await forget.preview_span(request.app.state.pool, first, last, zone)
    return TEMPLATES.TemplateResponse(
        request, "_forget_preview.html",
        {"start": start, "end": end, "first": first, "last": last, "c": counts,
         "widened": span != (first, last), "span_start": span[0].astimezone(zone), "span_end": span[1].astimezone(zone)},
    )


@router.post("/settings/forget", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def forget_now(request: Request, start: str = Form(...), end: str = Form(...), confirm: str = Form("")):
    if confirm != "yes":
        raise HTTPException(status_code=422, detail="not confirmed")
    state = request.app.state
    first, last = _local_range(start, end, state.settings.zone)
    try:
        counts = await forget.forget_range(state.pool, state.settings, first, last)
    except volume.MediaVolumeMissing:
        return HTMLResponse(f'<p class="err">{MEDIA_MISSING}</p>')
    return TEMPLATES.TemplateResponse(request, "_forgotten.html", {"first": first, "last": last, "c": counts})


MEDIA_MISSING = "Ổ media chưa được gắn, nên chưa thể quên. Hãy gắn lại ổ rồi thử lại."
NAME_REASON = f"Tên token phải có từ 1 tới {mcp_tokens.NAME_MAX} ký tự."


async def _tokens_view(request: Request, *, created: str | None = None, error: str | None = None) -> HTMLResponse:
    """The token list; right after a token is made it is shown once, so the answer must never be cached."""
    state = request.app.state
    return TEMPLATES.TemplateResponse(
        request,
        "_mcp_tokens.html",
        {"mcp_tokens": await mcp_tokens.list_tokens(state.pool), "created": created, "error": error,
         "zone": state.settings.zone},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/settings/mcp/tokens", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def create_mcp_token(request: Request, name: str = Form("")):
    try:
        _, token = await mcp_tokens.create_token(request.app.state.pool, name)
    except mcp_tokens.TokenError:
        return await _tokens_view(request, error=NAME_REASON)
    return await _tokens_view(request, created=token)


@router.post("/settings/mcp/tokens/{token_id}/revoke", response_class=HTMLResponse,
             dependencies=[Depends(require_htmx)])
async def revoke_mcp_token(request: Request, token_id: str):
    if not await mcp_tokens.revoke_token(request.app.state.pool, token_id):
        raise HTTPException(status_code=404, detail="unknown token")
    return await _tokens_view(request)


@router.post("/settings/mcp/images", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def save_mcp_images(request: Request, enabled: str = Form("")):
    chosen = store.McpSettings(images=enabled == "on")
    await store.set_mcp(request.app.state.pool, chosen)
    # One fragment holds the status line and the saved message, so the page never shows two different states.
    return TEMPLATES.TemplateResponse(request, "_mcp_images.html", {"mcp": chosen, "saved": True})

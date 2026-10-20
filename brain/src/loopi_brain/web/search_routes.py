"""The "Tìm kiếm & Chat" page (spec §10.4, §10.6): plain hybrid search, and answers with citations streamed
as Server-Sent Events."""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from markupsafe import Markup, escape
from pydantic import BeforeValidator

from .. import chat, places, search
from ..days import range_utc
from ..pipeline import moments as moment_rules
from .routes import MARKER_LABEL, TEMPLATES, require_htmx

log = logging.getLogger(__name__)
NO_CITATION = "Câu trả lời không trích nguồn nào, có thể không chính xác."

KIND_LABELS = {"visual": "Hình ảnh", "speech": "Lời nói", "marker": "Đánh dấu"}
SEARCH_LIMIT = 30

router = APIRouter()


def _blank_is_none(value):
    """A browser submits an empty date field as ``from=``: no limit. Anything else non-blank must be a date."""
    return (value.strip() or None) if isinstance(value, str) else value


OptionalDate = Annotated[dt.date | None, BeforeValidator(_blank_is_none)]


def hit_view(hit: search.Hit, zone, *, named: bool = False) -> dict:
    """A moment as a search result or a chat source: time, kind, text, thumbnail, link to its Day page, and its place
    when ``named`` (the results come from two or more devices, or two or more cameras are active)."""
    local = hit.ts_start.astimezone(zone)
    text = hit.text
    if hit.kind == "marker":
        text = moment_rules.marker_note(hit.item_meta or {}) or MARKER_LABEL
    return {
        "id": hit.id,
        "kind": hit.kind,
        "kind_label": KIND_LABELS.get(hit.kind, hit.kind),
        "time": local.strftime("%d/%m/%Y %H:%M"),
        "text": text,
        "lang": hit.lang,
        "keyframe_id": hit.keyframe_id,
        "place": hit.location if named else None,
        "link": f"/?day={local.date().isoformat()}#m-{hit.id}",
    }


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = Query(default="", max_length=2000),
    first: Annotated[OptionalDate, Query(alias="from")] = None,
    last: Annotated[OptionalDate, Query(alias="to")] = None,
):
    state = request.app.state
    zone = state.settings.zone
    q = search.clean_query(q)
    result = None
    if q:
        start, end = range_utc(first, last, zone)
        result = await search.search_moments(
            state.pool, search.request_router(state.settings, state.llm), q, start=start, end=end,
            limit=SEARCH_LIMIT, max_distance=state.settings.search_max_distance,
            min_similarity=state.settings.search_min_similarity,
        )
    named = await places.name_places(state.pool, (h.device_id for h in result.hits)) if result else False
    return TEMPLATES.TemplateResponse(
        request,
        "search.html",
        {
            "q": q,
            "first": first,
            "last": last,
            "hits": [hit_view(hit, zone, named=named) for hit in result.hits] if result else None,
            "semantic": result.semantic if result else True,
        },
    )


def render_answer(text: str, sources: list[dict]) -> Markup:
    """The answer as HTML: text escaped, each valid [n] a link to source n, invalid numbers dropped, and a warning
    when nothing is cited."""
    parts: list[str] = []
    position = 0
    for match in chat.CITATION.finditer(text):
        parts.append(str(escape(text[position:match.start()])))
        for n in chat.cited(match.group(0), len(sources)):
            parts.append(f'<a class="cite" href="{escape(sources[n - 1]["link"])}">[{n}]</a>')
        position = match.end()
    parts.append(str(escape(text[position:])))
    html = "<p>" + "".join(parts).replace("\n", "<br>") + "</p>"
    if sources and not chat.cited(text, len(sources)):
        html += f'<p class="muted">{NO_CITATION}</p>'
    return Markup(html)


def sse(event: str, payload: dict) -> str:
    """One Server-Sent Event; the payload is JSON, so it never contains a raw newline."""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/chat", dependencies=[Depends(require_htmx)])
async def chat_answer(request: Request, q: str = Form(..., max_length=2000)):
    state = request.app.state
    zone = state.settings.zone
    router_ = search.request_router(state.settings, state.llm)

    async def events():
        sources: list[dict] = []
        try:
            async for event, payload in chat.chat_events(state.pool, router_, q, state.settings):
                if event == "sources":
                    sources = [hit_view(hit, zone, named=payload["named"]) for hit in payload["hits"]]
                    payload = {"html": TEMPLATES.get_template("_sources.html").render(hits=sources)}
                elif event == "answer":
                    payload = {"html": str(render_answer(payload["text"], [] if payload["not_found"] else sources))}
                yield sse(event, payload)
        except Exception:
            log.exception("chat answer failed")
            yield sse("error", {"text": "Có lỗi khi trả lời, thử lại."})
            yield sse("done", {})

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

"""The "Tìm kiếm & Chat" page (spec §10.4, §10.6): plain hybrid search here; cited chat answers in Task 8."""
from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BeforeValidator

from .. import search
from ..days import day_bounds_utc
from ..pipeline import moments as moment_rules
from .routes import MARKER_LABEL, TEMPLATES

KIND_LABELS = {"visual": "Hình ảnh", "speech": "Lời nói", "marker": "Đánh dấu"}
SEARCH_LIMIT = 30

router = APIRouter()


def _blank_is_none(value):
    """A browser submits an empty date field as ``from=``: no limit. Anything else non-blank must be a date."""
    return (value.strip() or None) if isinstance(value, str) else value


OptionalDate = Annotated[dt.date | None, BeforeValidator(_blank_is_none)]


def hit_view(hit: search.Hit, zone) -> dict:
    """A moment as a search result or a chat source: time, kind, text, thumbnail, link to its Day page."""
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
        "link": f"/?day={local.date().isoformat()}#m-{hit.id}",
    }


def range_utc(first: dt.date | None, last: dt.date | None, zone) -> tuple[dt.datetime | None, dt.datetime | None]:
    """Local days [first, last] (either optional) as a UTC range [start, end)."""
    start = day_bounds_utc(first, zone)[0] if first else None
    end = day_bounds_utc(last, zone)[1] if last else None
    return start, end


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
    return TEMPLATES.TemplateResponse(
        request,
        "search.html",
        {
            "q": q,
            "first": first,
            "last": last,
            "hits": [hit_view(hit, zone) for hit in result.hits] if result else None,
            "semantic": result.semantic if result else True,
        },
    )

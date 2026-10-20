"""Hybrid retrieval over moments (spec §10.4 step 2).

Two searches, each limited to the time filter: pgvector cosine distance between the question's embedding and
the moments' embeddings (meaning, across languages: a Vietnamese question finds an English caption), and
pg_trgm word similarity between the question and the moments' text (exact words, names, moments without an
embedding yet). Each keeps only results within its threshold; the two lists are merged by reciprocal rank
fusion. No result means nothing relevant: the caller says "không tìm thấy" instead of guessing.
"""
from __future__ import annotations

import datetime as dt
import logging
import unicodedata
from dataclasses import dataclass

import httpx
from pgvector import Vector
from psycopg_pool import AsyncConnectionPool

from .config import Settings
from .providers.base import ProviderError
from .providers.config import ProvidersConfigError, load_providers
from .providers.router import Router, build_router

log = logging.getLogger(__name__)
# Qwen3-Embedding: queries carry an instruction, documents do not (P2a embeds moments without one).
QUERY_INSTRUCTION = "Instruct: Given a question about a personal diary, retrieve the moments that answer it\nQuery: "
CANDIDATES = 40  # per search, before the merge
RRF_K = 60
QUERY_MAX = 500  # characters of a question that are searched
_COLUMNS = (
    "m.id, m.kind, m.ts_start, m.text, m.lang, m.keyframe_id, m.item_id, i.meta AS item_meta, m.device_id,"
    " d.location"
)
_FILTER = "(%(start)s::timestamptz IS NULL OR m.ts_start >= %(start)s) AND (%(end)s::timestamptz IS NULL OR m.ts_start < %(end)s)"


@dataclass(frozen=True)
class Hit:
    id: str
    kind: str
    ts_start: dt.datetime
    text: str
    lang: str | None
    keyframe_id: str | None
    item_id: str | None
    item_meta: dict | None
    distance: float | None  # cosine distance to the question (None: found by words only)
    similarity: float | None  # word similarity (None: found by meaning only)
    score: float
    device_id: str | None = None  # the device that recorded the moment
    location: str | None = None  # where that device stands (D34)


@dataclass(frozen=True)
class SearchResult:
    hits: list[Hit]
    semantic: bool  # False when the question could not be embedded (model server down): words only


def clean_query(text: str) -> str:
    """A question as it is searched: control characters (NUL, which PostgreSQL text cannot hold, escapes, zero-width
    marks) become spaces, whitespace is collapsed, at most QUERY_MAX characters are kept. May be empty."""
    spaced = "".join(" " if unicodedata.category(c)[0] == "C" and not c.isspace() else c for c in text)
    return " ".join(spaced.split())[:QUERY_MAX]


def request_router(settings: Settings, client: httpx.AsyncClient) -> Router | None:
    """A router for the api process (search, chat); None if providers.yaml is invalid."""
    try:
        return build_router(load_providers(settings.providers_file), settings, client)
    except ProvidersConfigError as exc:
        log.warning("search and chat run without models: %s", exc)
        return None


async def _by_meaning(pool, router: Router, query: str, params: dict, max_distance: float) -> list[dict] | None:
    try:
        result = await router.embed("embed", [QUERY_INSTRUCTION + query])
    except ProviderError as exc:
        log.warning("searching by words only: the question could not be embedded: %s", exc)
        return None
    async with pool.connection() as conn, conn.transaction():
        # With a time filter, keep scanning the HNSW index until enough rows pass the filter (pgvector >= 0.8).
        await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        cur = await conn.execute(
            f"SELECT {_COLUMNS}, (m.embedding <=> %(vector)s)::float8 AS distance FROM moment m"
            f" LEFT JOIN item i ON i.id = m.item_id LEFT JOIN device d ON d.id = m.device_id WHERE m.embedding IS NOT NULL AND {_FILTER}"
            " ORDER BY m.embedding <=> %(vector)s LIMIT %(limit)s",
            {**params, "vector": Vector(result.vectors[0])},
        )
        rows = await cur.fetchall()
    return sorted((r for r in rows if r["distance"] <= max_distance), key=lambda r: (r["distance"], r["id"]))


async def _by_words(pool, query: str, params: dict, min_similarity: float) -> list[dict]:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)", (str(min_similarity),))
        cur = await conn.execute(
            f"SELECT {_COLUMNS}, word_similarity(%(query)s, m.text)::float8 AS similarity FROM moment m"
            f" LEFT JOIN item i ON i.id = m.item_id LEFT JOIN device d ON d.id = m.device_id WHERE %(query)s <%% m.text AND {_FILTER}"
            " ORDER BY similarity DESC, m.ts_start DESC, m.id LIMIT %(limit)s",
            {**params, "query": query},
        )
        return await cur.fetchall()


async def search_moments(
    pool: AsyncConnectionPool,
    router: Router | None,
    query: str,
    *,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    limit: int = 8,
    max_distance: float = 0.6,
    min_similarity: float = 0.3,
    candidates: int = CANDIDATES,
) -> SearchResult:
    """The ``limit`` most relevant moments for ``query`` with ts_start in [start, end) (either bound optional);
    each of the two searches keeps its ``candidates`` best before they are merged."""
    query = clean_query(query)
    if not query:
        return SearchResult([], semantic=router is not None)
    params = {"start": start, "end": end, "limit": candidates}
    meaning = await _by_meaning(pool, router, query, params, max_distance) if router is not None else None
    words = await _by_words(pool, query, params, min_similarity)
    rows: dict[str, dict] = {}
    scores: dict[str, float] = {}
    for found in (meaning or [], words):
        for rank, row in enumerate(found, start=1):
            rows.setdefault(row["id"], {}).update(row)
            scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (RRF_K + rank)
    ordered = sorted(scores, key=lambda moment_id: (-scores[moment_id], rows[moment_id]["ts_start"], moment_id))
    hits = [
        Hit(
            id=moment_id, kind=row["kind"], ts_start=row["ts_start"], text=row["text"], lang=row["lang"],
            keyframe_id=row["keyframe_id"], item_id=row["item_id"], item_meta=row["item_meta"],
            distance=row.get("distance"), similarity=row.get("similarity"), score=scores[moment_id],
            device_id=row["device_id"], location=row["location"],
        )
        for moment_id in ordered[:limit]
        for row in (rows[moment_id],)
    ]
    return SearchResult(hits, semantic=meaning is not None)

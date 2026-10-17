"""The read-only MCP server (spec §10.5, P3 design §3): the official SDK's MCPServer speaking Streamable HTTP,
stateless with JSON answers, served at exactly ``/mcp`` inside the api process behind a bearer-token gate.

This module deliberately does not use ``from __future__ import annotations``: the SDK reads the tools' annotations
when they are registered, to build their input and output schemas."""
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import httpx
from fastapi import FastAPI
from mcp.server import ServerRequestContext
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import search, store
from ..config import Settings
from . import memory, tokens

log = logging.getLogger(__name__)

PATH = "/mcp"
INVALID_ARGUMENTS = "invalid arguments"
INTERNAL_ERROR = "internal error"
SDK_TOOL_ERROR = "Error executing tool "  # how the SDK starts the errors it writes for a tool call
SEARCH_LIMIT = 10
INSTRUCTIONS = (
    "Loopi is a private, local-first diary recorded at home by a fixed camera and microphone. These read-only tools "
    "return its moments (picture captions, speech transcripts, marked moments), the day's chapters and recap, and the "
    "commitments found in conversations. Cite the moment_id of every moment you use. Times are local with an offset."
)
UNAUTHORIZED = json.dumps({"error": "unauthorized"}).encode()
NOT_ALLOWED = json.dumps({"error": "method_not_allowed"}).encode()

# The SDK logs tool failures with their text (INFO) under ``mcp``, which may carry a question or a memory: keep it at
# WARNING. (Its client side logs under the top-level ``client`` logger and never runs in the api; the tests that run it
# in process check every log record.) The tool wrappers below log only tool names, durations, result counts and error
# classes, which are safe by construction, so this module's logger stays at INFO: ``serve`` keeps the root logger at
# WARNING for everything else, and the owner still sees what the cloud client read.
logging.getLogger("mcp").setLevel(logging.WARNING)
log.setLevel(logging.INFO)


@dataclass(frozen=True)
class Deps:
    pool: AsyncConnectionPool
    settings: Settings
    llm: httpx.AsyncClient  # the model client search_memories embeds questions with


def _result(model: BaseModel, picture: bytes | None = None) -> CallToolResult:
    content: list[Any] = [TextContent(type="text", text=model.model_dump_json())]
    if picture is not None:
        content.append(Image(data=picture, format="jpeg").to_image_content())
    return CallToolResult(content=content, structured_content=model.model_dump(mode="json"))


def _error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def _item_count(result: CallToolResult) -> int:
    """How many items an ok result carries, for the log line: the length of its main list, 1 for a recap or a moment."""
    structured = result.structured_content or {}
    for key in ("moments", "chapters", "commitments"):
        if key in structured:
            return len(structured[key])
    return 1


async def _run(name: str, call: Callable[[], Awaitable[CallToolResult]]) -> CallToolResult:
    """Run one tool body. NotFound and BadInput become tool errors with their fixed text, anything else
    INTERNAL_ERROR; only the tool's name, its time, an ok result's item count and an error's class are logged, never an
    argument or a result."""
    started = time.monotonic()
    try:
        result = await call()
    except (memory.NotFound, memory.BadInput) as exc:
        log.info("mcp %s: %s in %.2f s", name, type(exc).__name__, time.monotonic() - started)
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001 - the client gets a fixed text; the class is logged, not the message
        log.error("mcp %s failed: %s", name, type(exc).__name__)
        return _error(INTERNAL_ERROR)
    log.info("mcp %s ok in %.2f s (%d items)", name, time.monotonic() - started, _item_count(result))
    return result


async def hide_validation_input(ctx: ServerRequestContext[Any, Any], call_next: Any) -> Any:
    """Our tools answer every error themselves (``_run``), never with a text starting ``Error executing tool ``; such
    a text is the SDK's own. Its argument-validation errors (one or several: "1 validation error for …", "2 validation
    errors for …") quote the values that were sent, so they become INVALID_ARGUMENTS; any other SDK tool error
    becomes INTERNAL_ERROR. ``Unknown tool: …`` names only the tool and is left as it is."""
    result = await call_next(ctx)
    if ctx.method == "tools/call" and isinstance(result, dict) and result.get("isError") is True:
        text = (result.get("content") or [{}])[0].get("text", "")
        if text.startswith(SDK_TOOL_ERROR):
            fixed = INVALID_ARGUMENTS if " validation error" in text else INTERNAL_ERROR
            return {**result, "content": [{"type": "text", "text": fixed}]}
    return result


def build_server(deps: Deps) -> MCPServer:
    server = MCPServer(name="loopi", instructions=INSTRUCTIONS, middleware=[hide_validation_input])
    zone = deps.settings.zone

    @server.tool()
    async def search_memories(
        query: Annotated[str, Field(description="What to look for, in any language (the first 500 characters count).")],
        start: Annotated[str | None, Field(description="Earliest moment: YYYY-MM-DD (from that local day) or an ISO 8601 date-time (local when it has no offset).")] = None,
        end: Annotated[str | None, Field(description="Latest moment: YYYY-MM-DD (that local day included) or an ISO 8601 date-time.")] = None,
        limit: Annotated[int | None, Field(ge=1, le=50, description="How many moments to return.")] = SEARCH_LIMIT,
    ) -> Annotated[CallToolResult, memory.SearchOut]:
        """Search the owner's memories (picture captions, speech, marked moments) by meaning and by words. Cite moment_id."""

        async def call() -> CallToolResult:
            router = search.request_router(deps.settings, deps.llm)
            count = limit if limit is not None else SEARCH_LIMIT  # some models send null for an optional argument
            return _result(await memory.search_memories(deps.pool, router, deps.settings, query, start, end, count))

        return await _run("search_memories", call)

    @server.tool()
    async def get_timeline(
        date: Annotated[str, Field(description="The local day, YYYY-MM-DD.")],
    ) -> Annotated[CallToolResult, memory.TimelineOut]:
        """The chapters of one day (title, summary, times), its marked moments and how many moments of each kind it has."""
        return await _run("get_timeline", lambda: _as_result(memory.get_timeline(deps.pool, date, zone)))

    @server.tool()
    async def get_day_recap(
        date: Annotated[str, Field(description="The local day, YYYY-MM-DD.")],
    ) -> Annotated[CallToolResult, memory.RecapOut]:
        """The end-of-day recap of one day, as Vietnamese markdown (main events, memorable moments, commitments)."""
        return await _run("get_day_recap", lambda: _as_result(memory.get_day_recap(deps.pool, date, zone)))

    @server.tool()
    async def get_moment(
        moment_id: Annotated[str, Field(description="A moment_id from search_memories or get_timeline.")],
    ) -> Annotated[CallToolResult, memory.MomentOut]:
        """One moment: its text, times, language and picture caption; its keyframe picture only if the owner allows it."""

        async def call() -> CallToolResult:
            images = (await store.get_mcp(deps.pool)).images
            out, picture = await memory.get_moment(deps.pool, deps.settings, moment_id, images)
            return _result(out, picture)

        return await _run("get_moment", call)

    @server.tool()
    async def list_commitments(
        status: Annotated[Literal["open", "done", "dismissed"] | None, Field(description="Only this status; all when omitted.")] = None,
    ) -> Annotated[CallToolResult, memory.CommitmentsOut]:
        """Things to do or promised, found in conversations, newest first (at most 200)."""
        return await _run("list_commitments", lambda: _as_result(memory.list_commitments(deps.pool, status)))

    return server


async def _as_result(read: Awaitable[BaseModel]) -> CallToolResult:
    return _result(await read)


async def _send_json(send: Send, status: int, body: bytes, extra: list[tuple[bytes, bytes]]) -> None:
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *extra]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class BearerGate:
    """Every request to /mcp: a live bearer token, or 401 with a fixed body before any MCP code runs; then POST only
    (stateless JSON needs nothing else, and a GET would hold an idle event stream open)."""

    def __init__(self, app: ASGIApp, pool: AsyncConnectionPool) -> None:
        self.app, self.pool = app, pool

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        scheme, _, token = raw.partition(" ")
        token = token.strip()
        token_id = await tokens.check_token(self.pool, token) if scheme.lower() == "bearer" and token else None
        if token_id is None:
            await _send_json(send, 401, UNAUTHORIZED, [(b"www-authenticate", b"Bearer")])
            return
        if scope["method"] != "POST":
            await _send_json(send, 405, NOT_ALLOWED, [(b"allow", b"POST")])
            return
        await self.app({**scope, "state": {**scope.get("state", {}), "mcp_token_id": token_id}}, receive, send)


def mount(app: FastAPI, deps: Deps) -> MCPServer:
    """Serve the MCP server at exactly PATH: a Starlette Route, because a mount would redirect ``POST /mcp`` to
    ``/mcp/``. The app's lifespan must run ``server.session_manager.run()`` (once) around serving."""
    server = build_server(deps)
    asgi = server.streamable_http_app(
        streamable_http_path=PATH,
        stateless_http=True,
        json_response=True,
        # Behind Caddy every request carries the LAN Host header, which the SDK's DNS-rebinding check would refuse
        # (421); the bearer gate, not the Host header, decides who may call.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    app.router.routes.append(Route(PATH, endpoint=BearerGate(asgi, deps.pool)))
    return server

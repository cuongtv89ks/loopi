import asyncio
import base64
import contextlib
import datetime as dt
import io
import logging

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from PIL import Image

from helpers import FakeOllama, captioned_keyframe, fetch_all, insert_moment, paired_device
from loopi_brain import store
from loopi_brain.app import create_app
from loopi_brain.mcp import memory, tokens

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
TOOLS = {"search_memories", "get_timeline", "get_day_recap", "get_moment", "list_commitments"}
SECRET = "SECRET-7f3a"


@contextlib.asynccontextmanager
async def running(app):
    """Run the app's lifespan in one task. The MCP session manager's task group must be left by the task that
    entered it, and pytest-asyncio runs a fixture's setup and teardown in different tasks."""
    started, stop = asyncio.Event(), asyncio.Event()

    async def run():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(run())
    waiter = asyncio.create_task(started.wait())
    await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        waiter.cancel()
        task.result()  # the lifespan failed to start: re-raise its error
    try:
        yield app
    finally:
        stop.set()
        await task


@pytest.fixture
async def app(pool, pki_settings):
    ollama = FakeOllama()
    application = create_app(pki_settings, pool=pool, http_client=ollama.client(), llm_client=ollama.client())
    async with running(application):
        yield application


@pytest.fixture
async def token(pool):
    _, value = await tokens.create_token(pool, "test")
    return value


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@contextlib.asynccontextmanager
async def mcp_client(app, token, mode="auto"):
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers={"Authorization": f"Bearer {token}"})
    async with http, Client(streamable_http_client("https://brain.test/mcp", http_client=http), mode=mode) as client:
        yield client


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_the_five_tools_are_listed_with_their_output_schemas(app, token, mode):
    async with mcp_client(app, token, mode) as client:
        listed = (await client.list_tools()).tools
    assert {t.name for t in listed} == TOOLS
    assert all(t.output_schema for t in listed) and all(t.description for t in listed)


async def test_search_answers_moments_that_cite_their_ids(app, token, dev, pool):
    moment = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, axis=0, lang="vi", text="đi chợ")
    async with mcp_client(app, token) as client:
        result = await client.call_tool("search_memories", {"query": "đi chợ", "start": "2026-10-14", "end": "2026-10-14"})
    assert result.is_error is False
    assert [m["moment_id"] for m in result.structured_content["moments"]] == [moment]
    assert result.structured_content["moments"][0]["ts_start"] == "2026-10-14T09:00:00+09:00"


async def test_every_tool_answers_for_a_day(app, token, dev, pool):
    marker = await insert_moment(pool, dev["device_id"], kind="marker", ts=NINE, text="Đánh dấu")
    async with mcp_client(app, token) as client:
        timeline = await client.call_tool("get_timeline", {"date": "2026-10-14"})
        recap = await client.call_tool("get_day_recap", {"date": "2026-10-14"})
        moment = await client.call_tool("get_moment", {"moment_id": marker})
        todo = await client.call_tool("list_commitments", {"status": "open"})
    assert timeline.structured_content["markers"][0]["moment_id"] == marker
    assert recap.structured_content["exists"] is False
    assert (moment.structured_content["text"], moment.structured_content["image"]) == ("Đánh dấu", False)
    assert todo.structured_content == {"commitments": []}
    assert not any(r.is_error for r in (timeline, recap, moment, todo))


async def test_get_moment_sends_a_picture_only_when_the_owner_allows_it(app, token, dev, pool, pki_settings):
    keyframe = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
    moment = await insert_moment(pool, dev["device_id"], kind="visual", ts=NINE, text="x", keyframe_id=keyframe)
    async with mcp_client(app, token) as client:
        off = await client.call_tool("get_moment", {"moment_id": moment})
        await store.set_mcp(pool, store.McpSettings(images=True))
        on = await client.call_tool("get_moment", {"moment_id": moment})
    assert [c.type for c in off.content] == ["text"] and off.structured_content["image"] is False
    assert [c.type for c in on.content] == ["text", "image"] and on.structured_content["image"] is True
    assert on.content[1].mime_type == "image/jpeg"
    with Image.open(io.BytesIO(base64.b64decode(on.content[1].data))) as img:
        assert img.width == 320


@pytest.mark.parametrize(("tool", "args", "text"), [
    ("get_moment", {"moment_id": f"01{SECRET}"}, "no such moment"),
    ("get_timeline", {"date": f"2026-13-45-{SECRET}"}, "date must be YYYY-MM-DD"),
    ("search_memories", {"query": "x", "start": f"{SECRET}"}, "start and end must be YYYY-MM-DD or ISO 8601 date-times"),
    ("search_memories", {"query": " "}, "query is empty"),
    ("search_memories", {"query": SECRET, "limit": 1000}, "invalid arguments"),
    ("list_commitments", {"status": SECRET}, "invalid arguments"),
    ("get_moment", {}, "invalid arguments"),
])
async def test_errors_are_fixed_texts_that_never_repeat_the_input(app, token, tool, args, text):
    async with mcp_client(app, token) as client:
        result = await client.call_tool(tool, args)
    assert result.is_error is True and [c.text for c in result.content] == [text]


async def test_a_crash_inside_a_tool_is_an_internal_error_without_its_text_in_the_logs(app, token, monkeypatch, caplog):
    async def broken(*_args, **_kwargs):
        raise RuntimeError(f"connection lost while reading {SECRET}")

    monkeypatch.setattr(memory, "get_timeline", broken)
    caplog.set_level(logging.DEBUG)
    async with mcp_client(app, token) as client:
        result = await client.call_tool("get_timeline", {"date": "2026-10-14"})
    assert result.is_error is True and [c.text for c in result.content] == ["internal error"]
    assert SECRET not in caplog.text and "RuntimeError" in caplog.text


async def test_questions_and_answers_never_reach_the_logs(app, token, dev, pool, caplog):
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, axis=0, text=f"nói {SECRET} hôm nay")
    caplog.set_level(logging.DEBUG)
    async with mcp_client(app, token) as client:
        result = await client.call_tool("search_memories", {"query": f"{SECRET} hôm nay"})
    assert result.structured_content["moments"]  # the secret was searched and found …
    assert SECRET not in caplog.text  # … and appears in no log record, from the server, the SDK or its client
    assert "mcp search_memories ok" in caplog.text


async def test_the_gate_refuses_before_any_mcp_code_runs(app, token, pool):
    revoked_id, revoked = await tokens.create_token(pool, "old")
    await tokens.revoke_token(pool, revoked_id)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://brain.test") as c:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": token},
                        {"Authorization": f"Basic {token}"}, {"Authorization": f"Bearer {revoked}"}):
            r = await c.post("/mcp", json=body, headers=headers)
            assert (r.status_code, r.json()) == (401, {"error": "unauthorized"})
            assert r.headers["www-authenticate"] == "Bearer"
        r = await c.get("/mcp", headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"})
        assert (r.status_code, r.headers["allow"]) == (405, "POST")
        assert (await c.get("/mcp")).status_code == 401
    assert all(t["last_used_at"] is None for t in await tokens.list_tokens(pool) if t["id"] == revoked_id)


async def test_a_good_call_marks_its_token_used(app, token, pool):
    async with mcp_client(app, token) as client:
        await client.list_tools()
    [row] = await fetch_all(pool, "SELECT last_used_at FROM mcp_token WHERE name = 'test'")
    assert row["last_used_at"] is not None


async def test_the_web_pages_still_answer_next_to_mcp(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://brain.test") as c:
        assert (await c.get("/healthz")).json() == {"status": "ok"}
        assert (await c.get("/settings")).status_code == 200

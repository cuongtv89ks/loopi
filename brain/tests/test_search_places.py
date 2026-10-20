import datetime as dt
import json

import httpx
import pytest

from helpers import FakeOllama, insert_moment, paired_device
from loopi_brain import chat, places, search
from loopi_brain.app import create_app
from loopi_brain.mcp import memory

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)


@pytest.fixture
def ollama():
    return FakeOllama()


@pytest.fixture
async def web(pool, pki_settings, jobs_open, ollama):
    """The app with a fake model server, as in tests/test_search.py (moments without embeddings are found by words)."""
    app = create_app(pki_settings, pool=pool, http_client=ollama.client(), llm_client=ollama.client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


@pytest.fixture
async def two(pool, pki_settings):
    living = await paired_device(pool, pki_settings)
    kitchen = await paired_device(pool, pki_settings)
    await places.set_location(pool, living["device_id"], "Phòng khách")
    await places.set_location(pool, kitchen["device_id"], "Bếp")
    return living["device_id"], kitchen["device_id"]


async def test_a_hit_carries_its_device_and_location(pool, two):
    living, _ = two
    moment = await insert_moment(pool, living, kind="speech", ts=NINE, text="đi chợ mua rau", lang="vi")
    [hit] = (await search.search_moments(pool, None, "đi chợ mua rau")).hits
    assert (hit.id, hit.device_id, hit.location) == (moment, living, "Phòng khách")


async def test_the_search_page_tags_results_from_several_cameras(web, pool, two):
    living, kitchen = two
    await insert_moment(pool, living, kind="speech", ts=NINE, text="đi chợ mua rau", lang="vi")
    await insert_moment(pool, kitchen, kind="speech", ts=NINE + dt.timedelta(minutes=1), text="đi chợ mua cá",
                        lang="vi")
    text = (await web.get("/search", params={"q": "đi chợ"})).text
    assert '<span class="place">Phòng khách</span>' in text and '<span class="place">Bếp</span>' in text


async def test_results_from_one_camera_are_not_tagged(web, pool, two):
    living, _ = two
    await insert_moment(pool, living, kind="speech", ts=NINE, text="đi chợ mua rau", lang="vi")
    assert 'class="place"' not in (await web.get("/search", params={"q": "đi chợ"})).text


def test_chat_sources_name_the_place_when_there_are_several(pki_settings):
    def hit(n, device, location):
        return search.Hit(f"M{n}", "speech", NINE, "đi chợ", "vi", None, None, None, None, None, 0.0,
                          device_id=device, location=location)

    several = chat.source_lines([hit(1, "A", "Phòng khách"), hit(2, "B", "Bếp")], pki_settings.zone)
    assert several == ["[1] 2026-10-14 09:00 at Phòng khách, speech (vi): đi chợ",
                       "[2] 2026-10-14 09:00 at Bếp, speech (vi): đi chợ"]
    single = chat.source_lines([hit(1, "A", "Phòng khách")], pki_settings.zone)
    assert single == ["[1] 2026-10-14 09:00, speech (vi): đi chợ"]


async def test_mcp_results_carry_the_location(pool, pki_settings, two):
    living, kitchen = two
    speech = await insert_moment(pool, living, kind="speech", ts=NINE, text="đi chợ mua rau", lang="vi")
    marker = await insert_moment(pool, kitchen, kind="marker", ts=NINE + dt.timedelta(minutes=1), text="x")
    found = await memory.search_memories(pool, None, pki_settings, "đi chợ mua rau", None, None, 10)
    assert [(m.moment_id, m.location) for m in found.moments] == [(speech, "Phòng khách")]
    timeline = await memory.get_timeline(pool, "2026-10-14", pki_settings.zone)
    assert [(m.moment_id, m.location) for m in timeline.markers] == [(marker, "Bếp")]
    one, _ = await memory.get_moment(pool, pki_settings, speech, images=False)
    assert one.location == "Phòng khách"


async def test_the_chat_endpoint_tags_its_sources_and_names_the_places_to_the_model(web, pool, two, ollama):
    living, kitchen = two
    await insert_moment(pool, living, kind="speech", ts=NINE, text="đi chợ mua rau", lang="vi")
    await insert_moment(pool, kitchen, kind="speech", ts=NINE + dt.timedelta(minutes=1), text="đi chợ mua cá",
                        lang="vi")
    ollama.reply = lambda body: "Bạn đi chợ [1][2]."
    async with web.stream("POST", "/chat", data={"q": "đi chợ"}, headers={"HX-Request": "true"}) as r:
        body = (await r.aread()).decode()
    sources = next(json.loads(block.split("data: ", 1)[1]) for block in body.split("\n\n")
                   if block.startswith("event: sources"))
    html = sources["html"]
    assert '<span class="place">Phòng khách</span>' in html and '<span class="place">Bếp</span>' in html
    [answer] = [b for path, b in ollama.requests if path == "/api/chat" and b["stream"]]
    prompt = answer["messages"][-1]["content"]
    assert "at Phòng khách, speech (vi): đi chợ mua rau" in prompt and "at Bếp, speech (vi): đi chợ mua cá" in prompt

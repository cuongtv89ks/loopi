import datetime as dt

import httpx
import pytest

from helpers import FakeOllama, insert_item, insert_keyframe, insert_moment, paired_device
from loopi_brain import search
from loopi_brain.app import create_app
from loopi_brain.config import Settings
from loopi_brain.providers.config import DEFAULT_CONFIG
from loopi_brain.providers.router import build_router

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
# FakeOllama embeds the question as the unit vector on axis 0: moments on axis 0 mean the same thing.


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def ollama():
    return FakeOllama()


@pytest.fixture
def router(pki_settings, ollama):
    return build_router(DEFAULT_CONFIG, pki_settings, ollama.client())


@pytest.fixture
async def web(pool, pki_settings, jobs_open, ollama):
    app = create_app(pki_settings, pool=pool, http_client=ollama.client(), llm_client=ollama.client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


async def _memories(pool, device_id) -> dict:
    return {
        "kitchen": await insert_moment(pool, device_id, kind="visual", ts=NINE, axis=0, lang="en",
                                       text="Two people cook noodles in the kitchen"),
        "market": await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=5), axis=1,
                                      lang="vi", text="Mai mình đi chợ mua rau nhé"),
        "both": await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=9), axis=0,
                                    lang="vi", text="Nấu mì xong rồi đi chợ"),
        "other": await insert_moment(pool, device_id, kind="visual", ts=NINE + dt.timedelta(days=1), axis=2,
                                     lang="en", text="An empty room at night"),
    }


async def test_meaning_and_words_are_both_searched_and_merged(pool, dev, router, ollama):
    ids = await _memories(pool, dev["device_id"])
    result = await search.search_moments(pool, router, "đi chợ")
    assert result.semantic is True
    assert [h.id for h in result.hits] == [ids["both"], ids["kitchen"], ids["market"]]  # found twice ranks first
    both, kitchen, market = result.hits
    assert both.distance == pytest.approx(0.0) and both.similarity == pytest.approx(1.0)
    assert kitchen.similarity is None and market.distance is None
    [(_, body)] = [r for r in ollama.requests if r[0] == "/api/embed"]
    assert body["input"] == [search.QUERY_INSTRUCTION + "đi chợ"] and body["model"] == "qwen3-embedding:0.6b"


async def test_a_time_range_limits_both_searches(pool, dev, router):
    ids = await _memories(pool, dev["device_id"])
    tomorrow = NINE + dt.timedelta(days=1)
    result = await search.search_moments(pool, router, "đi chợ", start=tomorrow - dt.timedelta(hours=9))
    assert result.hits == []
    result = await search.search_moments(pool, router, "empty room", start=tomorrow - dt.timedelta(hours=9))
    assert [h.id for h in result.hits] == [ids["other"]]
    result = await search.search_moments(pool, router, "đi chợ", end=NINE + dt.timedelta(minutes=6))
    assert [h.id for h in result.hits] == [ids["kitchen"], ids["market"]]


async def test_without_the_embedding_model_words_still_find_moments(pool, dev, router, ollama):
    ids = await _memories(pool, dev["device_id"])
    ollama.down = True
    result = await search.search_moments(pool, router, "đi chợ")
    assert result.semantic is False and [h.id for h in result.hits] == [ids["both"], ids["market"]]
    result = await search.search_moments(pool, None, "đi chợ")  # providers.yaml unusable
    assert result.semantic is False and len(result.hits) == 2


async def test_nothing_relevant_finds_nothing(pool, dev, router):
    await insert_moment(pool, dev["device_id"], kind="visual", ts=NINE, axis=3, text="A cat sleeps on the sofa")
    assert (await search.search_moments(pool, router, "khám răng")).hits == []


async def test_an_empty_question_searches_nothing(pool, dev, router, ollama):
    await _memories(pool, dev["device_id"])
    assert (await search.search_moments(pool, router, "   \n ")).hits == [] and ollama.requests == []


async def test_moments_without_an_embedding_are_found_by_their_words(pool, dev, router):
    waiting = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, lang="vi",
                                  text="Chiều nay đón con lúc năm giờ")
    assert [h.id for h in (await search.search_moments(pool, router, "đón con")).hits] == [waiting]


async def test_thresholds_decide_what_is_relevant(pool, dev, router):
    ids = await _memories(pool, dev["device_id"])
    result = await search.search_moments(pool, router, "đi chợ", max_distance=2.0, min_similarity=0.99)
    assert {h.id for h in result.hits} == set(ids.values())  # every embedded moment is close enough at 2
    result = await search.search_moments(pool, router, "đi chợ", limit=1)
    assert [h.id for h in result.hits] == [ids["both"]]


async def test_the_search_page_lists_time_kind_thumbnail_and_a_link_to_the_day(web, pool, pki_settings, dev):
    device_id = dev["device_id"]
    video = await insert_item(pool, device_id, kind="video", started_at=NINE)
    kf = await insert_keyframe(pool, pki_settings, video, device_id, NINE)
    moment = await insert_moment(pool, device_id, kind="visual", ts=NINE, axis=0, lang="en",
                                 text="A <b>birthday</b> cake on the table")
    async with pool.connection() as conn:
        await conn.execute("UPDATE moment SET keyframe_id = %s WHERE id = %s", (kf, moment))
    text = (await web.get("/search", params={"q": "sinh nhật"})).text
    assert '<a href="/search">Tìm kiếm</a>' in text and 'value="sinh nhật"' in text
    assert f'<a href="/?day=2026-10-14#m-{moment}"><img src="/thumb/{kf}.jpg"' in text
    assert "<time>14/10/2026 09:00</time></a> · Hình ảnh" in text
    assert "A &lt;b&gt;birthday&lt;/b&gt; cake on the table" in text


async def test_the_search_page_says_when_nothing_is_found_and_keeps_the_dates(web, pool, dev):
    await _memories(pool, dev["device_id"])
    text = (await web.get("/search", params={"q": "đi chợ", "from": "2026-10-15", "to": "2026-10-15"})).text
    assert "Không tìm thấy khoảnh khắc nào." in text
    assert 'name="from" value="2026-10-15"' in text and 'name="to" value="2026-10-15"' in text
    text = (await web.get("/search", params={"q": "đi chợ", "from": "2026-10-14", "to": "2026-10-14"})).text
    assert text.count('class="hit"') == 3
    assert (await web.get("/search", params={"q": "x", "from": "hôm qua"})).status_code == 422


async def test_the_search_page_without_a_question_shows_the_form(web, ollama):
    text = (await web.get("/search")).text
    assert 'name="q"' in text and "Không tìm thấy khoảnh khắc" not in text and ollama.requests == []


async def test_a_stopped_model_server_is_explained(web, pool, dev, ollama):
    await _memories(pool, dev["device_id"])
    ollama.down = True
    text = (await web.get("/search", params={"q": "đi chợ"})).text
    assert "Đang chỉ tìm theo chữ" in text and text.count('class="hit"') == 2


def test_search_thresholds_are_settings(monkeypatch):
    s = Settings(database_url="postgresql://x")
    assert (s.search_max_distance, s.search_min_similarity) == (0.6, 0.3)
    monkeypatch.setenv("LOOPI_DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("LOOPI_SEARCH_MAX_DISTANCE", "0.45")
    assert Settings().search_max_distance == 0.45


async def test_control_characters_in_a_question_never_reach_the_database(pool, dev, router, ollama):
    ids = await _memories(pool, dev["device_id"])
    result = await search.search_moments(pool, router, "đi\x00 chợ\x07")  # NUL, BEL: spaces, not errors
    assert [h.id for h in result.hits] == [ids["both"], ids["kitchen"], ids["market"]]
    [(_, body)] = [r for r in ollama.requests if r[0] == "/api/embed"]
    assert body["input"] == [search.QUERY_INSTRUCTION + "đi chợ"]
    ollama.requests.clear()
    assert (await search.search_moments(pool, router, "\x00\x1b​﻿")).hits == [] and ollama.requests == []
    assert (await search.search_moments(pool, None, "a\x00b")).hits == []


async def test_the_search_page_survives_control_characters(web, pool, dev, ollama):
    await _memories(pool, dev["device_id"])
    for q in ("%00", "%00%07%1b"):
        response = await web.get(f"/search?q={q}")
        assert response.status_code == 200 and 'name="q"' in response.text
        assert "Không tìm thấy khoảnh khắc" not in response.text and "\x00" not in response.text
    assert ollama.requests == []
    response = await web.get("/search?q=a%00b")  # searched as "a b": any answer but a crash
    assert response.status_code == 200 and "\x00" not in response.text
    response = await web.get("/search?q=%C4%91i%00ch%E1%BB%A3")  # "đi\x00chợ": searched as "đi chợ"
    assert response.status_code == 200 and response.text.count('class="hit"') == 3 and "\x00" not in response.text


async def test_the_date_range_means_local_days_not_utc_days(web, pool, dev):
    late = dt.datetime(2026, 10, 14, 16, 0, tzinfo=dt.UTC)  # 01:00 on the 15th in Seoul
    moment = await insert_moment(pool, dev["device_id"], kind="visual", ts=late, axis=0, lang="en",
                                 text="A night owl reads in bed")
    on_the_14th = (await web.get("/search", params={"q": "đọc sách", "from": "2026-10-14", "to": "2026-10-14"})).text
    assert "Không tìm thấy khoảnh khắc nào." in on_the_14th and f"m-{moment}" not in on_the_14th
    on_the_15th = (await web.get("/search", params={"q": "đọc sách", "from": "2026-10-15", "to": "2026-10-15"})).text
    assert f'<a href="/?day=2026-10-15#m-{moment}">' in on_the_15th
    assert "<time>15/10/2026 01:00</time>" in on_the_15th

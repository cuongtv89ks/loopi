import datetime as dt
import json

import httpx
import pytest

from helpers import FakeOllama, insert_item, insert_moment, paired_device
from loopi_brain import chat, search
from loopi_brain.app import create_app
from loopi_brain.llm import ASR_CAVEAT
from loopi_brain.providers.base import ProviderError, ProviderUnavailable
from loopi_brain.providers.config import DEFAULT_CONFIG, ProvidersConfig
from loopi_brain.providers.ollama import OllamaProvider
from loopi_brain.providers.router import build_router
from loopi_brain.web import search_routes

HX = {"HX-Request": "true"}
TODAY = dt.date(2026, 10, 14)
NOW = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.UTC)  # 21:00 in Seoul, a Wednesday
NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)
ANSWER = "Sáng mai bạn đi khám răng lúc 9 giờ [1]. Trưa nay có nấu mì [2][9]."


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def ollama():
    fake = FakeOllama()
    fake.reply = lambda body: {"from": "", "to": ""} if "format" in body else ANSWER
    return fake


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
        "dentist": await insert_moment(pool, device_id, kind="speech", ts=NINE, axis=0, lang="vi",
                                       text="Mai 9 giờ mình đi khám răng <nhé>"),
        "noodles": await insert_moment(pool, device_id, kind="visual", ts=NINE + dt.timedelta(hours=3), axis=0,
                                       lang="en", text="Two people cook noodles"),
    }


async def _events(ctx_gen) -> list[tuple[str, dict]]:
    return [event async for event in ctx_gen]


@pytest.mark.parametrize(("answer", "expected"), [
    ({"from": "2026-10-13", "to": "2026-10-13"}, (TODAY - dt.timedelta(days=1), TODAY - dt.timedelta(days=1))),
    ({"from": "2026-10-12", "to": ""}, (dt.date(2026, 10, 12), dt.date(2026, 10, 12))),
    ({"from": "2026-10-14", "to": "2026-10-05"}, (dt.date(2026, 10, 5), TODAY)),  # put in order
    ({"from": "2026-10-10", "to": "2026-12-31"}, (dt.date(2026, 10, 10), TODAY)),  # the future is cut off
    ({"from": "2027-01-01", "to": "2027-01-02"}, (None, None)),  # all in the future: no filter
    ({"from": "2020-01-01", "to": "2026-10-14"}, (TODAY - dt.timedelta(days=365), TODAY)),  # at most 366 days
    ({"from": "", "to": ""}, (None, None)),
    ({"from": "0001-01-01", "to": "0001-01-01"}, (None, None)),  # a placeholder year: it used to overflow
    ({"from": "0001-06-01", "to": ""}, (None, None)),
    ({"from": "1999-12-31", "to": "1999-12-31"}, (None, None)),  # before 2000: unusable
    ({"from": "0001-01-01", "to": "2026-10-13"}, (TODAY - dt.timedelta(days=1), TODAY - dt.timedelta(days=1))),
    ({"from": "hôm qua", "to": "13/10"}, (None, None)),
    ("not json at all", (None, None)),
])
async def test_the_time_filter_is_read_by_the_model_and_cleaned(router, ollama, answer, expected):
    ollama.reply = lambda body: answer
    assert await chat.time_filter(router, "Hôm qua mình làm gì?", TODAY) == expected
    body = ollama.requests[-1][1]
    assert body["format"] == chat.FILTER_SCHEMA and body["model"] == "qwen3:8b"
    assert "Today is Wednesday, 2026-10-14." in body["messages"][0]["content"]


async def test_no_time_filter_when_the_model_server_is_down(router, ollama):
    ollama.down = True
    assert await chat.time_filter(router, "Hôm qua mình làm gì?", TODAY) == (None, None)


async def test_a_question_gets_sources_then_a_streamed_cited_answer(pool, dev, router, ollama, pki_settings):
    ids = await _memories(pool, dev["device_id"])
    events = await _events(chat.chat_events(pool, router, "Mai mình phải làm gì?", pki_settings, now=NOW))
    names = [name for name, _ in events]
    assert names[:4] == ["status", "status", "sources", "status"] and names[-2:] == ["answer", "done"]
    assert set(names[4:-2]) == {"token"} and len(names[4:-2]) > 1  # streamed in pieces
    assert [h.id for h in events[2][1]["hits"]] == [ids["dentist"], ids["noodles"]]
    assert "".join(p["text"] for n, p in events if n == "token") == ANSWER
    assert events[-2][1] == {"text": ANSWER, "not_found": False}
    stream = [(b, t) for (path, b), (_, t) in zip(ollama.requests, ollama.timeouts, strict=True)
              if path == "/api/chat" and b["stream"]]
    [(body, timeout)] = stream
    assert body["think"] is False and body["options"]["num_predict"] == chat.ANSWER_MAX_TOKENS
    assert timeout["read"] == 900.0  # a CPU answer may take minutes (LOOPI_LLM_TIMEOUT_S)
    system, user = body["messages"]
    assert "[2] or [1][3]" in system["content"] and chat.NOT_FOUND in system["content"]
    assert ASR_CAVEAT in system["content"]  # transcripts may hold invented lines
    assert user["content"].startswith("Sources:\n[1] 2026-10-14 09:00, speech (vi): Mai 9 giờ mình đi khám răng <nhé>")
    assert "[2] 2026-10-14 12:00, picture: Two people cook noodles" in user["content"]
    assert user["content"].endswith("Question: Mai mình phải làm gì?")


async def test_nothing_relevant_says_not_found_and_asks_no_model_for_an_answer(pool, dev, router, ollama,
                                                                               pki_settings):
    await insert_moment(pool, dev["device_id"], kind="visual", ts=NINE, axis=5, text="An empty room")
    events = await _events(chat.chat_events(pool, router, "Khi nào mình đi Busan?", pki_settings, now=NOW))
    assert events[-2:] == [("answer", {"text": chat.NOT_FOUND, "not_found": True}), ("done", {})]
    assert "sources" not in [n for n, _ in events]
    assert not any(b.get("stream") for path, b in ollama.requests if path == "/api/chat")


async def test_a_time_filter_is_announced_and_limits_the_sources(pool, dev, router, ollama, pki_settings):
    await _memories(pool, dev["device_id"])
    ollama.reply = lambda body: {"from": "2026-10-13", "to": "2026-10-13"} if "format" in body else ANSWER
    events = await _events(chat.chat_events(pool, router, "Hôm qua có gì?", pki_settings, now=NOW))
    assert ("filter", {"text": "Tìm trong ngày 13/10/2026"}) in events
    assert events[-2][1]["text"] == chat.NOT_FOUND  # the memories are from today


async def test_an_answer_that_breaks_off_is_reported_after_the_sources(pool, dev, router, ollama, pki_settings):
    await _memories(pool, dev["device_id"])
    ollama.stream_fail_after = 2
    events = await _events(chat.chat_events(pool, router, "Mai mình phải làm gì?", pki_settings, now=NOW))
    names = [n for n, _ in events]
    assert "sources" in names and names[-2:] == ["error", "done"] and "answer" not in names
    assert "máy chủ model" in events[-2][1]["text"] and "ở dưới" in events[-2][1]["text"]  # sources render below


@pytest.mark.parametrize(("question", "expected"), [
    ("Hôm qua mình làm gì?", True), ("Mai mình phải làm gì?", True), ("What did we cook last week?", True),
    ("어제 뭐 했어?", True), ("Chuyện ngày 14/10 là gì?", True),
    ("Chìa khoá để ở đâu?", False), ("Ai đã gọi điện cho bố?", False),
])
def test_only_questions_with_a_time_word_need_a_time_filter(question, expected):
    assert chat.mentions_time(question) is expected


async def test_a_question_without_a_time_word_asks_the_model_once(pool, dev, router, ollama, pki_settings):
    await _memories(pool, dev["device_id"])
    events = await _events(chat.chat_events(pool, router, "Chìa khoá để ở đâu?", pki_settings, now=NOW))
    assert events[-1] == ("done", {})
    chats = [b for path, b in ollama.requests if path == "/api/chat"]
    assert len(chats) == 1 and chats[0]["stream"] is True  # no time-filter call, only the answer


async def test_a_model_that_finds_nothing_is_shown_as_not_found(web, pool, dev, ollama):
    await _memories(pool, dev["device_id"])
    ollama.reply = lambda body: {"from": "", "to": ""} if "format" in body else chat.NOT_FOUND
    async with web.stream("POST", "/chat", data={"q": "Mai mình phải làm gì?"}, headers=HX) as r:
        events = _parse((await r.aread()).decode())
    assert events[-2] == ("answer", {"html": f"<p>{chat.NOT_FOUND}</p>"})  # no "không trích nguồn" warning


async def test_an_empty_question_is_not_sent_anywhere(pool, router, ollama, pki_settings):
    events = await _events(chat.chat_events(pool, router, "   ", pki_settings, now=NOW))
    assert events[0] == ("answer", {"text": "Hãy nhập một câu hỏi.", "not_found": True}) and ollama.requests == []


def test_only_existing_sources_count_as_citations():
    assert chat.cited("A [1]. B [3, 2][2] C [7] [x]", 3) == [1, 3, 2]
    assert chat.cited("nothing cited", 3) == []


async def test_ollama_streams_ndjson_and_reports_broken_streams():
    fake = FakeOllama(caption="Một câu trả lời khá dài")
    provider = OllamaProvider("http://models.test:11434", client=fake.client())
    pieces = [p async for p in provider.stream_chat("qwen3:8b", [{"role": "user", "content": "x"}])]
    assert "".join(pieces) == "Một câu trả lời khá dài" and len(pieces) == 3
    assert fake.requests[0][1]["stream"] is True
    fake.stream_fail_after = 1
    with pytest.raises(ProviderError, match="unexpectedly stopped"):
        _ = [p async for p in provider.stream_chat("qwen3:8b", [{"role": "user", "content": "x"}])]
    with pytest.raises(ProviderUnavailable, match="HTTP 404"):
        _ = [p async for p in provider.stream_chat("missing:1b", [{"role": "user", "content": "x"}])]
    fake.down = True
    with pytest.raises(ProviderUnavailable):
        _ = [p async for p in provider.stream_chat("qwen3:8b", [{"role": "user", "content": "x"}])]
    garbage = OllamaProvider("http://models.test:11434", client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>\n"))))
    with pytest.raises(ProviderError, match="not JSON"):
        _ = [p async for p in garbage.stream_chat("qwen3:8b", [{"role": "user", "content": "x"}])]


async def test_a_stream_falls_back_only_before_its_first_piece(pki_settings, ollama):
    tasks = {name: route.model_dump() for name, route in DEFAULT_CONFIG.tasks.items()}
    tasks["chat"] = {"provider": "ollama", "model": "qwen3:8b", "fallback": {"provider": "ollama", "model": "qwen3:4b"}}
    router = build_router(ProvidersConfig.model_validate({"tasks": tasks}), pki_settings, ollama.client())
    ollama.models.remove("qwen3:8b")  # not pulled: the fallback answers
    pieces = [p async for p in router.stream_chat("chat", [{"role": "user", "content": "x"}])]
    assert {version for _, version in pieces} == {"ollama:qwen3:4b"}
    ollama.models.append("qwen3:8b")
    ollama.stream_fail_after = 1  # the primary breaks after it started: no fallback, the error goes up
    with pytest.raises(ProviderError):
        _ = [p async for p in router.stream_chat("chat", [{"role": "user", "content": "x"}])]
    ollama.down = True
    with pytest.raises(ProviderUnavailable):
        _ = [p async for p in router.stream_chat("chat", [{"role": "user", "content": "x"}])]


def _parse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


async def test_the_chat_endpoint_streams_server_sent_events(web, pool, dev):
    ids = await _memories(pool, dev["device_id"])
    assert (await web.post("/chat", data={"q": "Mai làm gì?"})).status_code == 403
    async with web.stream("POST", "/chat", data={"q": "Mai mình phải làm gì?"}, headers=HX) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = _parse((await r.aread()).decode())
    sources = dict(events)["sources"]["html"]
    assert sources.startswith('<ol class="hits">') and sources.count('class="hit"') == 2
    assert "Mai 9 giờ mình đi khám răng &lt;nhé&gt;" in sources and f"#m-{ids['dentist']}" in sources
    answer = events[-2][1]["html"]
    assert f'<a class="cite" href="/?day=2026-10-14#m-{ids["dentist"]}">[1]</a>' in answer
    assert "[9]" not in answer and "[2]</a>" in answer
    assert events[-1] == ("done", {})


async def test_the_chat_endpoint_flags_uncited_answers_and_not_found(web, pool, dev, ollama):
    await _memories(pool, dev["device_id"])
    ollama.reply = lambda body: {"from": "", "to": ""} if "format" in body else "Bạn đi khám răng <script>."
    async with web.stream("POST", "/chat", data={"q": "Mai mình phải làm gì?"}, headers=HX) as r:
        events = _parse((await r.aread()).decode())
    answer = events[-2][1]["html"]
    assert "&lt;script&gt;" in answer and "không trích nguồn nào" in answer
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM moment")  # nothing left to find
    async with web.stream("POST", "/chat", data={"q": "Mai mình phải làm gì?"}, headers=HX) as r:
        events = _parse((await r.aread()).decode())
    assert events[-2] == ("answer", {"html": f"<p>{chat.NOT_FOUND}</p>"})


async def test_the_search_page_carries_the_chat_box(web):
    text = (await web.get("/search")).text
    assert 'id="chat-form"' in text and 'fetch("/chat"' in text and '"HX-Request": "true"' in text


# --- beyond the plan's tests: Task 7's cleaning and branches that the chat path reuses -------------------------


async def test_control_characters_in_a_question_are_cleaned_before_any_model_sees_them(pool, dev, router, ollama,
                                                                                       pki_settings):
    await _memories(pool, dev["device_id"])
    dirty = "Mai\x00  mình\x07 phải\nlàm gì?\x1b"  # NUL, BEL, a newline, ESC: PostgreSQL text cannot hold the NUL
    events = await _events(chat.chat_events(pool, router, dirty, pki_settings, now=NOW))
    assert "sources" in [n for n, _ in events] and events[-2][0] == "answer" and events[-1] == ("done", {})
    clean = "Mai mình phải làm gì?"
    chats = [b for path, b in ollama.requests if path == "/api/chat"]
    assert [m["messages"][1]["content"] for m in chats if "format" in m] == [clean]  # the time filter's question
    [stream] = [b for b in chats if b["stream"]]
    assert stream["messages"][1]["content"].endswith(f"Question: {clean}")
    [embed] = [b for path, b in ollama.requests if path == "/api/embed"]
    assert embed["input"] == [search.QUERY_INSTRUCTION + clean]


async def test_a_question_of_control_characters_only_is_empty(pool, router, ollama, pki_settings):
    events = await _events(chat.chat_events(pool, router, "\x00\x1b​﻿\n", pki_settings, now=NOW))
    assert events == [("answer", {"text": "Hãy nhập một câu hỏi.", "not_found": True}), ("done", {})]
    assert ollama.requests == []


async def test_the_chat_endpoint_survives_control_characters(web, pool, dev, ollama):
    await _memories(pool, dev["device_id"])
    async with web.stream("POST", "/chat", data={"q": "Mai\x00 mình phải làm gì?"}, headers=HX) as r:
        assert r.status_code == 200
        body = (await r.aread()).decode()
    events = _parse(body)
    assert dict(events)["sources"]["html"].count('class="hit"') == 2 and events[-1] == ("done", {})
    assert "\x00" not in body and "\\u0000" not in body
    ollama.requests.clear()
    async with web.stream("POST", "/chat", data={"q": "\x00\x07"}, headers=HX) as r:
        assert r.status_code == 200
        events = _parse((await r.aread()).decode())
    assert events == [("answer", {"html": "<p>Hãy nhập một câu hỏi.</p>"}), ("done", {})]
    assert ollama.requests == []


async def test_without_models_the_sources_are_shown_and_no_answer_is_attempted(pool, dev, ollama, pki_settings):
    ids = await _memories(pool, dev["device_id"])
    events = await _events(chat.chat_events(pool, None, "Mai 9 giờ mình đi khám răng", pki_settings, now=NOW))
    assert [n for n, _ in events] == ["status", "status", "sources", "error", "done"]
    assert [h.id for h in events[2][1]["hits"]] == [ids["dentist"]]  # found by its words alone
    assert "cấu hình provider lỗi" in events[3][1]["text"] and "ở dưới" in events[3][1]["text"]
    assert ollama.requests == []


async def test_a_broken_providers_file_still_shows_the_sources_over_http(pool, dev, pki_settings, jobs_open, ollama,
                                                                         tmp_path):
    await _memories(pool, dev["device_id"])
    bad = tmp_path / "providers.yaml"
    bad.write_text("tasks: {}\n")
    app = create_app(pki_settings.model_copy(update={"providers_file": bad}), pool=pool,
                     http_client=ollama.client(), llm_client=ollama.client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        async with c.stream("POST", "/chat", data={"q": "Mai 9 giờ mình đi khám răng"}, headers=HX) as r:
            events = _parse((await r.aread()).decode())
    assert [n for n, _ in events] == ["status", "status", "sources", "error", "done"]
    assert events[2][1]["html"].count('class="hit"') == 1 and "cấu hình provider lỗi" in events[3][1]["text"]
    assert ollama.requests == []


@pytest.mark.parametrize(("meta", "line", "shown"), [
    ({"note": "  Nhớ gọi cho <bố>  "}, "the user marked this moment: Nhớ gọi cho <bố>", "<p>Nhớ gọi cho &lt;bố&gt;</p>"),
    ({}, "the user marked this moment\n", "<p>Đánh dấu</p>"),  # the device's button sends no note
    ({"note": "   "}, "the user marked this moment\n", "<p>Đánh dấu</p>"),
])
async def test_a_marker_is_a_source_with_its_note(web, pool, dev, ollama, meta, line, shown):
    item = await insert_item(pool, dev["device_id"], kind="marker", started_at=NINE, seconds=0, meta=meta)
    await insert_moment(pool, dev["device_id"], kind="marker", ts=NINE, axis=0, text="Marked moment", item_id=item)
    async with web.stream("POST", "/chat", data={"q": "Mình đã đánh dấu gì?"}, headers=HX) as r:
        events = _parse((await r.aread()).decode())
    [stream] = [b for path, b in ollama.requests if path == "/api/chat" and b["stream"]]
    assert f"[1] 2026-10-14 09:00, marked moment: {line}" in stream["messages"][1]["content"]
    html = dict(events)["sources"]["html"]
    assert shown in html and "Marked moment" not in html and "· Đánh dấu" in html


async def test_a_failure_inside_the_stream_is_reported_not_raised(web, monkeypatch):
    async def broken(*args, **kwargs):
        yield "status", {"text": "Đang hiểu câu hỏi…"}
        raise RuntimeError("database went away")

    monkeypatch.setattr(search_routes.chat, "chat_events", broken)
    async with web.stream("POST", "/chat", data={"q": "Mai làm gì?"}, headers=HX) as r:
        assert r.status_code == 200
        events = _parse((await r.aread()).decode())
    assert events == [("status", {"text": "Đang hiểu câu hỏi…"}), ("error", {"text": "Có lỗi khi trả lời, thử lại."}),
                      ("done", {})]


def test_an_answer_links_each_valid_citation_and_escapes_the_rest():
    sources = [{"link": "/?day=2026-10-14#m-A"}, {"link": "/?day=2026-10-14#m-B"}, {"link": '/?x="&y#m-C'}]
    html = str(search_routes.render_answer("Một <b>[1, 3]</b>\nHai [2][8] [x]", sources))
    assert html.startswith("<p>Một &lt;b&gt;") and "<br>Hai " in html and "[8]" not in html and "[x]" in html
    assert '<a class="cite" href="/?day=2026-10-14#m-A">[1]</a><a class="cite" href="/?x=&#34;&amp;y#m-C">[3]</a>' in html
    assert "không trích nguồn nào" not in html
    assert "không trích nguồn nào" in str(search_routes.render_answer("không có [5]", sources))
    assert "không trích nguồn nào" not in str(search_routes.render_answer("không có gì", []))  # not-found: no warning


async def test_a_range_of_days_is_announced_as_a_range(pool, dev, router, ollama, pki_settings):
    await _memories(pool, dev["device_id"])
    ollama.reply = lambda body: {"from": "2026-10-05", "to": "2026-10-11"} if "format" in body else ANSWER
    events = await _events(chat.chat_events(pool, router, "Tuần trước có gì?", pki_settings, now=NOW))
    assert ("filter", {"text": "Tìm từ 05/10/2026 đến 11/10/2026"}) in events


async def test_ollama_skips_blank_lines_and_refuses_what_is_not_a_stream_of_objects():
    def served(content: bytes):
        return OllamaProvider("http://models.test:11434", client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=content))))

    ok = served(b'\n{"message": {"content": "ab"}, "done": false}\n  \n{"message": {"content": "c"}, "done": true}\n')
    assert [p async for p in ok.stream_chat("qwen3:8b", [])] == ["ab", "c"]
    with pytest.raises(ProviderError, match="unexpected stream line"):
        _ = [p async for p in served(b"[1]\n").stream_chat("qwen3:8b", [])]

    def slow(request):
        raise httpx.ReadTimeout("no answer in 900 s", request=request)

    timed_out = OllamaProvider("http://models.test:11434", client=httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    with pytest.raises(ProviderError, match="ReadTimeout") as caught:
        _ = [p async for p in timed_out.stream_chat("qwen3:8b", [])]
    assert not isinstance(caught.value, ProviderUnavailable)  # a failed request, not a stopped server


async def test_a_stream_without_any_text_ends_without_trying_the_fallback(router, ollama):
    ollama.reply = lambda body: ""
    assert [p async for p in router.stream_chat("chat", [{"role": "user", "content": "x"}])] == []
    assert len([1 for path, _ in ollama.requests if path == "/api/chat"]) == 1


# --- fix round 1 -------------------------------------------------------------------------------------------------


def test_a_date_that_cannot_be_clamped_means_no_filter():
    assert chat.clean_filter(dt.date.min, dt.date.min, TODAY) == (None, None)  # last - 365 days overflows
    assert chat.clean_filter(dt.date.min, TODAY, TODAY) == (TODAY - dt.timedelta(days=365), TODAY)  # still clamps


async def test_a_year_one_date_from_the_model_does_not_break_the_answer(pool, dev, router, ollama, pki_settings):
    await _memories(pool, dev["device_id"])
    ollama.reply = lambda body: {"from": "0001-01-01", "to": "0001-01-01"} if "format" in body else ANSWER
    events = await _events(chat.chat_events(pool, router, "Hôm qua có gì?", pki_settings, now=NOW))
    names = [n for n, _ in events]
    assert "filter" not in names and "error" not in names and names[-2:] == ["answer", "done"]
    assert "sources" in names  # no filter: all the history is searched


@pytest.mark.parametrize(("now", "stated"), [
    (NOW, "Now is Wednesday, 2026-10-14 21:00 (Asia/Seoul)."),
    (dt.datetime(2026, 10, 14, 16, 0, tzinfo=dt.UTC), "Now is Thursday, 2026-10-15 01:00 (Asia/Seoul)."),  # local, not UTC
])
async def test_the_answer_prompt_states_the_local_date_and_time(pool, dev, router, ollama, pki_settings, now, stated):
    await _memories(pool, dev["device_id"])
    await _events(chat.chat_events(pool, router, "Mai mình phải làm gì?", pki_settings, now=now))
    [body] = [b for path, b in ollama.requests if path == "/api/chat" and b["stream"]]
    system = body["messages"][0]["content"]
    assert stated in system
    assert "[2] or [1][3]" in system and chat.NOT_FOUND in system and ASR_CAVEAT in system  # the rest is unchanged
    assert "relative to its own date" in system  # an old "mai" is not today's plan


@pytest.mark.parametrize("silence", ["", "  \n "])
async def test_a_model_that_writes_nothing_is_an_error_not_an_empty_answer(pool, dev, router, ollama, pki_settings,
                                                                          silence):
    await _memories(pool, dev["device_id"])
    ollama.reply = lambda body: {"from": "", "to": ""} if "format" in body else silence
    events = await _events(chat.chat_events(pool, router, "Mai mình phải làm gì?", pki_settings, now=NOW))
    names = [n for n, _ in events]
    assert "sources" in names and names[-2:] == ["error", "done"] and "answer" not in names
    assert events[-2][1]["text"] == chat.ANSWER_FAILED and "ở dưới" in chat.ANSWER_FAILED


async def test_the_chat_endpoint_reports_an_empty_answer(web, pool, dev, ollama):
    await _memories(pool, dev["device_id"])
    ollama.reply = lambda body: {"from": "", "to": ""} if "format" in body else ""
    async with web.stream("POST", "/chat", data={"q": "Mai mình phải làm gì?"}, headers=HX) as r:
        events = _parse((await r.aread()).decode())
    assert [n for n, _ in events][-2:] == ["error", "done"] and "answer" not in dict(events)

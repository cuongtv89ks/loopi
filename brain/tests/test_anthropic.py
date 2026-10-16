import json

import httpx
import pytest
from pydantic import SecretStr

from helpers import FakeOllama
from loopi_brain.app import create_app
from loopi_brain.config import Settings
from loopi_brain.pipeline import chapters, recap
from loopi_brain.providers.anthropic import AnthropicProvider, strict_schema
from loopi_brain.providers.base import PrivacyBlocked, ProviderError, ProviderUnavailable, is_cloud
from loopi_brain.providers.config import DEFAULT_CONFIG, ProvidersConfig, TaskRoute
from loopi_brain.providers.ollama import OllamaHealth
from loopi_brain.providers.router import Router, build_router

KEY = "sk-ant-test-0000"


class FakeAnthropic:
    """An in-process stand-in for the Messages API (an httpx.MockTransport handler)."""

    def __init__(self, text: str = "Xin chào", status: int = 200, stop_reason: str = "end_turn"):
        self.text, self.status, self.stop_reason = text, status, stop_reason
        self.requests: list[tuple[dict, dict]] = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append((dict(request.headers), body))
        if self.status != 200:
            return httpx.Response(self.status, json={"type": "error", "error": {"type": "x", "message": "nope"}})
        if body.get("stream"):
            events = [
                {"type": "message_start", "message": {"id": "m"}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": ""}},
                {"type": "ping"},
                *[{"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": self.text[i:i + 4]}}
                  for i in range(0, len(self.text), 4)],
                {"type": "message_delta", "delta": {"stop_reason": self.stop_reason}},
                {"type": "message_stop"},
            ]
            sse = "".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)
            return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "type": "message", "stop_reason": self.stop_reason,
            "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": self.text}],
        })

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def _provider(fake) -> AnthropicProvider:
    return AnthropicProvider(KEY, base_url="https://api.anthropic.test", client=fake.client())


async def test_chat_sends_text_only_with_the_system_prompt_on_top_and_no_sampling_parameters():
    fake = FakeAnthropic(text='{"title": "Sáng", "summary": "Đọc sách."}')
    text = await _provider(fake).chat(
        "claude-haiku-5-5",
        [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Tóm tắt"}],
        json_schema=chapters.SUMMARY_SCHEMA, temperature=0.3, max_tokens=400,
    )
    assert json.loads(text) == {"title": "Sáng", "summary": "Đọc sách."}
    [(headers, body)] = fake.requests
    assert headers["x-api-key"] == KEY and headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == "claude-haiku-5-5" and body["system"] == "Be brief."
    # Thinking off, and a cap the answer always fits in (the brain clips longer answers).
    assert body["thinking"] == {"type": "disabled"} and body["max_tokens"] == 1024
    assert body["messages"] == [{"role": "user", "content": "Tóm tắt"}]
    assert "temperature" not in body and body["output_config"]["effort"] == "low"
    schema = body["output_config"]["format"]["schema"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert schema["additionalProperties"] is False and "maxLength" not in json.dumps(schema)
    assert repr(_provider(fake)) == "AnthropicProvider('https://api.anthropic.test')"  # no key in logs


@pytest.mark.parametrize(("model", "thinking", "max_tokens"), [
    ("claude-haiku-5-5", {"type": "disabled"}, 1024),
    ("claude-sonnet-5-5", {"type": "between_tools"}, 1024),  # Sonnet 5.5 rejects "disabled"
    ("claude-opus-5-5", None, 8192),  # always thinks: thinking and answer share the budget
])
async def test_thinking_is_turned_off_where_allowed_and_otherwise_budgeted(model, thinking, max_tokens):
    fake = FakeAnthropic()
    await _provider(fake).chat(model, [{"role": "user", "content": "x"}], max_tokens=60)
    pieces = [p async for p in _provider(fake).stream_chat(model, [{"role": "user", "content": "x"}], max_tokens=600)]
    assert pieces
    for _, body in fake.requests:
        assert body.get("thinking") == thinking and body["max_tokens"] == max_tokens


async def test_an_answer_cut_off_before_any_text_is_an_error():
    with pytest.raises(ProviderError, match="max_tokens was used up"):
        await _provider(FakeAnthropic(text="", stop_reason="max_tokens")).chat("m", [{"role": "user", "content": "x"}])


def test_strict_schema_closes_every_object_and_drops_limits():
    schema = strict_schema(recap.RECAP_SCHEMA)
    item = schema["properties"]["commitments"]["items"]
    assert item["additionalProperties"] is False and schema["additionalProperties"] is False
    assert "maxItems" not in json.dumps(schema) and "maxLength" not in json.dumps(schema)
    assert recap.RECAP_SCHEMA["properties"]["main_events"]["maxItems"] == recap.MAX_EVENTS  # the original is unchanged


@pytest.mark.parametrize(("status", "kind"), [
    (429, ProviderUnavailable), (529, ProviderUnavailable), (503, ProviderUnavailable),
    (401, ProviderError), (400, ProviderError), (404, ProviderError),
])
async def test_temporary_errors_are_unavailable_and_mistakes_are_errors(status, kind):
    with pytest.raises(kind) as info:
        await _provider(FakeAnthropic(status=status)).chat("claude-haiku-5-5", [{"role": "user", "content": "x"}])
    assert f"HTTP {status}: nope" in str(info.value)
    if kind is ProviderError:
        assert not isinstance(info.value, ProviderUnavailable)


async def test_refusals_empty_answers_and_broken_connections_are_errors():
    with pytest.raises(ProviderError, match="declined"):
        await _provider(FakeAnthropic(stop_reason="refusal")).chat("m", [{"role": "user", "content": "x"}])
    with pytest.raises(ProviderError, match="no text"):
        await _provider(FakeAnthropic(text="")).chat("m", [{"role": "user", "content": "x"}])

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    down = AnthropicProvider(KEY, client=httpx.AsyncClient(transport=httpx.MockTransport(refused)))
    with pytest.raises(ProviderUnavailable):
        await down.chat("m", [{"role": "user", "content": "x"}])
    with pytest.raises(ProviderUnavailable):
        _ = [p async for p in down.stream_chat("m", [{"role": "user", "content": "x"}])]


async def test_images_and_embeddings_are_never_sent():
    fake = FakeAnthropic()
    with pytest.raises(PrivacyBlocked):
        await _provider(fake).chat("m", [{"role": "user", "content": "x"}], images=[b"jpeg"])
    with pytest.raises(ProviderError):
        await _provider(fake).embed("m", ["x"])
    assert fake.requests == []


async def test_streaming_reads_text_deltas_only():
    fake = FakeAnthropic(text="Sáng mai đi khám răng [1].")
    pieces = [p async for p in _provider(fake).stream_chat("claude-sonnet-5-5", [{"role": "user", "content": "x"}])]
    assert "".join(pieces) == "Sáng mai đi khám răng [1]." and len(pieces) > 1
    assert fake.requests[0][1]["stream"] is True
    with pytest.raises(ProviderError, match="declined"):
        _ = [p async for p in _provider(FakeAnthropic(stop_reason="refusal")).stream_chat("m", [{"role": "user", "content": "x"}])]
    with pytest.raises(ProviderUnavailable):
        _ = [p async for p in _provider(FakeAnthropic(status=529)).stream_chat("m", [{"role": "user", "content": "x"}])]

    def overloaded(request):
        event = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
        return httpx.Response(200, content=f"event: error\ndata: {json.dumps(event)}\n\n".encode())

    mid = AnthropicProvider(KEY, client=httpx.AsyncClient(transport=httpx.MockTransport(overloaded)))
    with pytest.raises(ProviderUnavailable, match="Overloaded"):
        _ = [p async for p in mid.stream_chat("m", [{"role": "user", "content": "x"}])]


def test_anthropic_may_serve_text_tasks_only():
    tasks = {name: route.model_dump() for name, route in DEFAULT_CONFIG.tasks.items()}
    tasks["recap"] = {"provider": "ollama", "model": "qwen3:8b",
                      "fallback": {"provider": "anthropic", "model": "claude-haiku-5-5"}}
    tasks["chat"] = {"provider": "anthropic", "model": "claude-sonnet-5-5",
                     "fallback": {"provider": "ollama", "model": "qwen3:8b"}}
    config = ProvidersConfig.model_validate({"tasks": tasks})
    assert config.providers_used() == {"ollama", "local", "anthropic"}
    for task in ("caption", "embed", "asr"):
        with pytest.raises(ValueError):
            ProvidersConfig.model_validate({"tasks": {**tasks, task: {"provider": "anthropic", "model": "c"}}})


def test_the_privacy_gate_is_an_allow_list():
    assert not is_cloud("ollama") and not is_cloud("local")
    assert is_cloud("anthropic") and is_cloud("some-future-provider")


async def test_an_unknown_provider_never_gets_images_and_task_ready_follows_the_privacy_rule():
    config = ProvidersConfig.model_validate({"tasks": {n: r.model_dump() for n, r in DEFAULT_CONFIG.tasks.items()}})
    config.tasks["caption"] = TaskRoute.model_construct(
        provider="newcloud", model="m", fallback=TaskRoute(provider="ollama", model="minicpm-v4.6:1b"))
    stub = AnthropicProvider(KEY, client=FakeAnthropic().client())
    router = Router(config, {"newcloud": stub, "ollama": stub})
    without_vision = OllamaHealth(reachable=True, present=[], missing=["minicpm-v4.6:1b"])
    assert not router.task_ready("caption", without_vision)  # the cloud route may not take the images
    config.privacy.allow_cloud_images = True
    assert router.task_ready("caption", without_vision)


def test_the_api_key_comes_from_anthropic_api_key_and_blank_means_none(monkeypatch):
    monkeypatch.setenv("LOOPI_DATABASE_URL", "postgresql://x")
    assert Settings().anthropic_api_key is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  ")
    assert Settings().anthropic_api_key is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    settings = Settings()
    assert settings.anthropic_api_key.get_secret_value() == KEY and KEY not in repr(settings)


async def test_routes_to_anthropic_are_used_only_with_a_key(pki_settings, caplog):
    tasks = {name: route.model_dump() for name, route in DEFAULT_CONFIG.tasks.items()}
    tasks["chat"] = {"provider": "anthropic", "model": "claude-sonnet-5-5",
                     "fallback": {"provider": "ollama", "model": "qwen3:8b"}}
    config = ProvidersConfig.model_validate({"tasks": tasks})
    ollama = FakeOllama(caption="từ Ollama")
    no_key = build_router(config, pki_settings, ollama.client())
    assert "ANTHROPIC_API_KEY is not set" in caplog.text
    assert (await no_key.chat("chat", [{"role": "user", "content": "x"}])).model_version == "ollama:qwen3:8b"
    fake = FakeAnthropic(text="từ Claude")

    def both(request):
        return fake(request) if request.url.host == "api.anthropic.test" else ollama(request)

    keyed = pki_settings.model_copy(update={"anthropic_api_key": SecretStr(KEY), "anthropic_url": "https://api.anthropic.test"})
    router = build_router(config, keyed, httpx.AsyncClient(transport=httpx.MockTransport(both)))
    result = await router.chat("chat", [{"role": "user", "content": "x"}])
    assert (result.text, result.model_version) == ("từ Claude", "anthropic:claude-sonnet-5-5")
    fake.status = 529  # overloaded: the local fallback answers, streamed too
    pieces = [p async for p in router.stream_chat("chat", [{"role": "user", "content": "x"}])]
    assert "".join(t for t, _ in pieces) == "từ Ollama" and pieces[0][1] == "ollama:qwen3:8b"
    only_cloud = ProvidersConfig.model_validate({"tasks": {**tasks, "chat": {"provider": "anthropic", "model": "c"}}})
    with pytest.raises(ProviderUnavailable, match="ANTHROPIC_API_KEY"):
        await build_router(only_cloud, pki_settings, ollama.client()).chat("chat", [{"role": "user", "content": "x"}])


async def test_the_settings_page_says_whether_the_key_is_set_and_never_shows_it(pool, pki_settings, jobs_open, tmp_path):
    tasks = {name: route.model_dump() for name, route in DEFAULT_CONFIG.tasks.items()}
    tasks["recap"] = {"provider": "ollama", "model": "qwen3:8b",
                      "fallback": {"provider": "anthropic", "model": "claude-haiku-5-5"}}
    routed = tmp_path / "providers.yaml"
    routed.write_text(json.dumps({"tasks": tasks}))
    for update, expected in (({}, "chưa đặt"), ({"anthropic_api_key": SecretStr(KEY)}, "đã đặt")):
        app = create_app(pki_settings.model_copy(update={"providers_file": routed, **update}), pool=pool,
                         http_client=FakeOllama().client(), llm_client=FakeOllama().client())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
            text = (await c.get("/settings")).text
        assert f"ANTHROPIC_API_KEY: {expected}" in text and KEY not in text
        assert "<code>ollama:qwen3:8b → anthropic:claude-haiku-5-5</code>" in text
        assert ("bị bỏ qua" in text) == (expected == "chưa đặt")
        # With a key, the page says which tasks may send text to the cloud.
        assert ("Gửi text lên cloud (Anthropic) khi dùng tuyến đó: Recap cuối ngày" in text) == (expected == "đã đặt")


async def test_malformed_answers_and_transport_failures_are_errors_that_never_carry_the_key():
    messages = [{"role": "user", "content": "x"}]

    def provider(handler) -> AnthropicProvider:
        return AnthropicProvider(KEY, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    def reset(request):
        raise httpx.ReadError("connection reset", request=request)

    bodies = {
        "not JSON": lambda request: httpx.Response(200, content=b"<html>"),
        "unexpected": lambda request: httpx.Response(200, json=["a"]),
        "plain text error": lambda request: httpx.Response(401, content=b"invalid x-api-key"),
        "dropped": reset,
    }
    for name, handler in bodies.items():
        with pytest.raises(ProviderError) as info:
            await provider(handler).chat("m", messages)
        assert KEY not in str(info.value), name
        assert not isinstance(info.value, ProviderUnavailable), name
    with pytest.raises(ProviderError, match="not JSON"):
        _ = [p async for p in provider(lambda r: httpx.Response(200, content=b"data: {broken\n\n")).stream_chat("m", messages)]
    with pytest.raises(ProviderError, match="ReadError"):
        _ = [p async for p in provider(reset).stream_chat("m", messages)]

import asyncio
import json
import logging
import traceback

import httpx
import pytest
from pydantic import SecretStr

from helpers import FakeOllama
from loopi_brain.app import create_app
from loopi_brain.config import Settings
from loopi_brain.pipeline import chapters, recap
from loopi_brain.providers.anthropic import ERROR_TAIL, AnthropicProvider, strict_schema
from loopi_brain.providers.base import PrivacyBlocked, ProviderError, ProviderUnavailable, is_cloud
from loopi_brain.providers import router as router_module
from loopi_brain.providers.config import DEFAULT_CONFIG, ProvidersConfig, TaskRoute
from loopi_brain.providers.ollama import OllamaHealth
from loopi_brain.providers.router import Router, build_router

KEY = "sk-ant-test-0000"


class FakeAnthropic:
    """An in-process stand-in for the Messages API (an httpx.MockTransport handler)."""

    def __init__(self, text: str = "Xin chào", status: int = 200, stop_reason: str = "end_turn"):
        self.text, self.status, self.stop_reason = text, status, stop_reason
        self.requests: list[tuple[dict, dict]] = []
        self.timeouts: list[dict] = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append((dict(request.headers), body))
        self.timeouts.append(request.extensions["timeout"])
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


async def test_routes_to_anthropic_are_used_only_with_a_key(pki_settings, caplog, monkeypatch):
    monkeypatch.setattr(router_module, "_missing_key_warned", False)
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
        # A cloud fallback is not only for an Ollama outage: a local model that fails or runs out of time uses it too.
        assert ("được dùng cả khi model local lỗi hoặc chạy quá 15 phút, không chỉ khi Ollama tắt" in text) == (
            expected == "đã đặt")


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


async def test_a_stream_stopped_before_any_text_is_an_error_but_a_clipped_answer_is_kept():
    messages = [{"role": "user", "content": "x"}]
    with pytest.raises(ProviderError, match="max_tokens was used up before any answer text") as info:
        _ = [p async for p in _provider(FakeAnthropic(text="", stop_reason="max_tokens")).stream_chat("m", messages)]
    assert not isinstance(info.value, ProviderUnavailable)
    with pytest.raises(ProviderError, match="stop_reason pause_turn before any answer text"):
        _ = [p async for p in _provider(FakeAnthropic(text="", stop_reason="pause_turn")).stream_chat("m", messages)]
    # Text was already shown: a cut-off answer is not an error (the reader has part of it), as for a local model.
    clipped = [p async for p in _provider(FakeAnthropic(text="Sáng mai", stop_reason="max_tokens")).stream_chat("m", messages)]
    assert "".join(clipped) == "Sáng mai"


@pytest.mark.parametrize("stop_reason", ["max_tokens", "end_turn"])
async def test_a_stream_stopped_before_any_text_falls_back_to_the_next_route(pki_settings, stop_reason):
    tasks = {name: route.model_dump() for name, route in DEFAULT_CONFIG.tasks.items()}
    tasks["chat"] = {"provider": "anthropic", "model": "claude-sonnet-5-5",
                     "fallback": {"provider": "ollama", "model": "qwen3:8b"}}
    config = ProvidersConfig.model_validate({"tasks": tasks})
    ollama = FakeOllama(caption="từ Ollama")
    fake = FakeAnthropic(text="", stop_reason=stop_reason)

    def both(request):
        return fake(request) if request.url.host == "api.anthropic.test" else ollama(request)

    keyed = pki_settings.model_copy(update={"anthropic_api_key": SecretStr(KEY), "anthropic_url": "https://api.anthropic.test"})
    router = build_router(config, keyed, httpx.AsyncClient(transport=httpx.MockTransport(both)))
    pieces = [p async for p in router.stream_chat("chat", [{"role": "user", "content": "x"}])]
    assert "".join(t for t, _ in pieces) == "từ Ollama" and {v for _, v in pieces} == {"ollama:qwen3:8b"}
    assert fake.requests[0][1]["stream"] is True  # Anthropic was asked first


async def test_every_call_uses_the_providers_own_timeout_not_the_shared_clients():
    fake = FakeAnthropic()
    shared = httpx.AsyncClient(transport=httpx.MockTransport(fake), timeout=5.0)  # the Ollama client's short timeout
    provider = AnthropicProvider(KEY, base_url="https://api.anthropic.test", client=shared)
    await provider.chat("m", [{"role": "user", "content": "x"}])
    _ = [p async for p in provider.stream_chat("m", [{"role": "user", "content": "x"}])]
    assert fake.timeouts == [{"connect": 10.0, "read": 120.0, "write": 120.0, "pool": 120.0}] * 2


def _stream_of(*events: dict):
    sse = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
    return lambda request: httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})


@pytest.mark.parametrize(("error_type", "kind"), [
    ("api_error", ProviderUnavailable), ("overloaded_error", ProviderUnavailable),
    ("rate_limit_error", ProviderUnavailable), ("invalid_request_error", ProviderError),
])
async def test_a_streamed_error_event_is_unavailable_when_the_same_http_status_would_be(error_type, kind):
    handler = _stream_of({"type": "error", "error": {"type": error_type, "message": "boom"}})
    provider = AnthropicProvider(KEY, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(kind, match=error_type) as info:
        _ = [p async for p in provider.stream_chat("m", [{"role": "user", "content": "x"}])]
    assert (kind is ProviderUnavailable) == isinstance(info.value, ProviderUnavailable)


async def test_a_stream_that_ends_without_any_text_is_an_error_like_chat():
    messages = [{"role": "user", "content": "x"}]
    with pytest.raises(ProviderError, match="no text"):
        _ = [p async for p in _provider(FakeAnthropic(text="", stop_reason="end_turn")).stream_chat("m", messages)]
    closed = _stream_of({"type": "message_start", "message": {"id": "m"}})  # closes without message_stop
    provider = AnthropicProvider(KEY, client=httpx.AsyncClient(transport=httpx.MockTransport(closed)))
    with pytest.raises(ProviderError, match="no text"):
        _ = [p async for p in provider.stream_chat("m", messages)]


def test_strict_schema_strips_keywords_only_where_they_are_keywords():
    schema = {
        "type": "object",
        "properties": {
            "minimum": {"type": "integer", "minimum": 1, "maximum": 9, "multipleOf": 2},
            "maxLength": {"type": "string", "maxLength": 5, "enum": ["a", "b"]},
            "nested": {"type": ["object", "null"], "properties": {"minItems": {"type": "array", "minItems": 1,
                                                                                  "items": {"type": "string"}}}},
        },
        "default": {"minimum": 3},
    }
    out = strict_schema(schema)
    assert set(out["properties"]) == {"minimum", "maxLength", "nested"}  # property names are never keywords
    assert out["properties"]["minimum"] == {"type": "integer"}
    assert out["properties"]["maxLength"] == {"type": "string", "enum": ["a", "b"]}
    assert out["properties"]["nested"]["additionalProperties"] is False
    assert out["properties"]["nested"]["properties"]["minItems"] == {"type": "array", "items": {"type": "string"}}
    assert out["default"] == {"minimum": 3}  # literal data is left alone
    assert schema["properties"]["minimum"]["minimum"] == 1  # the original is unchanged


class _Hangup:
    """A listener on 127.0.0.1 that accepts a connection and hangs up: enough for the HTTP library to build (and
    refuse) a request the way it would against the real API."""

    async def __aenter__(self) -> str:
        self.connections = 0

        async def accept(reader, writer):
            self.connections += 1
            writer.close()

        self.server = await asyncio.start_server(accept, "127.0.0.1", 0)
        return f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


def _assert_no_secret_anywhere(error: BaseException, secret: str):
    assert error.__cause__ is None and error.__context__ is None  # no chained exception carries the original text
    assert secret not in str(error) and secret not in repr(error)
    assert secret not in "".join(traceback.format_exception(error))


@pytest.mark.parametrize("suffix", ["\n", "\r\n", "\r", " "])
async def test_a_key_with_stray_whitespace_never_reaches_an_error_a_log_or_a_chain(suffix, caplog):
    secret = "sk-ant-leak-4242"
    messages = [{"role": "user", "content": "x"}]
    hangup = _Hangup()
    async with hangup as url:
        # A real transport: the HTTP library itself would refuse the header value and quote it, in its exception
        # ("Illegal header value b'<key>'") and in its DEBUG trace log. The provider never hands it such a key.
        client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport())
        provider = AnthropicProvider(secret + suffix, base_url=url, client=client)
        with pytest.raises(ProviderError) as chat_error:
            await provider.chat("m", messages)
        with pytest.raises(ProviderError) as stream_error:
            _ = [p async for p in provider.stream_chat("m", messages)]
        for error in (chat_error.value, stream_error.value):
            _assert_no_secret_anywhere(error, secret)
            assert not isinstance(error, ProviderUnavailable)  # a bad key is a mistake, not an outage
        assert hangup.connections == 0  # nothing was sent: the key never reached the HTTP library
        # Through the router (its warning log and the stored error text are built from the exception).
        tasks = {name: route.model_dump() for name, route in DEFAULT_CONFIG.tasks.items()}
        tasks["chat"] = {"provider": "anthropic", "model": "claude-sonnet-5-5",
                         "fallback": {"provider": "ollama", "model": "qwen3:8b"}}

        class Local:
            async def chat(self, model, messages, *, images, json_schema, temperature, max_tokens):
                return "từ Ollama"

        router = Router(ProvidersConfig.model_validate({"tasks": tasks}), {"anthropic": provider, "ollama": Local()})
        with caplog.at_level(logging.DEBUG):
            result = await router.chat("chat", messages)
        assert result.model_version == "ollama:qwen3:8b"
        assert secret not in caplog.text and all(secret not in str(r.__dict__) for r in caplog.records)
        assert any("failed" in r.getMessage() for r in caplog.records), "the failed route is logged"


async def test_the_key_is_scrubbed_from_an_api_message_that_echoes_it():
    def echo(request):
        return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error",
                                                                     "message": f"invalid x-api-key: {KEY}"}})

    provider = AnthropicProvider(KEY, client=httpx.AsyncClient(transport=httpx.MockTransport(echo)))
    with pytest.raises(ProviderError) as info:
        await provider.chat("m", [{"role": "user", "content": "x"}])
    assert KEY not in str(info.value) and "invalid x-api-key" in str(info.value)
    streamed = _stream_of({"type": "error", "error": {"type": "api_error", "message": f"bad {KEY}"}})
    provider = AnthropicProvider(KEY, client=httpx.AsyncClient(transport=httpx.MockTransport(streamed)))
    with pytest.raises(ProviderError) as info:
        _ = [p async for p in provider.stream_chat("m", [{"role": "user", "content": "x"}])]
    assert KEY not in str(info.value) and "bad" in str(info.value)


async def test_a_key_cut_by_the_error_tail_is_still_scrubbed():
    """The tail of a long API message keeps only its end: a key echoed where the cut falls must not leave its end."""
    def echo(request):
        return httpx.Response(400, json={"type": "error", "error": {
            "type": "invalid_request_error", "message": KEY + "x" * (ERROR_TAIL - 10)}})

    provider = AnthropicProvider(KEY, client=httpx.AsyncClient(transport=httpx.MockTransport(echo)))
    with pytest.raises(ProviderError) as info:
        await provider.chat("m", [{"role": "user", "content": "x"}])
    assert KEY[-10:] not in str(info.value) and "***" in str(info.value)


def test_a_key_with_stray_whitespace_is_trimmed_not_rejected(monkeypatch):
    monkeypatch.setenv("LOOPI_DATABASE_URL", "postgresql://x")
    for raw in (f"{KEY}\n", f"{KEY}\r\n", f"  {KEY} ", f"\t{KEY}\r"):
        monkeypatch.setenv("ANTHROPIC_API_KEY", raw)
        assert Settings().anthropic_api_key.get_secret_value() == KEY


async def test_the_missing_key_warning_is_logged_once_per_process(pki_settings, caplog, monkeypatch):
    monkeypatch.setattr(router_module, "_missing_key_warned", False)
    tasks = {name: route.model_dump() for name, route in DEFAULT_CONFIG.tasks.items()}
    tasks["recap"] = {"provider": "ollama", "model": "qwen3:8b",
                      "fallback": {"provider": "anthropic", "model": "claude-haiku-5-5"}}
    config = ProvidersConfig.model_validate({"tasks": tasks})
    for _ in range(3):  # a router is built for every search, chat question and status page
        build_router(config, pki_settings, FakeOllama().client())
    assert caplog.text.count("ANTHROPIC_API_KEY is not set") == 1


async def test_local_providers_stay_usable_without_being_registered():
    nothing = OllamaHealth(reachable=False, present=[], missing=DEFAULT_CONFIG.ollama_models())
    router = Router(DEFAULT_CONFIG, {})  # "local" (asr) is never a router provider
    assert router.task_ready("asr", nothing) and not router.task_ready("caption", nothing)


async def test_images_never_reach_an_unknown_cloud_provider_even_when_it_is_first_in_the_chain():
    cloud = FakeAnthropic()
    stand_in = AnthropicProvider(KEY, base_url="https://newcloud.test", client=cloud.client())

    class Local:
        async def chat(self, model, messages, *, images, json_schema, temperature, max_tokens):
            return f"{model} saw {len(images)} image"

    config = ProvidersConfig.model_validate({"tasks": {n: r.model_dump() for n, r in DEFAULT_CONFIG.tasks.items()}})
    config.tasks["caption"] = TaskRoute.model_construct(
        provider="newcloud", model="m", fallback=TaskRoute(provider="ollama", model="minicpm-v4.6:1b"))
    router = Router(config, {"newcloud": stand_in, "ollama": Local()})
    result = await router.chat("caption", [{"role": "user", "content": "x"}], images=[b"jpeg"])
    assert result.model_version == "ollama:minicpm-v4.6:1b" and result.text == "minicpm-v4.6:1b saw 1 image"
    assert cloud.requests == []  # not one request, not even a refused one
    config.tasks["caption"] = TaskRoute.model_construct(provider="newcloud", model="m", fallback=None)
    with pytest.raises(PrivacyBlocked):
        await router.chat("caption", [{"role": "user", "content": "x"}], images=[b"jpeg"])
    assert cloud.requests == []


async def test_a_refusal_by_the_http_library_is_a_fixed_message_without_the_key_or_a_chain():
    def refuses(request):  # as h11 does for a header value it cannot send: it quotes the value
        raise httpx.LocalProtocolError(f"Illegal header value b'{KEY}'")

    provider = AnthropicProvider(KEY, client=httpx.AsyncClient(transport=httpx.MockTransport(refuses)))
    messages = [{"role": "user", "content": "x"}]
    with pytest.raises(ProviderError, match="refused before it was sent") as chat_error:
        await provider.chat("m", messages)
    with pytest.raises(ProviderError, match="refused before it was sent") as stream_error:
        _ = [p async for p in provider.stream_chat("m", messages)]
    for error in (chat_error.value, stream_error.value):
        _assert_no_secret_anywhere(error, KEY)
        assert not isinstance(error, ProviderUnavailable)


@pytest.mark.parametrize("suffix", ["\n", " "])
async def test_the_real_http_library_refusing_a_key_is_mapped_without_the_key_or_a_chain(suffix, monkeypatch):
    """The second line of defence, with the key check switched off: what h11 really raises is what is mapped."""
    secret = "sk-ant-leak-4242"
    hangup = _Hangup()
    async with hangup as url:
        provider = AnthropicProvider(secret + suffix, base_url=url,
                                     client=httpx.AsyncClient(transport=httpx.AsyncHTTPTransport()))
        monkeypatch.setattr(provider, "_check_key", lambda: None)
        messages = [{"role": "user", "content": "x"}]
        with pytest.raises(ProviderError, match="refused before it was sent") as chat_error:
            await provider.chat("m", messages)
        with pytest.raises(ProviderError, match="refused before it was sent") as stream_error:
            _ = [p async for p in provider.stream_chat("m", messages)]
    for error in (chat_error.value, stream_error.value):
        _assert_no_secret_anywhere(error, secret)

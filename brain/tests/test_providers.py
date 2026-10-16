import base64
import json

import httpx
import pytest

from helpers import DEFAULT_CAPTION, FakeOllama
from loopi_brain.config import Settings
from loopi_brain.providers.base import (
    EMBED_DIM,
    PrivacyBlocked,
    ProviderError,
    ProviderUnavailable,
)
from loopi_brain.providers.config import (
    DEFAULT_CONFIG,
    ProvidersConfig,
    ProvidersConfigError,
    TaskRoute,
    load_providers,
)
from loopi_brain.providers.ollama import OllamaHealth, OllamaProvider
from loopi_brain.providers.router import Router, build_router

DEPLOY_PROVIDERS = __import__("pathlib").Path(__file__).resolve().parents[2] / "deploy" / "providers.yaml"
SETTINGS = Settings(database_url="postgresql://unused", ollama_url="http://models.test:11434")


def _config(**overrides) -> ProvidersConfig:
    tasks = {name: route.model_dump() for name, route in DEFAULT_CONFIG.tasks.items()}
    tasks.update(overrides.pop("tasks", {}))
    return ProvidersConfig.model_validate({"tasks": tasks, **overrides})


def test_committed_providers_yaml_equals_built_in_defaults():
    assert load_providers(DEPLOY_PROVIDERS) == DEFAULT_CONFIG
    assert load_providers(None) == DEFAULT_CONFIG
    assert DEFAULT_CONFIG.privacy.allow_cloud_images is False
    assert DEFAULT_CONFIG.privacy.allow_cloud_audio is False
    assert DEFAULT_CONFIG.ollama_models() == ["minicpm-v4.6:1b", "qwen3-embedding:0.6b", "qwen3:4b", "qwen3:8b"]


@pytest.mark.parametrize(
    "tasks",
    [
        {"caption": {"provider": "openai", "model": "gpt"}},
        {"asr": {"provider": "ollama", "model": "whisper"}},
        {"asr": {"provider": "local", "model": "faster-whisper/tiny"}},
        {"embed": {"provider": "local", "model": "x"}},
        {"caption": {"provider": "ollama", "model": "m", "fallback": {"provider": "anthropic", "model": "c"}}},
        {"embed": {"provider": "anthropic", "model": "c"}},
        {"vlog": {"provider": "ollama", "model": "qwen3:8b"}},
    ],
)
def test_invalid_routes_are_rejected_at_load_time(tmp_path, tasks):
    with pytest.raises(ValueError):
        _config(tasks=tasks)
    data = {"tasks": {**{n: r.model_dump() for n, r in DEFAULT_CONFIG.tasks.items()}, **tasks}}
    path = tmp_path / "providers.yaml"
    path.write_text(json.dumps(data))  # JSON is valid YAML
    with pytest.raises(ProvidersConfigError):
        load_providers(path)


def test_missing_task_typo_or_unreadable_file_is_a_config_error(tmp_path):
    path = tmp_path / "providers.yaml"
    path.write_text("tasks:\n  caption: {provider: ollama, model: m}\n")
    with pytest.raises(ProvidersConfigError, match="missing"):
        load_providers(path)
    path.write_text("privacy: {allow_cloud_image: true}\ntasks: {}\n")
    with pytest.raises(ProvidersConfigError):
        load_providers(path)
    path.write_text("tasks: [unclosed\n")
    with pytest.raises(ProvidersConfigError):
        load_providers(path)
    with pytest.raises(ProvidersConfigError, match="cannot read"):
        load_providers(tmp_path / "absent.yaml")


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("tasks:\n  caption: {provider: ollama, model: m, api_key: sk-ant-SECRET}\n",
         "tasks.caption.api_key: Extra inputs"),
        ("privacy: sk-ant-SECRET\ntasks: {}\n", "privacy: Input should be a valid dictionary"),
        ("sk-ant-SECRET\n", "file: Input should be a valid dictionary"),
        ("api_key: sk-ant-SECRET\ntasks: [unclosed\n", "line 3, column 1"),
        ("api_key: sk-ant-SECRET\x00\n", "cannot parse"),
    ],
)
def test_a_config_error_says_where_and_why_but_never_quotes_the_file(tmp_path, text, shown):
    path = tmp_path / "providers.yaml"
    path.write_text(text)
    with pytest.raises(ProvidersConfigError) as caught:
        load_providers(path)
    assert shown in str(caught.value) and "SECRET" not in str(caught.value)


async def test_ollama_chat_sends_schema_images_and_no_thinking():
    fake = FakeOllama()
    provider = OllamaProvider("http://models.test:11434/", keep_alive="30m", client=fake.client())
    text = await provider.chat(
        "minicpm-v4.6:1b",
        [{"role": "user", "content": "describe"}],
        images=[b"\xff\xd8jpeg"],
        json_schema={"type": "object"},
        temperature=0.0,
        max_tokens=400,
    )
    assert json.loads(text) == DEFAULT_CAPTION
    path, body = fake.requests[0]
    assert path == "/api/chat"
    assert body["stream"] is False and body["think"] is False and body["keep_alive"] == "30m"
    assert body["format"] == {"type": "object"} and body["options"] == {"temperature": 0.0, "num_predict": 400}
    assert body["messages"] == [
        {"role": "user", "content": "describe", "images": [base64.b64encode(b"\xff\xd8jpeg").decode()]}
    ]


async def test_ollama_chat_without_schema_or_images_sends_neither():
    fake = FakeOllama(caption="plain text")
    provider = OllamaProvider("http://models.test:11434", client=fake.client())
    assert await provider.chat("qwen3:8b", [{"role": "user", "content": "hi"}]) == "plain text"
    body = fake.requests[0][1]
    assert "format" not in body and "images" not in body["messages"][0]
    assert body["options"] == {"temperature": 0.0}  # no token limit unless asked


async def test_ollama_embed_returns_vectors():
    fake = FakeOllama()
    provider = OllamaProvider("http://models.test:11434", client=fake.client())
    vectors = await provider.embed("qwen3-embedding:0.6b", ["a", "b"])
    assert len(vectors) == 2 and len(vectors[0]) == EMBED_DIM
    assert fake.requests[0] == ("/api/embed", {"model": "qwen3-embedding:0.6b", "input": ["a", "b"], "keep_alive": "30m"})


def _transport_raising(error_type):
    def handler(request):
        raise error_type("simulated", request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_connection_failures_and_missing_models_are_unavailable():
    fake = FakeOllama()
    provider = OllamaProvider("http://models.test:11434", client=fake.client())
    with pytest.raises(ProviderUnavailable, match="HTTP 404.*not found"):  # model not pulled: configuration
        await provider.chat("missing:1b", [{"role": "user", "content": "x"}])
    with pytest.raises(ProviderUnavailable, match="HTTP 404"):
        await provider.embed("missing-embedding", ["a"])
    fake.down = True  # connection refused
    with pytest.raises(ProviderUnavailable):
        await provider.embed("qwen3-embedding:0.6b", ["a"])
    slow_connect = OllamaProvider("http://models.test:11434", client=_transport_raising(httpx.ConnectTimeout))
    with pytest.raises(ProviderUnavailable):
        await slow_connect.chat("qwen3:8b", [{"role": "user", "content": "x"}])


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.RemoteProtocolError])
async def test_failures_after_connecting_are_provider_errors(error_type):
    provider = OllamaProvider("http://models.test:11434", client=_transport_raising(error_type))
    with pytest.raises(ProviderError) as info:
        await provider.chat("qwen3:8b", [{"role": "user", "content": "x"}])
    assert not isinstance(info.value, ProviderUnavailable)  # normal retries, then the dead-letter list


async def test_server_errors_are_provider_errors():
    fake = FakeOllama()
    fake.error = 500
    provider = OllamaProvider("http://models.test:11434", client=fake.client())
    with pytest.raises(ProviderError, match="HTTP 500") as info:
        await provider.chat("minicpm-v4.6:1b", [{"role": "user", "content": "x"}])
    assert not isinstance(info.value, ProviderUnavailable)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="<html>oops</html>"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["a list"]),
        httpx.Response(200, json={"message": {}}),
    ],
)
async def test_unusable_chat_responses_are_provider_errors(response):
    provider = OllamaProvider(
        "http://models.test:11434", client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response))
    )
    with pytest.raises(ProviderError):
        await provider.chat("qwen3:8b", [{"role": "user", "content": "x"}])


async def test_embed_without_embeddings_is_provider_error():
    provider = OllamaProvider(
        "http://models.test:11434",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
    )
    with pytest.raises(ProviderError):
        await provider.embed("qwen3-embedding:0.6b", ["a"])


async def test_health_reports_missing_models_and_unreachable_server():
    fake = FakeOllama(models=["qwen3:8b", "minicpm-v4.6:1b"])
    router = build_router(DEFAULT_CONFIG, SETTINGS, fake.client())
    health = await router.health()
    assert health.reachable is True
    assert health.present == ["minicpm-v4.6:1b", "qwen3:8b"]
    assert health.missing == ["qwen3-embedding:0.6b", "qwen3:4b"]
    assert fake.timeouts == [("/api/tags", {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0})]
    assert router.task_ready("caption", health) and not router.task_ready("embed", health)
    fake.down = True
    health = await router.health()
    assert health.reachable is False and health.missing == DEFAULT_CONFIG.ollama_models()
    assert not router.task_ready("caption", health) and not router.task_ready("embed", health)


def test_routes_the_health_check_cannot_see_count_as_ready():
    config = _config()
    config.tasks["recap"] = TaskRoute.model_construct(provider="anthropic", model="claude", fallback=None)
    router = Router(config, {"anthropic": StubProvider()})  # ANTHROPIC_API_KEY is set
    nothing = OllamaHealth(reachable=False, present=[], missing=config.ollama_models())
    assert router.task_ready("recap", nothing) and not router.task_ready("caption", nothing)
    assert not Router(config, {}).task_ready("recap", nothing)  # no key: that route cannot run


async def test_pull_downloads_a_model():
    fake = FakeOllama(models=[])
    provider = OllamaProvider("http://models.test:11434", client=fake.client())
    await provider.pull("qwen3:4b")
    assert fake.requests == [("/api/pull", {"model": "qwen3:4b", "stream": False})]
    bad = OllamaProvider(
        "http://models.test:11434",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "x"}))),
    )
    with pytest.raises(ProviderError):
        await bad.pull("qwen3:4b")


class StubProvider:
    def __init__(self, error: Exception | None = None, dims: int = EMBED_DIM):
        self.error, self.dims, self.calls = error, dims, []

    async def chat(self, model, messages, *, images, json_schema, temperature, max_tokens=None):
        self.calls.append(("chat", model, images))
        if self.error:
            raise self.error
        return f"answer from {model}"

    async def embed(self, model, texts):
        self.calls.append(("embed", model, texts))
        if self.error:
            raise self.error
        return [[0.0] * self.dims for _ in texts]


def _router(config, **providers) -> Router:
    return Router(config, providers)


async def test_router_reports_model_version_of_the_route_that_answered():
    stub = StubProvider()
    router = _router(DEFAULT_CONFIG, ollama=stub)
    result = await router.chat("caption", [{"role": "user", "content": "x"}], images=[b"img"], json_schema={})
    assert result.text == "answer from minicpm-v4.6:1b"
    assert result.model_version == "ollama:minicpm-v4.6:1b"
    assert router.model_version("asr") == "local:faster-whisper/large-v3-turbo-int8"
    assert router.route("embed") == TaskRoute(provider="ollama", model="qwen3-embedding:0.6b")


async def test_router_falls_back_on_error_and_raises_the_last_error():
    config = _config(tasks={"recap": {"provider": "ollama", "model": "qwen3:8b",
                                      "fallback": {"provider": "ollama", "model": "qwen3:4b"}}})

    class FirstFails(StubProvider):
        async def chat(self, model, messages, *, images, json_schema, temperature, max_tokens=None):
            if model == "qwen3:8b":
                raise ProviderUnavailable("down")
            return await super().chat(model, messages, images=images, json_schema=json_schema,
                                      temperature=temperature, max_tokens=max_tokens)

    result = await _router(config, ollama=FirstFails()).chat("recap", [{"role": "user", "content": "x"}])
    assert result.model_version == "ollama:qwen3:4b"
    with pytest.raises(ProviderError, match="boom"):
        await _router(config, ollama=StubProvider(ProviderError("boom"))).chat(
            "recap", [{"role": "user", "content": "x"}]
        )


async def test_router_never_sends_images_to_a_cloud_provider():
    # P2a's config loader rejects cloud providers, so build the cloud route without validation.
    config = _config()
    config.tasks["caption"] = TaskRoute.model_construct(provider="anthropic", model="claude", fallback=None)
    stub = StubProvider()
    router = _router(config, ollama=stub, anthropic=stub)
    with pytest.raises(PrivacyBlocked):
        await router.chat("caption", [{"role": "user", "content": "x"}], images=[b"img"])
    assert stub.calls == []
    # Text without images may go to the cloud route; a local fallback still serves images.
    assert (await router.chat("caption", [{"role": "user", "content": "x"}])).model_version == "anthropic:claude"
    config.tasks["caption"] = TaskRoute.model_construct(
        provider="anthropic", model="claude", fallback=TaskRoute(provider="ollama", model="minicpm-v4.6:1b")
    )
    result = await router.chat("caption", [{"role": "user", "content": "x"}], images=[b"img"])
    assert result.model_version == "ollama:minicpm-v4.6:1b"


async def test_router_rejects_wrong_embedding_dimensions():
    router = _router(DEFAULT_CONFIG, ollama=StubProvider(dims=768))
    with pytest.raises(ProviderError, match="1024"):
        await router.embed("embed", ["a"])
    result = await _router(DEFAULT_CONFIG, ollama=StubProvider()).embed("embed", ["a", "b"])
    assert len(result.vectors) == 2 and result.model_version == "ollama:qwen3-embedding:0.6b"


async def test_text_models_leave_memory_soon_and_get_a_context_window():
    fake = FakeOllama()
    provider = OllamaProvider("http://models.test:11434", keep_alive="30m", client=fake.client(),
                              text_models=frozenset({"qwen3:8b"}), text_keep_alive="1m", num_ctx=8192,
                              text_timeout=900.0)
    await provider.chat("qwen3:8b", [{"role": "user", "content": "x"}], max_tokens=100)
    await provider.chat("minicpm-v4.6:1b", [{"role": "user", "content": "x"}], images=[b"img"])
    await provider.embed("qwen3-embedding:0.6b", ["a"])
    (_, text), (_, vision), (_, embed) = fake.requests
    assert text["keep_alive"] == "1m" and text["options"] == {"temperature": 0.0, "num_predict": 100, "num_ctx": 8192}
    assert vision["keep_alive"] == "30m" and "num_ctx" not in vision["options"]
    assert embed["keep_alive"] == "30m"
    # A recap on a CPU takes minutes: only the text model waits that long.
    (_, text_timeout), (_, vision_timeout), _ = fake.timeouts
    assert text_timeout == {"connect": 5.0, "read": 900.0, "write": 900.0, "pool": 900.0}
    assert vision_timeout["read"] == 5.0  # the client's own timeout


async def test_build_router_gives_every_text_task_model_the_text_options():
    assert DEFAULT_CONFIG.ollama_text_models() == {"qwen3:4b", "qwen3:8b"}
    fake = FakeOllama()
    settings = Settings(database_url="postgresql://unused", ollama_url="http://models.test:11434",
                        ollama_text_keep_alive="2m", llm_num_ctx=4096, llm_timeout_s=600)
    router = build_router(DEFAULT_CONFIG, settings, fake.client())
    for task in ("chapter_summary", "recap", "chat"):
        await router.chat(task, [{"role": "user", "content": "x"}])
    await router.chat("caption", [{"role": "user", "content": "x"}], images=[b"img"])
    assert [(b["model"], b["keep_alive"], b["options"].get("num_ctx")) for _, b in fake.requests] == [
        ("qwen3:4b", "2m", 4096), ("qwen3:8b", "2m", 4096), ("qwen3:8b", "2m", 4096),
        ("minicpm-v4.6:1b", "30m", None),
    ]
    assert [t["read"] for _, t in fake.timeouts] == [600.0, 600.0, 600.0, 5.0]


async def test_fake_ollama_answers_chats_with_its_reply_hook():
    fake = FakeOllama()
    fake.reply = lambda body: {"echo": body["model"]}
    provider = OllamaProvider("http://models.test:11434", client=fake.client())
    assert json.loads(await provider.chat("qwen3:4b", [{"role": "user", "content": "x"}])) == {"echo": "qwen3:4b"}

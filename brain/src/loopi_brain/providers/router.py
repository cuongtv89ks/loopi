"""Route each pipeline task to its provider, with privacy rules and fallback (spec §9.3)."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from ..config import Settings
from .anthropic import AnthropicProvider
from .base import EMBED_DIM, PrivacyBlocked, Provider, ProviderError, ProviderUnavailable, is_cloud
from .config import IMAGE_TASKS, ProvidersConfig, TaskRoute
from .ollama import OllamaHealth, OllamaProvider

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatResult:
    text: str
    model_version: str


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]]
    model_version: str


def model_version(route: TaskRoute) -> str:
    return f"{route.provider}:{route.model}"


class Router:
    def __init__(self, config: ProvidersConfig, providers: dict[str, Provider]):
        self.config = config
        self.providers = providers

    def route(self, task: str) -> TaskRoute:
        return self.config.tasks[task]

    def model_version(self, task: str) -> str:
        return model_version(self.route(task))

    def _allowed(self, task: str, *, images: bool) -> list[TaskRoute]:
        """The routes of ``task`` that may receive this request: with images, local providers only (unless
        allow_cloud_images); routes whose provider is not set up (no ANTHROPIC_API_KEY) are left out."""
        steps = [s for s in self.route(task).chain() if s.provider in self.providers]
        if images and not self.config.privacy.allow_cloud_images:
            steps = [s for s in steps if not is_cloud(s.provider)]
        return steps

    def _usable(self, task: str, *, images: bool) -> list[TaskRoute]:
        steps = self._allowed(task, images=images)
        if not steps:
            if images and any(is_cloud(s.provider) for s in self.route(task).chain()):
                raise PrivacyBlocked(f"task {task}: images may not be sent to a cloud provider")
            raise ProviderUnavailable(f"task {task}: no configured provider (is ANTHROPIC_API_KEY set?)")
        return steps

    async def chat(
        self,
        task: str,
        messages: list[dict],
        images: list[bytes] | None = None,
        json_schema: dict | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> ChatResult:
        error: ProviderError | None = None
        for step in self._usable(task, images=bool(images)):
            try:
                text = await self.providers[step.provider].chat(
                    step.model, messages, images=images, json_schema=json_schema, temperature=temperature,
                    max_tokens=max_tokens,
                )
            except ProviderError as exc:
                log.warning("task %s: %s failed: %s", task, model_version(step), exc)
                error = exc
                continue
            return ChatResult(text, model_version(step))
        assert error is not None
        raise error

    async def stream_chat(
        self, task: str, messages: list[dict], temperature: float = 0.0, max_tokens: int | None = None
    ) -> AsyncIterator[tuple[str, str]]:
        """Yield (piece of text, model_version) as the answer is written. A route that fails before its first piece
        falls back to the next one; a failure after that is raised (part of an answer has been shown)."""
        error: ProviderError | None = None
        for step in self._usable(task, images=False):
            stream = self.providers[step.provider].stream_chat(
                step.model, messages, temperature=temperature, max_tokens=max_tokens
            )
            try:
                first = await anext(stream)
            except StopAsyncIteration:
                return  # an empty answer
            except ProviderError as exc:
                log.warning("task %s: %s failed: %s", task, model_version(step), exc)
                error = exc
                await stream.aclose()
                continue
            version = model_version(step)
            yield first, version
            async for piece in stream:
                yield piece, version
            return
        assert error is not None
        raise error

    async def embed(self, task: str, texts: list[str]) -> EmbedResult:
        error: ProviderError | None = None
        for step in self._usable(task, images=False):
            try:
                vectors = await self.providers[step.provider].embed(step.model, texts)
                if len(vectors) != len(texts) or any(len(v) != EMBED_DIM for v in vectors):
                    raise ProviderError(
                        f"{model_version(step)} returned {len(vectors)} vectors of "
                        f"{sorted({len(v) for v in vectors})} dims; expected {len(texts)} of {EMBED_DIM}"
                    )
            except ProviderError as exc:
                log.warning("task %s: %s failed: %s", task, model_version(step), exc)
                error = exc
                continue
            return EmbedResult(vectors, model_version(step))
        assert error is not None
        raise error

    async def health(self) -> OllamaHealth:
        """Is the Ollama server reachable, and which configured models does it have?"""
        return await self.providers["ollama"].health(self.config.ollama_models())

    def task_ready(self, task: str, health: OllamaHealth) -> bool:
        """Can ``task`` run now: does one of the routes its requests may take have its model on the reachable
        Ollama server? A cloud route counts only for text, and only when its provider is set up; it cannot be
        checked from here and counts as ready."""
        steps = self._allowed(task, images=task in IMAGE_TASKS)
        return any(s.provider != "ollama" or s.model in health.present for s in steps)


def build_router(config: ProvidersConfig, settings: Settings, client: httpx.AsyncClient | None = None) -> Router:
    ollama = OllamaProvider(
        settings.ollama_url,
        keep_alive=settings.ollama_keep_alive,
        client=client,
        text_models=config.ollama_text_models(),
        text_keep_alive=settings.ollama_text_keep_alive,
        num_ctx=settings.llm_num_ctx,
        text_timeout=settings.llm_timeout_s,
    )
    providers: dict[str, Provider] = {"ollama": ollama}
    if settings.anthropic_api_key is not None:
        providers["anthropic"] = AnthropicProvider(
            settings.anthropic_api_key.get_secret_value(), base_url=settings.anthropic_url, client=client
        )
    elif "anthropic" in config.providers_used():
        log.warning("providers.yaml routes text tasks to anthropic but ANTHROPIC_API_KEY is not set: those routes are skipped")
    return Router(config, providers)

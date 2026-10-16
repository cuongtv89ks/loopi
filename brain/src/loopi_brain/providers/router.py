"""Route each pipeline task to its provider, with privacy rules and fallback (spec §9.3)."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..config import Settings
from .base import CLOUD_PROVIDERS, EMBED_DIM, PrivacyBlocked, Provider, ProviderError
from .config import ProvidersConfig, TaskRoute
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

    def _usable(self, task: str, *, images: bool) -> list[TaskRoute]:
        steps = self.route(task).chain()
        if images and not self.config.privacy.allow_cloud_images:
            allowed = [s for s in steps if s.provider not in CLOUD_PROVIDERS]
            if not allowed:
                raise PrivacyBlocked(f"task {task}: images may not be sent to a cloud provider")
            return allowed
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
        """Can ``task`` run now: does one of its routes have its model on the reachable Ollama server?

        Routes to other providers cannot be checked here and count as ready.
        """
        return any(s.provider != "ollama" or s.model in health.present for s in self.route(task).chain())


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
    return Router(config, {"ollama": ollama})

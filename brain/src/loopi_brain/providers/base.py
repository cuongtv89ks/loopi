"""Provider interface and the errors every provider raises."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

EMBED_DIM = 1024
# Providers that run on this machine. Every other provider is a cloud provider, including any added later:
# the privacy gate is an allow-list, so a new provider cannot receive images by accident (P2a deferred item).
LOCAL_PROVIDERS = frozenset({"ollama", "local"})


def is_cloud(provider: str) -> bool:
    return provider not in LOCAL_PROVIDERS


class ProviderError(Exception):
    """The provider failed a request it should serve: HTTP errors other than 404, read/write timeouts,
    dropped connections, unusable bodies, wrong dimensions. Retried normally, then dead-lettered."""


class ProviderUnavailable(ProviderError):
    """The provider cannot serve the task right now: connection refused, DNS failure, connect timeout,
    or HTTP 404 (model not pulled). A configuration/availability problem, not an item problem."""


class PrivacyBlocked(Exception):
    """The request carries images and every route for the task is a cloud provider (audio never reaches the
    router: ASR runs in the worker)."""


class Provider(Protocol):
    async def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        images: list[bytes] | None,
        json_schema: dict | None,
        temperature: float,
        max_tokens: int | None,
    ) -> str: ...

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]: ...

    def stream_chat(
        self, model: str, messages: list[dict], *, temperature: float, max_tokens: int | None
    ) -> AsyncIterator[str]: ...

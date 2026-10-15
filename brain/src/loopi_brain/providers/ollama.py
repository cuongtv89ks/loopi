"""Ollama HTTP API client (https://github.com/ollama/ollama/blob/main/docs/api.md)."""
from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx

from .base import ProviderError, ProviderUnavailable

# CPU inference of one image or one long prompt can take minutes.
DEFAULT_TIMEOUT = httpx.Timeout(300.0, connect=5.0)
# The dispatcher asks every minute whether the server can take work; it must answer fast.
HEALTH_TIMEOUT = httpx.Timeout(5.0)
ERROR_TAIL = 500


@dataclass(frozen=True)
class OllamaHealth:
    reachable: bool
    present: list[str]
    missing: list[str]


def _tagged(model: str) -> str:
    return model if ":" in model else f"{model}:latest"


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str, *, keep_alive: str = "30m", client: httpx.AsyncClient | None = None):
        self._base = base_url.rstrip("/")
        self._keep_alive = keep_alive
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, payload: dict | None = None, timeout=None) -> dict:
        kwargs: dict = {"json": payload} if payload is not None else {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            response = await self._client.request(method, f"{self._base}{path}", **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:  # refused, DNS failure, connect timeout
            raise ProviderUnavailable(f"ollama {path}: {type(exc).__name__}: {exc}") from exc
        except httpx.TransportError as exc:  # read/write timeout, dropped connection: this request failed
            raise ProviderError(f"ollama {path}: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("error") or response.text
            except ValueError:
                detail = response.text
            message = f"ollama {path} HTTP {response.status_code}: {str(detail)[-ERROR_TAIL:]}"
            if response.status_code == 404:  # model not pulled: configuration, shown on the status page
                raise ProviderUnavailable(message)
            raise ProviderError(message)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(f"ollama {path}: response is not JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError(f"ollama {path}: unexpected response {str(data)[:ERROR_TAIL]}")
        return data

    async def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        images: list[bytes] | None = None,
        json_schema: dict | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        msgs = [dict(m) for m in messages]
        if images:
            msgs[-1]["images"] = [base64.b64encode(img).decode("ascii") for img in images]
        options: dict = {"temperature": temperature}
        if max_tokens is not None:
            options["num_predict"] = max_tokens  # a looping model stops instead of filling the context
        payload: dict = {
            "model": model,
            "messages": msgs,
            "stream": False,
            "think": False,
            "keep_alive": self._keep_alive,
            "options": options,
        }
        if json_schema is not None:
            payload["format"] = json_schema
        data = await self._request("POST", "/api/chat", payload)
        content = (data.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise ProviderError("ollama /api/chat: response has no message content")
        return content

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        data = await self._request(
            "POST", "/api/embed", {"model": model, "input": texts, "keep_alive": self._keep_alive}
        )
        vectors = data.get("embeddings")
        if not isinstance(vectors, list):
            raise ProviderError("ollama /api/embed: response has no embeddings")
        return vectors

    async def health(self, models: list[str]) -> OllamaHealth:
        try:
            data = await self._request("GET", "/api/tags", timeout=HEALTH_TIMEOUT)
        except ProviderError:
            return OllamaHealth(reachable=False, present=[], missing=list(models))
        installed = {_tagged(m.get("name", "")) for m in data.get("models", []) if isinstance(m, dict)}
        present = [m for m in models if _tagged(m) in installed]
        return OllamaHealth(True, present, [m for m in models if _tagged(m) not in installed])

    async def pull(self, model: str) -> None:
        """Download ``model`` into the Ollama server (blocks until done; GBs on first run)."""
        data = await self._request(
            "POST", "/api/pull", {"model": model, "stream": False}, timeout=httpx.Timeout(None, connect=5.0)
        )
        if data.get("status") != "success":
            raise ProviderError(f"ollama pull {model}: {data}")

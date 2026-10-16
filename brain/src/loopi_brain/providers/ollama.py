"""Ollama HTTP API client (https://github.com/ollama/ollama/blob/main/docs/api.md)."""
from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
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

    def __init__(
        self,
        base_url: str,
        *,
        keep_alive: str = "30m",
        client: httpx.AsyncClient | None = None,
        text_models: frozenset[str] = frozenset(),
        text_keep_alive: str = "1m",
        num_ctx: int | None = None,
        text_timeout: float | None = None,
    ):
        """``text_models`` (the big text models) stay loaded only ``text_keep_alive`` after a request, get a
        ``num_ctx`` context window and wait up to ``text_timeout`` seconds for an answer; every other model stays
        ``keep_alive``, with Ollama's default context and the client's timeout."""
        self._base = base_url.rstrip("/")
        self._keep_alive = keep_alive
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        self._text_models = text_models
        self._text_keep_alive = text_keep_alive
        self._num_ctx = num_ctx
        self._text_timeout = text_timeout

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
            raise _http_error(path, response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(f"ollama {path}: response is not JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError(f"ollama {path}: unexpected response {str(data)[:ERROR_TAIL]}")
        return data

    def _timeout_for(self, model: str) -> httpx.Timeout | None:
        """A text model writes a whole recap before Ollama replies (CPU: minutes); others keep the client's timeout."""
        if model in self._text_models and self._text_timeout is not None:
            return httpx.Timeout(self._text_timeout, connect=5.0)
        return None

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
        payload = self._chat_payload(model, messages, images, json_schema, temperature, max_tokens)
        data = await self._request("POST", "/api/chat", payload, timeout=self._timeout_for(model))
        content = (data.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise ProviderError("ollama /api/chat: response has no message content")
        return content

    def _chat_payload(self, model, messages, images, json_schema, temperature, max_tokens) -> dict:
        msgs = [dict(m) for m in messages]
        if images:
            msgs[-1]["images"] = [base64.b64encode(img).decode("ascii") for img in images]
        text_model = model in self._text_models
        options: dict = {"temperature": temperature}
        if max_tokens is not None:
            options["num_predict"] = max_tokens  # a looping model stops instead of filling the context
        if text_model and self._num_ctx is not None:
            options["num_ctx"] = self._num_ctx  # Ollama silently drops the start of a longer prompt
        payload: dict = {
            "model": model,
            "messages": msgs,
            "stream": False,
            "think": False,
            "keep_alive": self._text_keep_alive if text_model else self._keep_alive,
            "options": options,
        }
        if json_schema is not None:
            payload["format"] = json_schema
        return payload

    async def stream_chat(
        self, model: str, messages: list[dict], *, temperature: float = 0.0, max_tokens: int | None = None
    ) -> AsyncIterator[str]:
        """Yield the answer's text as Ollama writes it (``stream: true``: one JSON object per line)."""
        payload = self._chat_payload(model, messages, None, None, temperature, max_tokens)
        payload["stream"] = True
        timeout = self._timeout_for(model)
        kwargs: dict = {"timeout": timeout} if timeout is not None else {}
        try:
            async with self._client.stream("POST", f"{self._base}/api/chat", json=payload, **kwargs) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise _http_error("/api/chat", response)
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except ValueError as exc:
                        raise ProviderError("ollama /api/chat: a streamed line is not JSON") from exc
                    if not isinstance(data, dict):
                        raise ProviderError(f"ollama /api/chat: unexpected stream line {line[:ERROR_TAIL]}")
                    if data.get("error"):
                        raise ProviderError(f"ollama /api/chat: {str(data['error'])[-ERROR_TAIL:]}")
                    piece = (data.get("message") or {}).get("content")
                    if isinstance(piece, str) and piece:
                        yield piece
                    if data.get("done"):
                        return
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderUnavailable(f"ollama /api/chat: {type(exc).__name__}: {exc}") from exc
        except httpx.TransportError as exc:
            raise ProviderError(f"ollama /api/chat: {type(exc).__name__}: {exc}") from exc

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


def _http_error(path: str, response: httpx.Response) -> ProviderError:
    try:
        detail = response.json().get("error") or response.text
    except ValueError:
        detail = response.text
    message = f"ollama {path} HTTP {response.status_code}: {str(detail)[-ERROR_TAIL:]}"
    if response.status_code == 404:  # model not pulled: configuration, shown on the status page
        return ProviderUnavailable(message)
    return ProviderError(message)

"""Anthropic Messages API client for text tasks only (spec §9.3: optional, cloud gets text only).

Plain httpx, no SDK dependency. Requests never carry images or audio. Notes on the API this relies on:
- ``POST /v1/messages`` with headers ``x-api-key`` and ``anthropic-version: 2023-06-01``; the system prompt is
  the top-level ``system`` field, not a message.
- Structured output is ``output_config.format = {"type": "json_schema", "schema": ...}``; the schema may not use
  length or number limits and every object needs ``additionalProperties: false`` (``strict_schema``). The
  caller still clips over-long answers, as for Ollama.
- Claude Haiku 5.5 and Sonnet 5.5 reject non-default sampling parameters, so no ``temperature`` is sent.
- They think by default, and ``max_tokens`` caps thinking **and** answer together: a small cap can end before
  any text. So thinking is turned off where the model allows it (Haiku 5.5: ``{"type": "disabled"}``, valid at
  effort ≤ high; Sonnet 5.5 rejects "disabled" and uses ``{"type": "between_tools"}``), ``max_tokens`` is at least
  1024, and any other model gets 8192 so its thinking leaves room for the answer; ``output_config.effort`` is
  "low"; only ``text`` content blocks are read; an answer cut off before any text is an error.
- Streaming is Server-Sent Events; text arrives as ``content_block_delta`` events with a ``text_delta``.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from .base import PrivacyBlocked, ProviderError, ProviderUnavailable

API_VERSION = "2023-06-01"
DEFAULT_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
DEFAULT_MAX_TOKENS = 2048
EFFORT = "low"
THINKING_OFF = {"claude-haiku-5-5": {"type": "disabled"}, "claude-sonnet-5-5": {"type": "between_tools"}}
MIN_MAX_TOKENS = 1024  # thinking off: room for any answer the brain asks for (it clips longer ones)
THINKING_MAX_TOKENS = 8192  # a model that always thinks: thinking and answer share this budget
ERROR_TAIL = 500
# Rate limits, overload (529) and server errors pass: the router falls back, a job does not spend a retry.
TEMPORARY = frozenset({429, 500, 502, 503, 504, 529})
_UNSUPPORTED = frozenset({"maxLength", "minLength", "minimum", "maximum", "maxItems", "minItems"})


def strict_schema(schema):
    """``schema`` without the limits structured output does not support, every object closed."""
    if isinstance(schema, list):
        return [strict_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {key: strict_schema(value) for key, value in schema.items() if key not in _UNSUPPORTED}
    if out.get("type") == "object":
        out["additionalProperties"] = False
    return out


def _error(path: str, response: httpx.Response) -> ProviderError:
    try:
        detail = (response.json().get("error") or {}).get("message") or response.text
    except (ValueError, AttributeError):
        detail = response.text
    message = f"anthropic {path} HTTP {response.status_code}: {str(detail)[-ERROR_TAIL:]}"
    return ProviderUnavailable(message) if response.status_code in TEMPORARY else ProviderError(message)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, *, base_url: str = "https://api.anthropic.com",
                 client: httpx.AsyncClient | None = None):
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)

    def __repr__(self) -> str:  # never shows the key
        return f"AnthropicProvider({self._base!r})"

    def _headers(self) -> dict:
        return {"x-api-key": self._key, "anthropic-version": API_VERSION, "content-type": "application/json"}

    @staticmethod
    def _body(model: str, messages: list[dict], json_schema: dict | None, max_tokens: int | None) -> dict:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        thinking = THINKING_OFF.get(model)
        floor = MIN_MAX_TOKENS if thinking else THINKING_MAX_TOKENS
        body: dict = {
            "model": model,
            "max_tokens": max(max_tokens or DEFAULT_MAX_TOKENS, floor),
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"],
            "output_config": {"effort": EFFORT},
        }
        if thinking:
            body["thinking"] = dict(thinking)
        if system:
            body["system"] = system
        if json_schema is not None:
            body["output_config"]["format"] = {"type": "json_schema", "schema": strict_schema(json_schema)}
        return body

    async def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        images: list[bytes] | None = None,
        json_schema: dict | None = None,
        temperature: float = 0.0,  # not sent (see the module docstring)
        max_tokens: int | None = None,
    ) -> str:
        if images:
            raise PrivacyBlocked("images are never sent to anthropic")
        try:
            response = await self._client.post(
                f"{self._base}/v1/messages", headers=self._headers(), json=self._body(model, messages, json_schema, max_tokens)
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderUnavailable(f"anthropic /v1/messages: {type(exc).__name__}: {exc}") from exc
        except httpx.TransportError as exc:
            raise ProviderError(f"anthropic /v1/messages: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise _error("/v1/messages", response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("anthropic /v1/messages: response is not JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError("anthropic /v1/messages: unexpected response")
        if data.get("stop_reason") == "refusal":
            raise ProviderError("anthropic /v1/messages: the model declined the request")
        text = "".join(
            block.get("text", "") for block in data.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if not text:
            if data.get("stop_reason") == "max_tokens":
                raise ProviderError("anthropic /v1/messages: max_tokens was used up before any answer text")
            raise ProviderError(f"anthropic /v1/messages: no text in the answer (stop_reason {data.get('stop_reason')})")
        return text

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        raise ProviderError("anthropic serves no embeddings")

    async def stream_chat(
        self, model: str, messages: list[dict], *, temperature: float = 0.0, max_tokens: int | None = None
    ) -> AsyncIterator[str]:
        body = {**self._body(model, messages, None, max_tokens), "stream": True}
        try:
            async with self._client.stream(
                "POST", f"{self._base}/v1/messages", headers=self._headers(), json=body
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise _error("/v1/messages", response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue  # "event:" lines repeat the type that the data carries
                    try:
                        data = json.loads(line[5:])
                    except ValueError as exc:
                        raise ProviderError("anthropic /v1/messages: a streamed event is not JSON") from exc
                    kind = data.get("type")
                    if kind == "error":
                        error = data.get("error") or {}
                        message = f"anthropic /v1/messages: {error.get('type')}: {error.get('message')}"
                        raise ProviderUnavailable(message) if error.get("type") == "overloaded_error" else ProviderError(message)
                    if kind == "content_block_delta" and (data.get("delta") or {}).get("type") == "text_delta":
                        text = data["delta"].get("text") or ""
                        if text:
                            yield text
                    elif kind == "message_delta" and (data.get("delta") or {}).get("stop_reason") == "refusal":
                        raise ProviderError("anthropic /v1/messages: the model declined the request")
                    elif kind == "message_stop":
                        return
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderUnavailable(f"anthropic /v1/messages: {type(exc).__name__}: {exc}") from exc
        except httpx.TransportError as exc:
            raise ProviderError(f"anthropic /v1/messages: {type(exc).__name__}: {exc}") from exc

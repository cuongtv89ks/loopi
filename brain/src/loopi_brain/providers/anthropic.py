"""Anthropic Messages API client for text tasks only (spec §9.3: optional, cloud gets text only).

Plain httpx, no SDK dependency. Requests never carry images or audio. Notes on the API this relies on:
- ``POST /v1/messages`` with headers ``x-api-key`` and ``anthropic-version: 2023-06-01``; the system prompt is
  the top-level ``system`` field, not a message.
- Structured output is ``output_config.format = {"type": "json_schema", "schema": ...}``; the schema may not use
  length or number limits and every object needs ``additionalProperties: false`` (``strict_schema``). The
  caller still clips over-long answers, as for Ollama.
- Claude Haiku 5.5 and Sonnet 5.5 reject non-default sampling parameters, so no ``temperature`` is sent (to any
  model).
- They think by default, and ``max_tokens`` caps thinking **and** answer together: a small cap can end before
  any text. So only the models in ``THINKING_OFF`` get an explicit ``thinking`` setting (Haiku 5.5:
  ``{"type": "disabled"}``, valid at effort ≤ high; Sonnet 5.5 rejects "disabled" and uses
  ``{"type": "between_tools"}``) and a ``max_tokens`` of at least 1024. Any other model gets no ``thinking`` field
  (it keeps its own default) and a ``max_tokens`` of at least 8192, so that thinking leaves room for the answer.
  ``output_config.effort`` is "low" on every request, whatever the model. Only ``text`` content blocks are read;
  an answer that ends before any text (refused, cut off by ``max_tokens``, empty) is an error, streamed or not.
- The key is only ever sent in the ``x-api-key`` header. A key that is not visible ASCII (a stray newline or
  space) is never handed to the HTTP library: the library quotes a refused header value, i.e. the key, in its
  exception and in its DEBUG trace log. Every error text is scrubbed of the key, and a failure of the library to
  build the request is replaced by a fixed message with no exception chained to it.
- Streaming is Server-Sent Events; text arrives as ``content_block_delta`` events with a ``text_delta``.
"""
from __future__ import annotations

import json
import re
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
# Stop reasons of an answer that ran to its end; any other one before the first text (max_tokens used up by thinking,
# a pause, a tool call) is an error, so the router can fall back before a piece reaches the reader.
_COMPLETE = frozenset({"end_turn", "stop_sequence"})
# Rate limits, overload (529) and server errors pass: the router falls back, a job does not spend a retry.
TEMPORARY = frozenset({429, 500, 502, 503, 504, 529})
# The same conditions as a streamed error event: rate_limit_error 429, api_error 500, timeout_error 504, overloaded 529.
TEMPORARY_EVENTS = frozenset({"rate_limit_error", "api_error", "timeout_error", "overloaded_error"})
_HEADER_SAFE = re.compile(r"[\x21-\x7e]+")  # visible ASCII: what an API key is made of
_UNSUPPORTED = frozenset({"maxLength", "minLength", "minimum", "maximum", "multipleOf", "maxItems", "minItems"})
_SCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions"})  # name -> schema
_LITERALS = frozenset({"enum", "const", "default", "examples"})  # data, not schemas


def strict_schema(schema):
    """``schema`` without the limits structured output does not support, every object closed. Only schema keywords
    are removed: a property that is named like one ("minimum") and literal data (enum, default) stay."""
    if isinstance(schema, list):
        return [strict_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED:
            continue
        if key in _LITERALS:
            out[key] = value
        elif key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: strict_schema(sub) for name, sub in value.items()}
        else:
            out[key] = strict_schema(value)
    kind = out.get("type")
    if kind == "object" or (isinstance(kind, list) and "object" in kind):
        out["additionalProperties"] = False
    return out


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

    def _check_key(self) -> None:
        if not _HEADER_SAFE.fullmatch(self._key):
            raise ProviderError("anthropic: ANTHROPIC_API_KEY cannot be sent as a header (it holds a space, a line"
                                " break or another character an API key does not have); nothing was sent")

    def _scrub(self, text: str) -> str:
        """``text`` without the key (as given and trimmed): error texts reach logs, the Xử lý page and tracebacks."""
        for secret in {self._key, self._key.strip()}:
            if secret:
                text = text.replace(secret, "***")
        return text

    def _http_error(self, path: str, response: httpx.Response) -> ProviderError:
        try:
            detail = (response.json().get("error") or {}).get("message") or response.text
        except (ValueError, AttributeError):
            detail = response.text
        # Scrubbed before it is cut: a cut through an echoed key would leave a piece of it that no longer matches.
        message = f"anthropic {path} HTTP {response.status_code}: {self._scrub(str(detail))[-ERROR_TAIL:]}"
        return ProviderUnavailable(message) if response.status_code in TEMPORARY else ProviderError(message)

    def _transport_failure(self, exc: httpx.TransportError) -> ProviderError:
        """The error for a failed request. The caller raises it outside its ``except`` block, so no exception is
        chained to it: a chained one would print what the HTTP library quoted."""
        if isinstance(exc, httpx.LocalProtocolError):
            # The library refused to build the request and quotes the header value it refused, i.e. the key.
            return ProviderError("anthropic /v1/messages: the request was refused before it was sent"
                                 " (is ANTHROPIC_API_KEY a valid header value?)")
        message = self._scrub(f"anthropic /v1/messages: {type(exc).__name__}: {exc}")
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return ProviderUnavailable(message)
        return ProviderError(message)

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
        self._check_key()
        failure: ProviderError | None = None
        try:
            response = await self._client.post(
                f"{self._base}/v1/messages", headers=self._headers(), timeout=DEFAULT_TIMEOUT,
                json=self._body(model, messages, json_schema, max_tokens),
            )
        except httpx.TransportError as exc:
            failure = self._transport_failure(exc)
        if failure is not None:
            raise failure
        if response.status_code >= 400:
            raise self._http_error("/v1/messages", response)
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
        self._check_key()
        body = {**self._body(model, messages, None, max_tokens), "stream": True}
        sent = False
        stop = None
        failure: ProviderError | None = None
        try:
            async with self._client.stream(
                "POST", f"{self._base}/v1/messages", headers=self._headers(), json=body, timeout=DEFAULT_TIMEOUT
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise self._http_error("/v1/messages", response)
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
                        message = self._scrub(f"anthropic /v1/messages: {error.get('type')}: {error.get('message')}")
                        raise ProviderUnavailable(message) if error.get("type") in TEMPORARY_EVENTS else ProviderError(message)
                    if kind == "content_block_delta" and (data.get("delta") or {}).get("type") == "text_delta":
                        text = data["delta"].get("text") or ""
                        if text:
                            sent = True
                            yield text
                    elif kind == "message_delta":
                        stop = (data.get("delta") or {}).get("stop_reason") or stop
                        if stop == "refusal":
                            raise ProviderError("anthropic /v1/messages: the model declined the request")
                        if stop and stop not in _COMPLETE and not sent:
                            detail = "max_tokens was used up" if stop == "max_tokens" else f"stop_reason {stop}"
                            raise ProviderError(f"anthropic /v1/messages: {detail} before any answer text")
                    elif kind == "message_stop":
                        break
        except httpx.TransportError as exc:
            failure = self._transport_failure(exc)
        if failure is not None:
            raise failure
        if not sent:  # ended (or closed) without a word: an error, so that the router falls back
            raise ProviderError(f"anthropic /v1/messages: no text in the answer (stop_reason {stop})")

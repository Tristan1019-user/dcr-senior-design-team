"""Strict async adapter for local or remote OpenAI-compatible chat APIs."""

from __future__ import annotations

import asyncio
import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx

from .base import (
    ModelBackend,
    ModelBackendConfigurationError,
    ModelBackendError,
    ModelBackendHTTPError,
    ModelBackendInputError,
    ModelBackendLimitError,
    ModelBackendResponseError,
    ModelBackendTimeoutError,
    ModelBackendTransportError,
    ModelReply,
    ToolCall,
)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonfinite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    if isinstance(value, Mapping):
        for child in value.values():
            _reject_nonfinite(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _reject_nonfinite(child)


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    """Configuration for an explicitly selected OpenAI-compatible endpoint."""

    base_url: str
    model_name: str
    api_key: str | None = field(default=None, repr=False)
    endpoint: str = "/chat/completions"
    timeout_seconds: float = 30.0
    max_response_bytes: int = 1_048_576
    max_context_tokens: int = 32_768
    max_output_tokens: int = 1_024

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides: Any) -> "OpenAICompatibleConfig":
        """Load required endpoint/model settings from the DCR_MODEL_* environment."""

        source = os.environ if env is None else env
        base_url = source.get("DCR_MODEL_BASE_URL", "").strip()
        model_name = source.get("DCR_MODEL_NAME", "").strip()
        if not base_url or not model_name:
            raise ModelBackendConfigurationError(
                "DCR_MODEL_BASE_URL and DCR_MODEL_NAME are required"
            )
        api_key = source.get("DCR_MODEL_API_KEY") or None
        values = {"api_key": api_key}
        values.update(overrides)
        return cls(
            base_url=base_url,
            model_name=model_name,
            **values,
        )


class OpenAICompatibleBackend(ModelBackend):
    """An async, non-retrying, response-validating OpenAI-compatible backend.

    The backend only proposes typed tool calls. It never resolves or executes a
    tool, writes to DCR transport, or treats prompting as a safety boundary.
    """

    _DEFAULT_LIMITS: ClassVar[httpx.Limits] = httpx.Limits(
        max_connections=8,
        max_keepalive_connections=4,
    )

    def __init__(
        self,
        base_url: str | None = None,
        model_name: str | None = None,
        api_key: str | None = None,
        *,
        model: str | None = None,
        endpoint: str = "/chat/completions",
        timeout_seconds: float = 30.0,
        max_response_bytes: int = 1_048_576,
        max_context_tokens: int = 32_768,
        max_output_tokens: int = 1_024,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if model_name is not None and model is not None and model_name != model:
            raise ModelBackendConfigurationError("conflicting model names supplied")
        model_name = model_name or model
        if not base_url or not model_name:
            raise ModelBackendConfigurationError(
                "base_url and model_name are required; use from_env() for DCR_MODEL_* settings"
            )
        if client is not None and transport is not None:
            raise ModelBackendConfigurationError("client and transport cannot both be supplied")
        if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
            raise ModelBackendConfigurationError("timeout_seconds must be finite and positive")
        if max_response_bytes <= 0:
            raise ModelBackendConfigurationError("max_response_bytes must be positive")
        if max_context_tokens <= 0 or max_output_tokens <= 0:
            raise ModelBackendConfigurationError("token bounds must be positive")
        if max_output_tokens > max_context_tokens:
            raise ModelBackendConfigurationError("max_output_tokens exceeds max_context_tokens")

        self._base_url = self._validate_base_url(base_url)
        self._model_name = self._validate_text(model_name, "model_name")
        self._endpoint = self._validate_endpoint(endpoint)
        self._api_key = api_key
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._max_context_tokens = max_context_tokens
        self._max_output_tokens = max_output_tokens
        self._generation_supported: bool | None = None
        self._tool_calling_supported: bool | None = None
        self._owns_client = client is None
        self._closed = False

        if client is not None:
            self._client = client
        else:
            self._client = httpx.AsyncClient(
                limits=self._DEFAULT_LIMITS,
                timeout=self._timeout,
                transport=transport,
                trust_env=False,
            )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> "OpenAICompatibleBackend":
        """Construct a backend from DCR_MODEL_BASE_URL/NAME/API_KEY."""

        backend_kwargs = {
            name: kwargs.pop(name)
            for name in ("client", "transport")
            if name in kwargs
        }
        config = OpenAICompatibleConfig.from_env(env, **kwargs)
        return cls(
            base_url=config.base_url,
            model_name=config.model_name,
            api_key=config.api_key,
            endpoint=config.endpoint,
            timeout_seconds=config.timeout_seconds,
            max_response_bytes=config.max_response_bytes,
            max_context_tokens=config.max_context_tokens,
            max_output_tokens=config.max_output_tokens,
            **backend_kwargs,
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self._base_url!r}, "
            f"model_name={self._model_name!r}, api_key=<redacted>)"
        )

    async def __aenter__(self) -> "OpenAICompatibleBackend":
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close an internally owned HTTP client."""

        if not self._closed:
            self._closed = True
            if self._owns_client:
                await self._client.aclose()

    async def health(self) -> bool:
        """Check that the selected model is discovered by the remote service."""

        if self._closed:
            return False
        try:
            model_ids = await self._discover_models()
        except ModelBackendError:
            return False
        return self._model_name in model_ids

    async def model_info(self) -> dict[str, Any]:
        """Discover model availability without inferring generation capabilities."""

        model_ids = await self._discover_models()
        return {
            "backend": "openai-compatible",
            "model": self._model_name,
            "endpoint_reachable": True,
            "model_discovered": self._model_name in model_ids,
            "discovered_models": model_ids,
            "generation_supported": self._generation_supported,
            "tool_calling_supported": self._tool_calling_supported,
            "max_context_tokens": self._max_context_tokens,
            "max_output_tokens": self._max_output_tokens,
            "max_response_bytes": self._max_response_bytes,
            "context_token_estimate": "UTF-8 request bytes / 4; approximate, not tokenizer-exact",
        }

    async def generate(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> ModelReply:
        """Send one bounded request and return strictly normalized model output."""

        if self._closed:
            raise ModelBackendTransportError("model backend is closed")
        request_body = self._encode_request(messages, tools)
        url = f"{self._base_url}{self._endpoint}"
        response_body = await self._request_bytes(
            "POST",
            url,
            content=request_body,
        )
        reply = self._parse_response(response_body)
        self._generation_supported = True
        if tools is not None and reply.tool_calls:
            self._tool_calling_supported = True
        return reply

    async def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        content: bytes | None = None,
    ) -> bytes:
        headers = {"Accept": "application/json"}
        if content is not None:
            headers["Content-Type"] = "application/json"
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            async with self._client.stream(
                method,
                url,
                headers=headers,
                content=content,
                timeout=self._timeout,
            ) as response:
                status_code = response.status_code
                if not 200 <= status_code < 300:
                    raise ModelBackendHTTPError(status_code)
                return await self._read_bounded(response)
        except ModelBackendError:
            raise
        except asyncio.CancelledError:
            raise
        except (httpx.TimeoutException, asyncio.TimeoutError):
            raise ModelBackendTimeoutError("model backend request timed out") from None
        except httpx.HTTPError:
            raise ModelBackendTransportError("model backend transport failed") from None
        except Exception:
            raise ModelBackendTransportError("model backend transport failed") from None

    async def _discover_models(self) -> list[str]:
        body = await self._request_bytes("GET", f"{self._base_url}/models")
        return self._parse_model_ids(body)

    async def _read_bounded(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self._max_response_bytes:
                    raise ModelBackendLimitError("model backend response exceeds configured limit")
                chunks.append(chunk)
        except ModelBackendError:
            raise
        except asyncio.CancelledError:
            raise
        except (httpx.TimeoutException, asyncio.TimeoutError):
            raise ModelBackendTimeoutError("model backend response timed out") from None
        except Exception:
            raise ModelBackendTransportError("model backend response read failed") from None
        return b"".join(chunks)

    def _encode_request(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
    ) -> bytes:
        if isinstance(messages, (str, bytes, bytearray)):
            raise ModelBackendInputError("messages must be a sequence of objects")
        try:
            message_list = list(messages)
        except (TypeError, ValueError):
            raise ModelBackendInputError("messages must be a sequence of objects") from None
        if not message_list:
            raise ModelBackendInputError("messages must not be empty")
        if any(not isinstance(message, Mapping) for message in message_list):
            raise ModelBackendInputError("messages must contain only objects")

        tool_list: list[Mapping[str, Any]] | None = None
        if tools is not None:
            if isinstance(tools, (str, bytes, bytearray)):
                raise ModelBackendInputError("tools must be a sequence of objects")
            try:
                tool_list = list(tools)
            except (TypeError, ValueError):
                raise ModelBackendInputError("tools must be a sequence of objects") from None
            if any(not isinstance(tool, Mapping) for tool in tool_list):
                raise ModelBackendInputError("tools must contain only objects")

        payload: dict[str, Any] = {
            "model": self._model_name,
            "messages": message_list,
            "max_tokens": self._max_output_tokens,
            "parallel_tool_calls": False,
            "temperature": 0,
        }
        if tool_list is not None:
            payload["tools"] = tool_list
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (RecursionError, TypeError, ValueError, UnicodeEncodeError):
            raise ModelBackendInputError("messages or tools are not JSON-serializable") from None

        # This is intentionally a conservative byte-based estimate, not an exact tokenizer count.
        estimated_input_tokens = max(1, math.ceil(len(encoded) / 4))
        if estimated_input_tokens + self._max_output_tokens > self._max_context_tokens:
            raise ModelBackendLimitError("estimated request context exceeds configured token bound")
        return encoded

    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ModelBackendConfigurationError("base_url is required")
        try:
            parsed = httpx.URL(value.strip())
        except Exception:
            raise ModelBackendConfigurationError("base_url is invalid") from None
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise ModelBackendConfigurationError("base_url must be an HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ModelBackendConfigurationError("base_url must not contain credentials or query data")
        return str(parsed).rstrip("/")

    @staticmethod
    def _validate_text(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ModelBackendConfigurationError(f"{field_name} is required")
        return value.strip()

    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        if not isinstance(value, str) or not value.startswith("/"):
            raise ModelBackendConfigurationError("endpoint must be an absolute path")
        try:
            parsed = httpx.URL(value)
        except Exception:
            raise ModelBackendConfigurationError("endpoint is invalid") from None
        if parsed.query or parsed.fragment:
            raise ModelBackendConfigurationError("endpoint must not contain query data")
        return value.rstrip("/") or "/"

    @staticmethod
    def _decode_json(body: bytes) -> Any:
        try:
            decoded = body.decode("utf-8")
            raw = json.loads(
                decoded,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
            _reject_nonfinite(raw)
            return raw
        except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError, TypeError):
            raise ModelBackendResponseError("model backend returned invalid JSON") from None

    @staticmethod
    def _parse_model_ids(body: bytes) -> list[str]:
        raw = OpenAICompatibleBackend._decode_json(body)
        try:
            if not isinstance(raw, dict) or not isinstance(raw.get("data"), list):
                raise ValueError
            model_ids: list[str] = []
            for item in raw["data"]:
                if not isinstance(item, dict):
                    raise ValueError
                model_id = item.get("id")
                if not isinstance(model_id, str) or not model_id.strip():
                    raise ValueError
                model_ids.append(model_id)
            return model_ids
        except Exception:
            raise ModelBackendResponseError("model backend returned malformed model list") from None

    @staticmethod
    def _parse_response(body: bytes) -> ModelReply:
        raw = OpenAICompatibleBackend._decode_json(body)

        try:
            if not isinstance(raw, dict):
                raise ValueError
            choices = raw.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise ValueError
            finish_reason = choice.get("finish_reason")
            if finish_reason in {"length", "content_filter"}:
                raise ValueError
            if finish_reason is not None and finish_reason not in {
                "stop",
                "tool_calls",
                "function_call",
            }:
                raise ValueError
            message = choice["message"]

            raw_tool_calls = message.get("tool_calls", [])
            if not isinstance(raw_tool_calls, list):
                raise ValueError
            tool_calls: list[ToolCall] = []
            for raw_call in raw_tool_calls:
                if not isinstance(raw_call, dict):
                    raise ValueError
                if raw_call.get("type") != "function":
                    raise ValueError
                call_id = raw_call.get("id")
                function = raw_call.get("function")
                if not isinstance(call_id, str) or not call_id.strip():
                    raise ValueError
                if not isinstance(function, dict):
                    raise ValueError
                name = function.get("name")
                arguments_text = function.get("arguments")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError
                if not isinstance(arguments_text, str):
                    raise ValueError
                arguments = json.loads(
                    arguments_text,
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_constant,
                )
                _reject_nonfinite(arguments)
                if not isinstance(arguments, dict):
                    raise ValueError
                tool_calls.append(
                    ToolCall(id=call_id, name=name, arguments=arguments)
                )

            has_content = "content" in message
            content = message.get("content")
            if content is None:
                if not tool_calls:
                    raise ValueError
                text = ""
            elif isinstance(content, str):
                text = content
            else:
                raise ValueError
            if not has_content and not tool_calls:
                raise ValueError

            usage = raw.get("usage")
            if usage is not None and not isinstance(usage, dict):
                raise ValueError
            return ModelReply(text=text, tool_calls=tool_calls, usage=usage)
        except ModelBackendResponseError:
            raise
        except Exception:
            raise ModelBackendResponseError("model backend returned malformed response") from None

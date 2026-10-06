"""Contract tests for the replaceable model backend."""

from __future__ import annotations

import json

import httpx
import pytest

from dcr_host.models import (
    ModelBackendConfigurationError,
    ModelBackendHTTPError,
    ModelBackendLimitError,
    ModelBackendResponseError,
    ModelBackendTimeoutError,
    OpenAICompatibleBackend,
    OpenAICompatibleConfig,
)


async def _generate(handler, *, messages=None, tools=None, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = OpenAICompatibleBackend(
        "https://model.example.test/v1",
        "test-model",
        client=client,
        **kwargs,
    )
    try:
        return await backend.generate(
            messages or [{"role": "user", "content": "status"}],
            tools,
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_valid_response_without_tools_uses_bounded_chat_request():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        assert "authorization" not in request.headers
        payload = json.loads(request.content)
        assert payload == {
            "model": "test-model",
            "messages": [{"role": "user", "content": "status"}],
            "max_tokens": 1024,
            "parallel_tool_calls": False,
            "temperature": 0,
        }
        return httpx.Response(
            200,
            json={
                "id": "chat-1",
                "choices": [{"message": {"role": "assistant", "content": "ready"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
            },
        )

    reply = await _generate(handler)

    assert reply.text == "ready"
    assert reply.tool_calls == []
    assert reply.usage == {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}


@pytest.mark.asyncio
async def test_valid_function_call_is_returned_without_execution():
    executed = False

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["tools"] == [{"type": "function", "function": {"name": "status"}}]
        assert payload["parallel_tool_calls"] is False
        assert payload["temperature"] == 0
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "status",
                                        "arguments": '{"component":"sensor_a"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    reply = await _generate(
        handler,
        tools=[{"type": "function", "function": {"name": "status"}}],
    )

    assert executed is False
    assert reply.text == ""
    assert reply.tool_calls[0].id == "call-1"
    assert reply.tool_calls[0].name == "status"
    assert reply.tool_calls[0].arguments == {"component": "sensor_a"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        '{"component":1,"component":2}',
        '{"value":NaN}',
        '["not", "an", "object"]',
        "not-json",
    ],
)
async def test_malformed_function_arguments_are_rejected(arguments):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "status", "arguments": arguments},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    with pytest.raises(ModelBackendResponseError, match="malformed response"):
        await _generate(handler)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call_patch",
    [{"id": "", "type": "function"}, {"type": "function"}],
)
async def test_missing_tool_id_is_rejected(call_patch):
    async def handler(request: httpx.Request) -> httpx.Response:
        call = {
            **call_patch,
            "function": {"name": "status", "arguments": "{}"},
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": None, "tool_calls": [call]}}]},
        )

    with pytest.raises(ModelBackendResponseError):
        await _generate(handler)


@pytest.mark.asyncio
async def test_missing_tool_name_is_rejected():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    with pytest.raises(ModelBackendResponseError):
        await _generate(handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
async def test_non_complete_finish_reasons_are_rejected(finish_reason):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {"content": "partial"},
                    }
                ]
            },
        )

    with pytest.raises(ModelBackendResponseError):
        await _generate(handler)


@pytest.mark.asyncio
async def test_timeout_is_sanitized():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret transport detail", request=request)

    with pytest.raises(ModelBackendTimeoutError) as exc_info:
        await _generate(handler, timeout_seconds=0.1)
    assert "secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_http_failure_does_not_expose_body_or_api_key():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer super-secret"
        return httpx.Response(503, content=b"upstream key=super-secret private body")

    with pytest.raises(ModelBackendHTTPError) as exc_info:
        await _generate(handler, api_key="super-secret")
    assert exc_info.value.status_code == 503
    assert "super-secret" not in str(exc_info.value)
    assert "private body" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_m18_tool_parser_rejection_is_offline_and_not_retried():
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        payload = json.loads(request.content)
        assert payload["tools"]
        return httpx.Response(
            400,
            content=b'{"error":{"message":"auto tool parser disabled"}}',
        )

    with pytest.raises(ModelBackendHTTPError) as exc_info:
        await _generate(
            handler,
            tools=[{"type": "function", "function": {"name": "status"}}],
        )

    assert exc_info.value.status_code == 400
    assert requests == 1
    assert "auto tool parser disabled" not in str(exc_info.value)


def test_environment_configuration_requires_explicit_endpoint_and_model():
    with pytest.raises(ModelBackendConfigurationError):
        OpenAICompatibleBackend.from_env(env={})


def test_secret_configuration_is_redacted_from_representations():
    config = OpenAICompatibleConfig(
        base_url="https://model.example.test/v1",
        model_name="test-model",
        api_key="super-secret",
    )
    assert "super-secret" not in repr(config)


@pytest.mark.asyncio
async def test_context_bound_is_configurable():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("request must be rejected before transport")

    with pytest.raises(ModelBackendLimitError):
        await _generate(handler, max_context_tokens=5, max_output_tokens=4)


@pytest.mark.asyncio
async def test_model_health_and_info_discover_models_without_claiming_generation_support():
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method + " " + request.url.path)
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "test-model", "object": "model"}]},
        )

    transport = httpx.MockTransport(handler)
    backend = OpenAICompatibleBackend(
        "https://model.example.test/v1",
        "test-model",
        transport=transport,
    )
    try:
        assert await backend.health() is True
        info = await backend.model_info()
    finally:
        await backend.aclose()

    assert calls == ["GET /v1/models", "GET /v1/models"]
    assert info["endpoint_reachable"] is True
    assert info["model_discovered"] is True
    assert info["generation_supported"] is None
    assert info["tool_calling_supported"] is None


@pytest.mark.asyncio
async def test_recursion_errors_are_sanitized():
    async def handler(request: httpx.Request) -> httpx.Response:
        deep_json = "[" * 1200 + "0" + "]" * 1200
        body = (
            '{"choices":[{"message":{"content":"ok"}}],"extra":'
            + deep_json
            + "}"
        ).encode()
        return httpx.Response(200, content=body)

    with pytest.raises(ModelBackendResponseError) as exc_info:
        await _generate(handler)
    assert "recursion" not in str(exc_info.value).lower()


def test_owned_http_client_ignores_inherited_environment():
    backend = OpenAICompatibleBackend(
        "https://model.example.test/v1",
        "test-model",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"choices": []})
        ),
    )
    try:
        assert backend._client._trust_env is False
    finally:
        import asyncio

        asyncio.run(backend.aclose())

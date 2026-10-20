"""The local model proxy: the only path from the official Loop to a provider.

The proxy is a loopback-only, token-bound protocol forwarder. It owns no Agent
Loop and no provider credential in the runtime; every call still occupies the
persistent ModelGateway slot and settles verified usage. These tests replace the
supplier with a fixed provider so they stay offline and cost nothing.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import httpx
import pytest

from personal_agent.adapters.harness.model_gateway import ModelGateway, ProviderOutcome
from personal_agent.adapters.harness.model_provider import AnthropicMessagesProvider
from personal_agent.adapters.harness.model_proxy import ModelProxy, ProxyTokenRegistry
from personal_agent.domain.common import AppError, ErrorCategory, new_id
from personal_agent.ports.contracts import ModelRequest, Owner, Usage

ANTHROPIC_MESSAGE = {
    "id": "msg_fixture",
    "type": "message",
    "role": "assistant",
    "model": "deepseek-flash",
    "content": [
        {"type": "text", "text": "fixed answer"},
        {"type": "tool_use", "id": "toolu_1", "name": "mcp__personal_agent__search.web",
         "input": {"query": "fixed"}},
    ],
    "stop_reason": "tool_use",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 11,
        "output_tokens": 5,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 0,
    },
}


class UnusedTransport:
    """The gateway slot is exercised, but never the Python-side transport."""

    async def complete(self, spec: ModelRequest) -> Any:  # pragma: no cover - must not run
        raise AssertionError("the proxy must not use the Python-side transport")


class FixedProvider:
    def __init__(self, outcome: ProviderOutcome | None = None) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.specs: list[ModelRequest] = []
        self.outcome = outcome or ProviderOutcome(
            status="completed",
            payload=ANTHROPIC_MESSAGE,
            usage=Usage(input_tokens=18, output_tokens=5, model_requests=1),
            completion_evidence_ref="provider:fixture",
        )
        self.release = asyncio.Event()
        self.release.set()

    async def complete(self, body: dict[str, Any], spec: ModelRequest) -> ProviderOutcome:
        self.bodies.append(json.loads(json.dumps(body)))
        self.specs.append(spec)
        await self.release.wait()
        return self.outcome


def binding(*, registry: ProxyTokenRegistry, **overrides: Any) -> str:
    values = {
        "owner": Owner(run_id=new_id(), attempt_id=new_id()),
        "epoch": 1,
        "model_id": "deepseek-flash",
        "reservation_id": "attempt:model",
        "reserved_tokens": 128,
        "output_limit": 32,
        "timeout_seconds": 10.0,
    }
    values.update(overrides)
    return registry.issue(**values).token


@pytest.fixture
async def proxy(database: Any, clock: Any) -> Any:
    registry = ProxyTokenRegistry()
    provider = FixedProvider()
    gateway = ModelGateway(database, clock, UnusedTransport())
    server = ModelProxy(gateway, registry, provider, clock)
    await server.start()
    try:
        yield server, registry, provider, gateway
    finally:
        await server.stop()


def body(**overrides: Any) -> dict[str, Any]:
    value = {
        "model": "deepseek-flash",
        "max_tokens": 32,
        "stream": True,
        "system": "fixed system prompt",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "fixed"}]}],
    }
    value.update(overrides)
    return value


async def post(server: ModelProxy, payload: dict[str, Any], token: str | None) -> Any:
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["x-api-key"] = token
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        return await client.post(
            server.url("/anthropic/v1/messages"), json=payload, headers=headers
        )


def sse_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        data = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if data:
            events.append(json.loads("".join(data)))
    return events


async def test_unknown_or_revoked_token_cannot_start_a_request(
    database: Any, clock: Any, proxy: Any,
) -> None:
    server, registry, provider, gateway = proxy
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        missing = await client.post(server.url("/anthropic/v1/messages"), json=body())
        assert missing.status_code == 401
        token = binding(registry=registry)
        registry.revoke(token)
        revoked = await client.post(
            server.url("/anthropic/v1/messages"), json=body(), headers={"x-api-key": token}
        )
    assert revoked.status_code == 401
    assert provider.bodies == [] and provider.specs == []
    assert await gateway.non_terminal_request() is None


async def test_one_cli_call_becomes_one_settled_gateway_request(proxy: Any) -> None:
    server, registry, provider, gateway = proxy
    token = binding(registry=registry)
    response = await post(server, body(), token)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = sse_events(response.text)
    kinds = [event["type"] for event in events]
    assert kinds[0] == "message_start" and kinds[-1] == "message_stop"
    assert kinds.count("content_block_start") == 2
    assert kinds.count("content_block_stop") == 2
    text_delta = next(event for event in events if event["type"] == "content_block_delta"
                      and event["delta"]["type"] == "text_delta")
    assert text_delta["delta"]["text"] == "fixed answer"
    tool_delta = next(event for event in events if event["type"] == "content_block_delta"
                      and event["delta"]["type"] == "input_json_delta")
    assert json.loads(tool_delta["delta"]["partial_json"]) == {"query": "fixed"}
    delta = next(event for event in events if event["type"] == "message_delta")
    assert delta["delta"]["stop_reason"] == "tool_use"
    assert delta["usage"]["output_tokens"] == 5

    # The supplier-visible body never carries the gateway token or the API key.
    forwarded = provider.bodies[0]
    assert forwarded["max_tokens"] == 32 and forwarded["model"] == "deepseek-flash"
    assert "x-api-key" not in json.dumps(forwarded)
    assert provider.specs[0].owner.attempt_id is not None

    assert await gateway.non_terminal_request() is None
    row = await gateway.describe(provider.specs[0].request_id)
    assert row["status"] == "COMPLETED" and row["actual_tokens"] == 23


async def test_provider_failure_keeps_the_slot_unknown(proxy: Any) -> None:
    server, registry, provider, gateway = proxy
    provider.outcome = ProviderOutcome(
        status="unknown",
        error=AppError(
            code="MODEL_TIMEOUT", message="the provider did not answer",
            category=ErrorCategory.TIMEOUT, status=504,
        ),
    )
    token = binding(registry=registry)
    response = await post(server, body(), token)
    assert response.status_code >= 500
    assert "MODEL_TIMEOUT" not in response.text
    row = await gateway.non_terminal_request()
    assert row is not None and row.status == "UNKNOWN"
    assert row.actual_tokens is None


async def test_only_one_request_holds_the_persistent_slot(proxy: Any) -> None:
    server, registry, provider, gateway = proxy
    token = binding(registry=registry)
    provider.release.clear()
    first = asyncio.create_task(post(server, body(), token))
    for _ in range(200):
        if provider.specs:
            break
        await asyncio.sleep(0.01)
    second = await post(server, body(), token)
    assert second.status_code == 409
    provider.release.set()
    first_response = await first
    assert first_response.status_code == 200
    assert await gateway.non_terminal_request() is None


async def test_proxy_rejects_a_model_outside_its_binding(proxy: Any) -> None:
    server, registry, _, gateway = proxy
    token = binding(registry=registry, model_id="deepseek-flash")
    response = await post(server, body(model="other-model"), token)
    assert response.status_code == 403
    assert await gateway.non_terminal_request() is None


async def test_provider_maps_usage_and_never_echoes_failure_detail(clock: Any) -> None:
    spec = ModelRequest(
        new_id(), Owner(run_id=new_id(), attempt_id=new_id()), 1, "deepseek-flash",
        [], 32, clock.now() + timedelta(seconds=5), "reservation",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/anthropic/v1/messages"
        assert request.headers["x-api-key"] == "gateway-token"
        sent = json.loads(request.content)
        assert sent["stream"] is False, "the proxy buffers one bounded supplier call"
        return httpx.Response(200, json=ANTHROPIC_MESSAGE)

    provider = AnthropicMessagesProvider(
        "http://127.0.0.1:1/anthropic", "gateway-token",
        transport=httpx.MockTransport(handler),
    )
    outcome = await provider.complete(body(), spec)
    assert outcome.status == "completed" and outcome.payload is not None
    assert outcome.usage.input_tokens == 18 and outcome.usage.output_tokens == 5
    assert outcome.completion_evidence_ref

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "PROVIDER_SECRET_DETAIL"}})

    broken = AnthropicMessagesProvider(
        "http://127.0.0.1:1/anthropic", "gateway-token",
        transport=httpx.MockTransport(failing),
    )
    failed = await broken.complete(body(), spec)
    assert failed.status == "unknown"
    assert failed.error is not None and "PROVIDER_SECRET_DETAIL" not in failed.error.message

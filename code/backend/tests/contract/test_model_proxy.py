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
from personal_agent.ports.contracts import (
    ModelCallGrant,
    ModelRequest,
    Owner,
    ToolCallGrant,
    ToolRequest,
    ToolResult,
    Usage,
)

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


async def test_a_gateway_refusal_releases_only_the_refused_call(
    database: Any, clock: Any,
) -> None:
    class Meter:
        def __init__(self) -> None:
            self.begins = 0
            self.settles = 0
            self.releases: list[ModelCallGrant] = []

        async def begin(self, **kwargs: Any) -> ModelCallGrant:
            self.begins += 1
            return ModelCallGrant(f"attempt:model:{self.begins}", 128)

        async def settle(self, **kwargs: Any) -> None:
            self.settles += 1

        async def release(self, **kwargs: Any) -> None:
            self.releases.append(kwargs["grant"])

    provider = FixedProvider()
    provider.release.clear()
    meter = Meter()
    tokens = ProxyTokenRegistry()
    gateway = ModelGateway(database, clock, UnusedTransport())
    server = ModelProxy(gateway, tokens, provider, clock, meter=meter)
    await server.start()
    try:
        token = binding(registry=tokens, root_run_id="root-run")
        first = asyncio.create_task(post(server, body(), token))
        for _ in range(200):
            if provider.specs:
                break
            await asyncio.sleep(0.01)
        second = await post(server, body(), token)
        assert second.status_code == 409
        assert len(meter.releases) == 1
        provider.release.set()
        assert (await first).status_code == 200
        assert meter.settles == 1
    finally:
        await server.stop()


async def test_proxy_rejects_a_model_outside_its_binding(proxy: Any) -> None:
    server, registry, _, gateway = proxy
    token = binding(registry=registry, model_id="deepseek-flash")
    response = await post(server, body(model="other-model"), token)
    assert response.status_code == 403
    assert await gateway.non_terminal_request() is None


async def test_tool_calls_use_the_bound_scope_and_return_the_body(
    database: Any, clock: Any,
) -> None:
    class RecordingTools:
        def __init__(self) -> None:
            self.requests: list[ToolRequest] = []
            self.refuse = False

        async def execute(self, request: ToolRequest) -> ToolResult:
            self.requests.append(request)
            if self.refuse:
                raise AppError(
                    code="TOOL_SCOPE_DENIED", message="denied",
                    category=ErrorCategory.PERMISSION, status=403,
                )
            return ToolResult(
                status="ok", content={"body": "page body"},
                source_refs=["https://example.org"], usage=Usage(tool_calls=1),
            )

    registry = ProxyTokenRegistry()
    tools = RecordingTools()
    gateway = ModelGateway(database, clock, UnusedTransport())
    server = ModelProxy(gateway, registry, FixedProvider(), clock, tools=tools)
    await server.start()
    try:
        token = binding(registry=registry, tool_scopes={"http.fetch": "scope-token"})
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            allowed = await client.post(
                server.url("/tools/call"),
                json={"name": "http.fetch", "arguments": {"url": "https://example.org"},
                      "scope_token": "forged", "epoch": 99},
                headers={"x-api-key": token},
            )
            assert allowed.status_code == 200, allowed.text
            payload = allowed.json()
            assert payload["isError"] is False
            assert json.loads(payload["content"][0]["text"]) == {"body": "page body"}
            assert tools.requests[0].scope_token == "scope-token"
            assert tools.requests[0].tool_name == "http.fetch"
            assert tools.requests[0].epoch == 1

            not_bound = await client.post(
                server.url("/tools/call"), json={"name": "shell.exec", "arguments": {}},
                headers={"x-api-key": token},
            )
            assert not_bound.status_code == 403

            tools.refuse = True
            refused = await client.post(
                server.url("/tools/call"),
                json={"name": "http.fetch", "arguments": {"url": "https://example.org"}},
                headers={"x-api-key": token},
            )
            assert refused.status_code == 200
            assert refused.json()["isError"] is True
            assert "TOOL_SCOPE_DENIED" in refused.json()["content"][0]["text"]

            missing = await client.post(
                server.url("/tools/call"), json={"name": "http.fetch", "arguments": {}},
            )
            assert missing.status_code == 401
    finally:
        await server.stop()


async def test_each_loop_call_reserves_and_settles_on_its_own_boundary(
    database: Any, clock: Any,
) -> None:
    class FakeMeter:
        def __init__(self) -> None:
            self.begins: list[tuple[str, str, int]] = []
            self.settles: list[tuple[str, Usage]] = []
            self.releases: list[ModelCallGrant] = []

        async def begin(
            self, *, root_run_id: str, attempt_id: str, output_limit: int,
            estimated_input: int,
        ) -> ModelCallGrant:
            self.begins.append((root_run_id, attempt_id, estimated_input))
            grant = ModelCallGrant(
                f"{attempt_id}:model:{len(self.begins)}", output_limit + estimated_input,
            )
            return grant

        async def settle(
            self, *, root_run_id: str, grant: ModelCallGrant, usage: Usage,
        ) -> None:
            self.settles.append((grant.reservation_id, usage))

        async def release(self, *, root_run_id: str, grant: ModelCallGrant) -> None:
            self.releases.append(grant)

    meter = FakeMeter()
    registry = ProxyTokenRegistry()
    gateway = ModelGateway(database, clock, UnusedTransport())
    server = ModelProxy(gateway, registry, FixedProvider(), clock, meter=meter)
    await server.start()
    try:
        token = binding(registry=registry, root_run_id="root-run")
        response = await post(server, body(), token)
        assert response.status_code == 200
        assert meter.begins and meter.begins[0][0] == "root-run"
        assert meter.begins[0][2] > 0
        assert len(meter.settles) == 1
        assert meter.settles[0][0].endswith(":model:1")
        assert meter.settles[0][1].tokens == 23
        assert meter.releases == []
    finally:
        await server.stop()


async def test_cli_loop_meter_keeps_an_unknown_reservation(database: Any, clock: Any) -> None:
    class FakeMeter:
        def __init__(self) -> None:
            self.settles = 0
            self.releases = 0

        async def begin(self, **kwargs: Any) -> ModelCallGrant:
            return ModelCallGrant("attempt:model:1", 128)

        async def settle(self, **kwargs: Any) -> None:
            self.settles += 1

        async def release(self, **kwargs: Any) -> None:
            self.releases += 1

    meter = FakeMeter()
    registry = ProxyTokenRegistry()
    provider = FixedProvider(outcome=ProviderOutcome(
        status="unknown",
        error=AppError(
            code="MODEL_TIMEOUT", message="timeout",
            category=ErrorCategory.TIMEOUT, status=504,
        ),
    ))
    gateway = ModelGateway(database, clock, UnusedTransport())
    server = ModelProxy(gateway, registry, provider, clock, meter=meter)
    await server.start()
    try:
        token = binding(registry=registry, root_run_id="root-run")
        response = await post(server, body(), token)
        assert response.status_code >= 500
        assert meter.settles == 0 and meter.releases == 0
    finally:
        await server.stop()


async def test_tool_calls_are_metered_once_and_release_on_refusal(
    database: Any, clock: Any,
) -> None:
    class FakeToolMeter:
        def __init__(self) -> None:
            self.begins: list[tuple[str, str]] = []
            self.settles = 0
            self.releases = 0

        async def begin(
            self, *, root_run_id: str, attempt_id: str, tool_name: str,
        ) -> ToolCallGrant:
            self.begins.append((root_run_id, tool_name))
            return ToolCallGrant(f"a:tool:{len(self.begins)}", {"tool_calls": 1})

        async def settle(self, **kwargs: Any) -> None:
            self.settles += 1

        async def release(self, **kwargs: Any) -> None:
            self.releases += 1

    class Tools:
        def __init__(self) -> None:
            self.refuse = False

        async def execute(self, request: ToolRequest) -> ToolResult:
            if self.refuse:
                raise AppError(
                    code="TOOL_SCOPE_DENIED", message="denied",
                    category=ErrorCategory.PERMISSION, status=403,
                )
            return ToolResult(status="ok", content={"ok": True}, usage=Usage(tool_calls=1))

    meter = FakeToolMeter()
    tools = Tools()
    registry = ProxyTokenRegistry()
    gateway = ModelGateway(database, clock, UnusedTransport())
    server = ModelProxy(
        gateway, registry, FixedProvider(), clock, tools=tools, tool_meter=meter,
    )
    await server.start()
    try:
        token = binding(
            registry=registry, root_run_id="root-run",
            tool_scopes={"http.fetch": "scope-token"},
        )
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            allowed = await client.post(
                server.url("/tools/call"),
                json={"name": "http.fetch", "arguments": {"url": "https://example.org"}},
                headers={"x-api-key": token},
            )
            assert allowed.status_code == 200
            assert meter.begins == [("root-run", "http.fetch")]
            assert meter.settles == 1

            tools.refuse = True
            refused = await client.post(
                server.url("/tools/call"),
                json={"name": "http.fetch", "arguments": {"url": "https://example.org"}},
                headers={"x-api-key": token},
            )
            assert refused.status_code == 200 and refused.json()["isError"] is True
            assert meter.settles == 1 and meter.releases == 1
    finally:
        await server.stop()


async def test_a_stopping_run_cannot_start_a_tool_call(
    database: Any, clock: Any,
) -> None:
    class RecordingTools:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, request: ToolRequest) -> ToolResult:
            self.calls += 1
            return ToolResult(status="ok", usage=Usage(tool_calls=1))

    async def stopping(run_id: str) -> bool:
        assert run_id == "root-run"
        return True

    tools = RecordingTools()
    registry = ProxyTokenRegistry()
    gateway = ModelGateway(database, clock, UnusedTransport())
    server = ModelProxy(
        gateway, registry, FixedProvider(), clock, tools=tools, stop_check=stopping,
    )
    await server.start()
    try:
        token = binding(
            registry=registry, root_run_id="root-run",
            tool_scopes={"http.fetch": "scope-token"},
        )
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            refused = await client.post(
                server.url("/tools/call"),
                json={"name": "http.fetch", "arguments": {"url": "https://example.org"}},
                headers={"x-api-key": token},
            )
        assert refused.status_code == 200
        payload = refused.json()
        assert payload["isError"] is True
        assert json.loads(payload["content"][0]["text"])["code"] == "INTERRUPTED"
        assert tools.calls == 0
    finally:
        await server.stop()


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

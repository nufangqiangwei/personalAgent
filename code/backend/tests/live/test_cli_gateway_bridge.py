"""Real pinned CLI through the loopback model gateway, with no supplier request.

The provider is a fixed in-process reply, so this test proves the wiring rather
than a model result: the official Loop's Messages calls must traverse the
loopback proxy and occupy/settle the persistent gateway slot. It runs only with
the explicit ``--live`` switch because it starts the real installed CLI.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from personal_agent.adapters.harness.adoption import configured_installation
from personal_agent.adapters.harness.cli import HeadlessCli
from personal_agent.adapters.harness.model_gateway import ModelGateway, ProviderOutcome
from personal_agent.adapters.harness.model_proxy import ModelProxy, ProxyTokenRegistry
from personal_agent.adapters.harness.patch import write_cli_patch
from personal_agent.adapters.tools.mcp_service import ToolHandlers, ToolService
from personal_agent.adapters.tools.scope import ToolScopeRegistry
from personal_agent.ports.contracts import ModelRequest, Owner, Usage

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[4]
ANSWER = {
    "id": "msg_bridge_fixture",
    "type": "message",
    "role": "assistant",
    "model": "deepseek-flash",
    "content": [{"type": "text", "text": "fixed bridge answer"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 9, "output_tokens": 4},
}


class FixedProvider:
    def __init__(self) -> None:
        self.calls: list[ModelRequest] = []

    async def complete(self, body: dict[str, Any], spec: ModelRequest) -> ProviderOutcome:
        self.calls.append(spec)
        return ProviderOutcome(
            status="completed", payload=ANSWER,
            usage=Usage(input_tokens=9, output_tokens=4, model_requests=1),
            completion_evidence_ref=f"fixed:{spec.request_id}",
        )


class UnusedTransport:
    async def complete(self, spec: ModelRequest) -> Any:  # pragma: no cover - must not run
        raise AssertionError("the bridge must not use the Python-side transport")


async def test_the_official_loop_reaches_models_only_through_the_gateway(
    tmp_path: Path, database: Any, clock: Any,
) -> None:
    installation = configured_installation()
    if installation is None:
        pytest.skip("set PERSONAL_AGENT_DSH_CLI_PATH to the installed pinned CLI")
    plugin = ROOT / "code/runtime-policy/dist/index.js"
    if not plugin.exists():
        pytest.skip("build code/runtime-policy before running the bridge check")

    provider = FixedProvider()
    gateway = ModelGateway(database, clock, UnusedTransport(), allowed_model_ids={"deepseek-flash"})
    tokens = ProxyTokenRegistry()
    proxy = ModelProxy(gateway, tokens, provider, clock)
    await proxy.start()
    try:
        binding = tokens.issue(
            owner=Owner(run_id="run-fixture", attempt_id="attempt-fixture"),
            epoch=1, model_id="deepseek-flash", reservation_id="attempt-fixture:model",
            reserved_tokens=256, output_limit=64, timeout_seconds=30,
        )
        patch = write_cli_patch(
            tmp_path / "restricted.patch.yml", policy_plugin=plugin, max_output_tokens=64,
        )
        result = await HeadlessCli(installation, patch, tmp_path / "managed").run(
            "Return the fixed bridge answer as plain text.",
            timeout_seconds=60,
            environment={
                "PERSONAL_AGENT_GATEWAY_BASE_URL": proxy.base_url,
                "PERSONAL_AGENT_GATEWAY_TOKEN": binding.token,
                "PERSONAL_AGENT_PROFILE_PATH": str(ROOT / "code/runtime-policy/profile.json"),
            },
        )
    finally:
        await proxy.stop()

    assert result.status == "completed", {
        "exit_code": result.exit_code, "error_code": result.error_code,
    }
    assert result.exit_code == 0 and result.text == "fixed bridge answer"
    assert result.usage.model_requests == 1
    assert len(provider.calls) == 1
    spec = provider.calls[0]
    assert spec.owner.attempt_id == "attempt-fixture" and spec.epoch == 1
    assert spec.deadline == clock.now() + timedelta(seconds=30)
    assert await gateway.non_terminal_request() is None
    row = await gateway.describe(spec.request_id)
    assert row["status"] == "COMPLETED" and row["actual_tokens"] == 13


class ScriptedProvider:
    """First reply asks for the managed tool, second returns the final answer."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    async def complete(self, body: dict[str, Any], spec: ModelRequest) -> ProviderOutcome:
        self.bodies.append(json.loads(json.dumps(body)))
        if len(self.bodies) == 1:
            message = {
                "id": "msg_tool", "type": "message", "role": "assistant",
                "model": "deepseek-flash",
                "content": [{
                    "type": "tool_use", "id": "toolu_1",
                    "name": "mcp__personal_agent__http_fetch",
                    "input": {"url": "https://example.org/page"},
                }],
                "stop_reason": "tool_use", "stop_sequence": None,
                "usage": {"input_tokens": 7, "output_tokens": 3},
            }
        else:
            message = {
                "id": "msg_final", "type": "message", "role": "assistant",
                "model": "deepseek-flash",
                "content": [{"type": "text", "text": "final from tool evidence"}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 11, "output_tokens": 5},
            }
        return ProviderOutcome(
            status="completed", payload=message,
            usage=Usage(input_tokens=7, output_tokens=3, model_requests=1),
            completion_evidence_ref=f"fixed:{spec.request_id}:{len(self.bodies)}",
        )


class FakeFetcher:
    async def fetch(self, url: str, *, allowed_domains: Any) -> dict[str, Any]:
        assert tuple(allowed_domains) == ("example.org",)
        return {
            "url": url, "final_url": url, "status": 200,
            "text": "page body from the managed tool", "truncated": False,
            "fetched_at": "2026-10-20T00:00:00Z",
        }


async def test_managed_mcp_tool_and_every_model_call_stay_on_the_gateway(
    tmp_path: Path, database: Any, clock: Any, monkeypatch: Any,
) -> None:
    installation = configured_installation()
    if installation is None:
        pytest.skip("set PERSONAL_AGENT_DSH_CLI_PATH to the installed pinned CLI")
    plugin = ROOT / "code/runtime-policy/dist/index.js"
    if not plugin.exists():
        pytest.skip("build code/runtime-policy before running the bridge check")

    # A supplier credential in the app environment must never reach the CLI.
    monkeypatch.setenv("PERSONAL_AGENT_MODEL_API_KEY", "SENTINEL_SUPPLIER_KEY")
    provider = ScriptedProvider()
    scopes = ToolScopeRegistry()
    tools = ToolService(
        ToolHandlers(fetch=FakeFetcher()), scopes, clock,  # type: ignore[arg-type]
    )
    gateway = ModelGateway(database, clock, UnusedTransport(), allowed_model_ids={"deepseek-flash"})
    tokens = ProxyTokenRegistry()
    proxy = ModelProxy(gateway, tokens, provider, clock, tools=tools)
    await proxy.start()
    try:
        scope_token = scopes.issue(
            run_id="run-fixture", attempt_id="attempt-fixture", epoch=1,
            tool_name="http.fetch", allowed_domains=("example.org",), now=clock.now(),
        )
        binding = tokens.issue(
            owner=Owner(run_id="run-fixture", attempt_id="attempt-fixture"),
            epoch=1, model_id="deepseek-flash", reservation_id="attempt-fixture:model",
            reserved_tokens=512, output_limit=64, timeout_seconds=30,
            tool_scopes={"http.fetch": scope_token},
        )
        patch = write_cli_patch(
            tmp_path / "restricted.patch.yml",
            policy_plugin=plugin,
            max_output_tokens=64,
            tool_command=[
                sys.executable, "-m", "personal_agent.adapters.tools.mcp_stdio",
            ],
        )
        result = await HeadlessCli(installation, patch, tmp_path / "managed").run(
            "Fetch https://example.org/page and report what it contains.",
            timeout_seconds=90,
            environment={
                "PERSONAL_AGENT_GATEWAY_BASE_URL": proxy.base_url,
                "PERSONAL_AGENT_GATEWAY_TOKEN": binding.token,
                "PERSONAL_AGENT_PROFILE_PATH": str(ROOT / "code/runtime-policy/profile.json"),
                "PERSONAL_AGENT_SCOPE_EPOCH": "1",
            },
        )
    finally:
        await proxy.stop()

    assert result.status == "completed", {
        "exit_code": result.exit_code, "error_code": result.error_code,
    }
    assert result.text == "final from tool evidence"
    assert result.usage.tool_calls == 1
    # Two model calls, both settled through the single gateway slot.
    assert len(provider.bodies) == 2
    assert await gateway.non_terminal_request() is None
    # The tool body reached the model; a hash or character count would not.
    tool_results = [
        block for message in provider.bodies[1]["messages"]
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    rendered = json.dumps(tool_results, ensure_ascii=False)
    assert "page body from the managed tool" in rendered, rendered[:2000]
    # The supplier key stayed in the app process; the CLI only ever saw a token.
    assert "SENTINEL_SUPPLIER_KEY" not in json.dumps(provider.bodies)

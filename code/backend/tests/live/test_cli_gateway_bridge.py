"""Real pinned CLI through the loopback model gateway, with no supplier request.

The provider is a fixed in-process reply, so this test proves the wiring rather
than a model result: the official Loop's Messages calls must traverse the
loopback proxy and occupy/settle the persistent gateway slot. It runs only with
the explicit ``--live`` switch because it starts the real installed CLI.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from personal_agent.adapters.harness.adoption import configured_installation
from personal_agent.adapters.harness.cli import HeadlessCli
from personal_agent.adapters.harness.model_gateway import ModelGateway, ProviderOutcome
from personal_agent.adapters.harness.model_proxy import ModelProxy, ProxyTokenRegistry
from personal_agent.adapters.harness.patch import write_cli_patch
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
                "PERSONAL_AGENT_GATEWAY_BASE_URL": proxy.url("/anthropic"),
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

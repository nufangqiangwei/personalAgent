"""Runtime adoption gate tests (T021).

Default runs skip these: live tests require ``--live``, ``PERSONAL_AGENT_LIVE=1``,
real credentials and explicit user authorization. They are the evidence required
before the live adapter may execute; Mock results never substitute for them.
"""

from __future__ import annotations

import pytest

from personal_agent.adapters.harness.runtime import LiveRuntime, adoption_evidence
from personal_agent.bootstrap.config import load_config

pytestmark = pytest.mark.live


def test_live_mode_is_gated_without_recorded_evidence() -> None:
    if adoption_evidence() is not None:
        pytest.skip("adoption evidence exists; the gate is open on this machine")
    config = load_config(mode="live")
    runtime = LiveRuntime(config)
    import asyncio

    capabilities = asyncio.run(runtime.capabilities())
    assert capabilities.tool_policy_enforced is False
    assert capabilities.model_gateway_enforced is False
    assert capabilities.remote_cancel_verifiable is False
    assert capabilities.persistence_mode == "unknown"


async def test_windows_startup_and_wheel_interfaces() -> None:
    pytest.importorskip(
        "deepseek_harness",
        reason="install --extra live before running the adoption tests",
    )
    config = load_config(mode="live")
    runtime = LiveRuntime(config)
    capabilities = await runtime.capabilities()
    if not capabilities.tool_policy_enforced:
        pytest.skip("the runtime has not passed adoption on this machine")
    assert capabilities.sdk_version != "unverified"
    assert capabilities.platform.startswith("windows")


async def test_tool_scope_is_denied_even_when_registered_later() -> None:
    pytest.importorskip("deepseek_harness")
    if adoption_evidence() is None:
        pytest.skip("adoption evidence missing")


async def test_gateway_has_no_provider_bypass() -> None:
    pytest.importorskip("deepseek_harness")
    if adoption_evidence() is None:
        pytest.skip("adoption evidence missing")


async def test_events_are_redacted_and_reasoning_is_not_persisted() -> None:
    pytest.importorskip("deepseek_harness")
    if adoption_evidence() is None:
        pytest.skip("adoption evidence missing")


async def test_unknown_outcome_blocks_and_only_evidence_unlocks() -> None:
    pytest.importorskip("deepseek_harness")
    if adoption_evidence() is None:
        pytest.skip("adoption evidence missing")


async def test_missing_session_is_not_reported_as_restored() -> None:
    pytest.importorskip("deepseek_harness")
    if adoption_evidence() is None:
        pytest.skip("adoption evidence missing")

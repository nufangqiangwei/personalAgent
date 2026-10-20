"""Port mapping is tested independently of the production adoption gate."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from personal_agent.adapters.harness.cli import CliResult
from personal_agent.adapters.harness.cli_runtime import CliRuntime
from personal_agent.adapters.harness.model_proxy import ProxyTokenRegistry
from personal_agent.adapters.tools.scope import ToolScopeRegistry
from personal_agent.domain.common import AppError, new_id
from personal_agent.ports.contracts import Owner, RuntimeTask, SessionContext, Usage


class FixedRunner:
    """Fixed provider observations, held in memory; no subprocess/network."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.environments: list[dict[str, str] | None] = []
        self.ready = asyncio.Event()
        self.ready.set()
        self.result = CliResult(
            "completed", 0, "vendor-id", '{"answer":"ok"}',
            Usage(input_tokens=13, output_tokens=2, model_requests=1), "verified-completion",
        )

    async def run(
        self, prompt: str, *, timeout_seconds: float,
        environment: dict[str, str] | None = None,
    ) -> CliResult:
        self.calls.append(prompt)
        self.environments.append(environment)
        await self.ready.wait()
        return self.result


def context() -> SessionContext:
    return SessionContext(new_id(), Owner(run_id=new_id(), attempt_id=new_id()),
                          1, "model", "profile", "capability", "policy")


def task(session_id: str, clock: Any) -> RuntimeTask:
    return RuntimeTask(
        new_id(), session_id, 1, "return the answer", (), {"input": "example"},
        {"type": "object", "required": ["answer"], "additionalProperties": False,
         "properties": {"answer": {"type": "string", "minLength": 1}}},
        clock.now() + timedelta(seconds=30), (), "reservation",
        model_reserved_tokens=128, model_output_limit=32,
    )


async def test_session_idempotency_and_changed_context_rejection(clock: Any) -> None:
    runner = FixedRunner()
    runtime = CliRuntime(runner, clock, profile_hash="profile")
    ctx = context()
    first = await runtime.create_session(ctx)
    assert await runtime.create_session(ctx) == first
    assert not runner.calls and first.provider_session_id is None
    with pytest.raises(AppError) as error:
        await runtime.create_session(replace(ctx, epoch=2))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


async def test_accepted_is_not_completed_and_same_submission_is_not_reexecuted(clock: Any) -> None:
    runner = FixedRunner()
    runner.ready.clear()
    runtime = CliRuntime(runner, clock, profile_hash="profile")
    handle = await runtime.create_session(context())
    request = task(handle.adapter_session_id, clock)
    submission = await runtime.submit(request)
    assert submission.status == "accepted"
    assert await runtime.submit(request) == submission
    waiting = asyncio.create_task(runtime.wait_result(submission.submission_id))
    await asyncio.sleep(0)
    assert not waiting.done()
    runner.ready.set()
    result = await waiting
    assert result.status == "completed" and result.structured_output == {"answer": "ok"}
    assert len(runner.calls) == 1
    prompt = json.loads(runner.calls[0])
    assert prompt["output_schema"] == dict(request.output_schema)
    assert "capability" not in runner.calls[0]
    events = [event async for event in runtime.events(handle.adapter_session_id)]
    assert [event.kind for event in events] == ["session.started", "result.available"]
    cursor = events[0].sequence
    stream = runtime.events(handle.adapter_session_id, cursor)
    remaining = [event.sequence async for event in stream]
    assert remaining == [2]
    reconciled = await runtime.reconcile(handle.adapter_session_id)
    assert reconciled.local_runtime_status == "completed"
    assert reconciled.completion_evidence


@pytest.mark.parametrize("text", ['{"wrong":"x"}', '[]', 'not JSON', '{"answer":""}'])
async def test_invalid_output_fails_but_retains_verified_usage(text: str, clock: Any) -> None:
    runner = FixedRunner()
    runner.result = replace(runner.result, text=text)
    runtime = CliRuntime(runner, clock, profile_hash="profile")
    handle = await runtime.create_session(context())
    submission = await runtime.submit(task(handle.adapter_session_id, clock))
    result = await runtime.wait_result(submission.submission_id)
    assert result.status == "failed"
    assert result.error and result.error.code == "RUNTIME_OUTPUT_INVALID"
    assert result.usage.tokens == 15 and result.completion_evidence_ref


async def test_stop_fences_new_actions_and_quarantines_late_success(clock: Any) -> None:
    runner = FixedRunner()
    runner.ready.clear()
    runtime = CliRuntime(runner, clock, profile_hash="profile")
    handle = await runtime.create_session(context())
    request = task(handle.adapter_session_id, clock)
    submission = await runtime.submit(request)
    await asyncio.sleep(0)
    stop = await runtime.request_stop(handle.adapter_session_id, "pause")
    assert stop.status == "requested"
    with pytest.raises(AppError) as error:
        await runtime.submit(replace(request, operation_id=new_id()))
    assert error.value.code == "RUN_STOPPED"
    runner.ready.set()
    result = await runtime.wait_result(submission.submission_id)
    assert result.status == "failed" and result.structured_output is None
    assert result.usage.tokens == 15 and result.completion_evidence_ref


async def test_dispose_keeps_unknown_and_never_claims_original_session_restored(clock: Any) -> None:
    runner = FixedRunner()
    runner.ready.clear()
    runtime = CliRuntime(runner, clock, profile_hash="profile")
    handle = await runtime.create_session(context())
    submission = await runtime.submit(task(handle.adapter_session_id, clock))
    await asyncio.sleep(0)
    await runtime.dispose(handle.runtime_id)
    result = await runtime.wait_result(submission.submission_id)
    assert result.status == "unknown" and result.completion_evidence_ref is None
    assert (await runtime.reconcile(handle.adapter_session_id)).local_runtime_status == "unknown"
    with pytest.raises(AppError) as error:
        await runtime.restore_session(handle, {})
    assert error.value.code == "SESSION_MISSING"


async def test_context_epoch_profile_and_expired_task_fail_before_launch(clock: Any) -> None:
    runner = FixedRunner()
    runtime = CliRuntime(runner, clock, profile_hash="profile")
    with pytest.raises(AppError):
        await runtime.create_session(replace(context(), profile_hash="changed"))
    handle = await runtime.create_session(context())
    request = task(handle.adapter_session_id, clock)
    for invalid in [replace(request, epoch=2), replace(request, deadline=clock.now())]:
        with pytest.raises(AppError):
            await runtime.submit(invalid)
    assert runner.calls == []


async def test_dispose_before_worker_starts_still_returns_unknown(clock: Any) -> None:
    runner = FixedRunner()
    runtime = CliRuntime(runner, clock, profile_hash="profile")
    handle = await runtime.create_session(context())
    submission = await runtime.submit(task(handle.adapter_session_id, clock))
    await runtime.dispose(handle.runtime_id)
    result = await runtime.wait_result(submission.submission_id)
    assert result.status == "unknown" and result.completion_evidence_ref is None
    assert (await runtime.reconcile(handle.adapter_session_id)).local_runtime_status == "unknown"
    assert runner.calls == []


async def test_cancelling_one_waiter_does_not_cancel_the_owned_process(clock: Any) -> None:
    runner = FixedRunner()
    runner.ready.clear()
    runtime = CliRuntime(runner, clock, profile_hash="profile")
    handle = await runtime.create_session(context())
    submission = await runtime.submit(task(handle.adapter_session_id, clock))
    waiter = asyncio.create_task(runtime.wait_result(submission.submission_id))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    runner.ready.set()
    assert (await runtime.wait_result(submission.submission_id)).status == "completed"


async def test_attempt_token_and_tool_scopes_are_issued_then_revoked(clock: Any) -> None:
    runner = FixedRunner()
    runner.ready.clear()
    tokens = ProxyTokenRegistry()
    scopes = ToolScopeRegistry()
    runtime = CliRuntime(
        runner, clock, profile_hash="profile",
        tokens=tokens, gateway_base_url="http://127.0.0.1:1",
        profile_path="C:/runtime/profile.json", scopes=scopes,
    )
    handle = await runtime.create_session(context())
    request = replace(
        task(handle.adapter_session_id, clock),
        tool_scope=("search.web",), allowed_domains=("example.org",),
    )
    submission = await runtime.submit(request)
    for _ in range(100):
        if runner.environments:
            break
        await asyncio.sleep(0.01)
    environment = runner.environments[0]
    assert environment is not None
    assert environment["PERSONAL_AGENT_GATEWAY_BASE_URL"] == "http://127.0.0.1:1"
    assert environment["PERSONAL_AGENT_PROFILE_PATH"] == "C:/runtime/profile.json"
    binding = tokens.resolve(environment["PERSONAL_AGENT_GATEWAY_TOKEN"])
    assert binding is not None
    assert binding.reservation_id == "reservation"
    grant = scopes.validate(
        token=binding.tool_scopes["search.web"], tool_name="search.web",
        epoch=1, now=clock.now(),
    )
    assert grant.allowed_domains == ("example.org",)

    runner.ready.set()
    result = await runtime.wait_result(submission.submission_id)
    assert result.status == "completed"
    assert tokens.resolve(environment["PERSONAL_AGENT_GATEWAY_TOKEN"]) is None
    with pytest.raises(AppError):
        scopes.validate(
            token=binding.tool_scopes["search.web"], tool_name="search.web",
            epoch=1, now=clock.now(),
        )

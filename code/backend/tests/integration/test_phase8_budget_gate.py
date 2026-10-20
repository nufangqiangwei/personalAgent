"""T087 checkpoint: a stricter workflow limit must trigger real engine assistance.

Uses public draft/confirmation/start APIs and an injected clock/runtime observation.
No business state is edited to manufacture WAITING_USER or an assistance request.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import httpx
import pytest

from personal_agent.application.workflow_registry import WorkflowRegistry
from personal_agent.bootstrap.seed import seed
from personal_agent.domain.common import AppError, ErrorCategory, new_id


class AdvancingRuntime:
    def __init__(self, inner: Any, clock: Any) -> None:
        self.inner = inner
        self.clock = clock

    async def wait_result(self, submission_id: str) -> Any:
        result = await self.inner.wait_result(submission_id)
        self.clock.advance(seconds=2)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


async def start_limited_run(
    client: httpx.AsyncClient,
    api_context: Any,
    dimension: str,
    value: int = 1,
    step_attempts: int | None = None,
    configure: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    config, context = api_context
    await seed(context.database)
    registry = WorkflowRegistry(context.database, context.clock)
    summary = next(
        item for item in await registry.list_summaries() if item["name"] == "公开主题资料研究"
    )
    old, _ = await registry.definition_of(summary["latest_version"]["id"])
    definition = copy.deepcopy(old.definition_json)
    definition["execution_policy"][dimension] = value
    if step_attempts is not None:
        definition["steps"][0]["policy"]["max_attempts"] = step_attempts
    if configure:
        configure(definition)
    saved = await registry.save_version(
        workflow_id=old.workflow_id,
        base_version_id=old.id,
        definition=definition,
        change_summary=f"T087 deterministic {dimension} limit",
        confirm_executable=True,
    )
    version_id = saved.version.id
    context.engine._deps.runtime = AdvancingRuntime(context.runtime, context.clock)

    created = await client.post(
        "/api/v1/requirements",
        json={"workflow_version_id": version_id},
        headers={"Idempotency-Key": new_id()},
    )
    assert created.status_code == 201, created.text
    draft_id = created.json()["id"]
    analysis = await client.post(
        f"/api/v1/requirements/{draft_id}/messages",
        json={"text": "12 人团队研究公共图书馆夜间开放"},
        headers={"Idempotency-Key": new_id()},
    )
    assert analysis.status_code == 202, analysis.text
    draft = (await client.get(f"/api/v1/requirements/{draft_id}")).json()
    assert draft["status"] == "READY", draft
    confirmed = await client.post(
        f"/api/v1/requirements/{draft_id}/confirm",
        json={
            "draft_revision": draft["revision"],
            "summary_hash": draft["summary_hash"],
            "workflow_version_id": version_id,
            "budget_policy_hash": config.policy.policy_hash,
            "data_policy_version": config.policy.version,
            "action": "START",
            "explicit_start_confirmation": True,
        },
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{draft["revision"]}"'},
    )
    assert confirmed.status_code == 201, confirmed.text
    started = await client.post(
        "/api/v1/runs",
        json={"confirmation_id": confirmed.json()["confirmation_id"]},
        headers={"Idempotency-Key": new_id()},
    )
    assert started.status_code == 201, started.text
    return str(started.json()["id"])


@pytest.mark.parametrize("dimension", ["max_model_requests", "max_active_seconds"])
async def test_stricter_workflow_limit_enters_budget_assistance(
    client: httpx.AsyncClient, api_context: Any, dimension: str
) -> None:
    _, context = api_context
    run_id = await start_limited_run(client, api_context, dimension)
    status = await context.executor.wait(run_id, max_wait=60)
    snapshot = (await client.get(f"/api/v1/runs/{run_id}")).json()
    requests = (await client.get(f"/api/v1/runs/{run_id}/assistance")).json()["items"]
    assert status == "WAITING_USER", {"status": status, "snapshot": snapshot, "requests": requests}
    assert snapshot["reason_code"] == "BUDGET_LIMIT"
    assert any(item["kind"] == "BUDGET_LIMIT" and item["status"] == "OPEN" for item in requests)


@pytest.mark.parametrize("dimension", ["max_model_requests", "max_active_seconds", "max_tokens"])
async def test_budget_wait_needs_explicit_grant_and_preserves_id_and_consumption(
    client: httpx.AsyncClient, api_context: Any, dimension: str
) -> None:
    _, context = api_context
    run_id = await start_limited_run(client, api_context, dimension)
    assert await context.executor.wait(run_id, max_wait=60) == "WAITING_USER"
    before = (await client.get(f"/api/v1/runs/{run_id}")).json()
    requests = (await client.get(f"/api/v1/runs/{run_id}/assistance")).json()["items"]
    request = next(item for item in requests if item["kind"] == "BUDGET_LIMIT")
    assert request["limit"] == before["budget"]["limits"]
    assert request["consumed"] == before["budget"]["consumed"]
    assert request["remaining_steps"] and request["observed_problem"]
    dimension_key = {
        "max_model_requests": "model_requests",
        "max_active_seconds": "active_ms",
        "max_tokens": "tokens",
    }[dimension]
    assert before["budget"]["limits"][dimension_key] == (
        1000 if dimension_key == "active_ms" else 1
    )

    # No answer and wall-clock passage cannot start another action or charge waiting time.
    context.clock.advance(seconds=7200)
    after_wait = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert after_wait["budget"] == before["budget"]
    assert after_wait["status"] == "WAITING_USER"
    endpoint = f"/api/v1/runs/{run_id}/assistance/{request['id']}/decisions"

    async def decide(**body: Any) -> httpx.Response:
        return await client.post(
            endpoint,
            json={"revision": request["revision"], **body},
            headers={"Idempotency-Key": new_id(), "If-Match": f'"{request["revision"]}"'},
        )

    discussed = await decide(decision="DISCUSS", note="先说明问题，尚未授权继续")
    assert discussed.status_code == 200, discussed.text
    request = (await client.get(f"/api/v1/runs/{run_id}/assistance")).json()["items"][0]
    assert request["status"] == "OPEN"
    for continue_flag, cost_flag in [(False, True), (True, False)]:
        refused = await decide(
            decision="GRANT_AND_CONTINUE",
            approved_increment={dimension_key: 100000},
            continue_authorized=continue_flag,
            acknowledge_cost=cost_flag,
        )
        assert refused.status_code == 422, refused.text
    still_waiting = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert still_waiting["status"] == "WAITING_USER"
    assert still_waiting["budget"] == before["budget"]

    granted = await decide(
        decision="GRANT_AND_CONTINUE",
        approved_increment={dimension_key: 100000},
        continue_authorized=True,
        acknowledge_cost=True,
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["run"]["id"] == run_id
    assert await context.executor.wait(run_id, max_wait=60) == "SUCCEEDED"
    final = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert final["artifact_ids"]
    for key, value in before["budget"]["consumed"].items():
        assert final["budget"]["consumed"][key] >= value
    if dimension == "max_active_seconds":
        assert 2000 <= final["budget"]["consumed"]["active_ms"] < 10000
    closed = (await client.get(f"/api/v1/runs/{run_id}/assistance")).json()["items"]
    assert len(closed) == 1 and closed[0]["status"] == "RESOLVED"


class FailingRuntime(AdvancingRuntime):
    calls = 0

    async def wait_result(self, submission_id: str) -> Any:
        self.calls += 1
        result = await super().wait_result(submission_id)
        return replace(
            result,
            status="failed",
            error=AppError(
                code="OBSERVED_FAILURE", message="固定失败观测", category=ErrorCategory.DEPENDENCY
            ),
        )


async def test_retry_cannot_continue_after_active_time_is_exhausted(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    failing = FailingRuntime(context.runtime, context.clock)
    context.runtime = failing
    run_id = await start_limited_run(client, api_context, "max_active_seconds")
    assert await context.executor.wait(run_id, max_wait=60) == "WAITING_USER"
    assert failing.calls == 1
    requests = (await client.get(f"/api/v1/runs/{run_id}/assistance")).json()["items"]
    assert requests[0]["kind"] == "BUDGET_LIMIT"


async def test_node_attempt_limit_is_stricter_than_global(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    failing = FailingRuntime(context.runtime, context.clock)
    context.runtime = failing
    run_id = await start_limited_run(client, api_context, "max_model_requests", 30, step_attempts=1)
    assert await context.executor.wait(run_id, max_wait=60) == "FAILED"
    assert failing.calls == 1


async def test_grant_must_cover_the_blocked_dimension_before_resuming(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    run_id = await start_limited_run(client, api_context, "max_model_requests")
    assert await context.executor.wait(run_id, max_wait=60) == "WAITING_USER"
    before = (await client.get(f"/api/v1/runs/{run_id}")).json()
    item = (await client.get(f"/api/v1/runs/{run_id}/assistance")).json()["items"][0]
    response = await client.post(
        f"/api/v1/runs/{run_id}/assistance/{item['id']}/decisions",
        json={
            "revision": item["revision"],
            "decision": "GRANT_AND_CONTINUE",
            "approved_increment": {"tokens": 100000},
            "continue_authorized": True,
            "acknowledge_cost": True,
        },
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{item["revision"]}"'},
    )
    assert response.status_code == 409, response.text
    assert (await client.get(f"/api/v1/runs/{run_id}")).json()["budget"] == before["budget"]


async def test_insufficient_token_reservation_can_be_explicitly_increased(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    # There is positive remaining budget, but not enough to bound a model request.
    _, context = api_context
    run_id = await start_limited_run(client, api_context, "max_tokens", 100)
    assert await context.executor.wait(run_id, max_wait=60) == "WAITING_USER"
    before = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert before["budget"]["consumed"]["model_requests"] == 0
    item = (await client.get(f"/api/v1/runs/{run_id}/assistance")).json()["items"][0]
    response = await client.post(
        f"/api/v1/runs/{run_id}/assistance/{item['id']}/decisions",
        json={
            "revision": item["revision"],
            "decision": "GRANT_AND_CONTINUE",
            "approved_increment": {"tokens": 100000},
            "continue_authorized": True,
            "acknowledge_cost": True,
        },
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{item["revision"]}"'},
    )
    assert response.status_code == 200, response.text
    assert await context.executor.wait(run_id, max_wait=60) == "SUCCEEDED"


async def test_runtime_deadline_includes_remaining_active_budget(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    inner = context.runtime
    deadlines = []

    class ObservedRuntime:
        async def submit(self, task: Any) -> Any:
            deadlines.append((task.deadline - context.clock.now()).total_seconds())
            return await inner.submit(task)

        def __getattr__(self, name: str) -> Any:
            return getattr(inner, name)

    context.runtime = ObservedRuntime()
    run_id = await start_limited_run(client, api_context, "max_active_seconds")
    await context.executor.wait(run_id, max_wait=60)
    assert deadlines and 0 < deadlines[0] <= 1


async def test_active_deadline_stops_waiting_for_a_runtime_result(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    inner = context.runtime

    class NeverReturns:
        async def wait_result(self, submission_id: str) -> Any:
            try:
                await asyncio.Event().wait()
            finally:
                context.clock.advance(seconds=2)

        def __getattr__(self, name: str) -> Any:
            return getattr(inner, name)

    context.runtime = NeverReturns()
    run_id = await start_limited_run(client, api_context, "max_active_seconds")
    assert await context.executor.wait(run_id, max_wait=3) == "WAITING_USER"
    snapshot = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert snapshot["reason_code"] == "BUDGET_LIMIT"
    assert snapshot["budget"]["consumed"]["active_ms"] >= 1000

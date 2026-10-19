"""Run control contract tests (T071).

Pause, cancel, resume, recovery diagnosis and history are exercised against a
deterministic runtime gate: the first model submission blocks until the test
releases it, so a stop request can arrive while an action is genuinely in flight.

The run id never changes, a stop is reported as draining until the in-flight action
returns, and a resumed run continues from the recorded transition instead of redoing
a step that already succeeded.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from personal_agent.adapters.persistence.execution_models import (
    StepAttempt,
    StepRun,
    WorkflowRun,
)
from personal_agent.adapters.persistence.requirement_models import (
    Confirmation,
    RequirementDraft,
)
from personal_agent.application.workflow_registry import WorkflowRegistry
from personal_agent.bootstrap.seed import seed
from personal_agent.domain.common import new_id, stable_hash

TOPIC_WORKFLOW = "公开主题资料研究"


class GatedRuntime:
    """Wraps the mock runtime so a submission can be held open on purpose."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block = True

    async def submit(self, task: Any) -> Any:
        self.entered.set()
        if self.block:
            await self.release.wait()
        return await self._inner.submit(task)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _confirmation(api_context: Any, fields: dict[str, Any]) -> str:
    config, context = api_context
    await seed(context.database)
    registry = WorkflowRegistry(context.database, context.clock)
    summaries = await registry.list_summaries()
    version_id = next(
        item["latest_version"]["id"] for item in summaries if item["name"] == TOPIC_WORKFLOW
    )
    field_values = {
        key: {
            "path": key,
            "value": value,
            "source": "user",
            "message_id": None,
            "suggestion_id": None,
            "accepted_at": None,
            "explicit_none": False,
        }
        for key, value in fields.items()
    }
    snapshot = {"fields": field_values, "preference_order": [], "output_spec": {}}
    async with context.database.session() as session:
        draft = RequirementDraft(
            id=new_id(),
            selected_version_id=version_id,
            revision=2,
            status="CONFIRMED",
            missing_fields_json=[],
            conflicts_json=[],
            field_values_json=field_values,
            preference_order_json=[],
            output_spec_json={},
            summary_markdown="",
            summary_hash="sha256:summary",
            source_draft_id=None,
            created_at=context.clock.now(),
            updated_at=context.clock.now(),
        )
        session.add(draft)
        await session.flush()
        confirmation = Confirmation(
            id=new_id(),
            draft_id=draft.id,
            draft_revision=1,
            workflow_version_id=version_id,
            summary_hash="sha256:summary",
            input_hash=stable_hash(snapshot),
            budget_policy_hash=config.policy.policy_hash,
            data_policy_version=config.policy.version,
            confirmed_at=context.clock.now(),
            action="START",
            field_snapshot_json=snapshot,
        )
        session.add(confirmation)
        await session.commit()
        return confirmation.id


async def _start(client: httpx.AsyncClient, api_context: Any, gate: GatedRuntime) -> str:
    _, context = api_context
    context.engine._deps.runtime = gate  # type: ignore[attr-defined]
    confirmation_id = await _confirmation(api_context, {"topic": "图书馆夜间开放"})
    response = await client.post(
        "/api/v1/runs",
        json={"confirmation_id": confirmation_id},
        headers={"Idempotency-Key": new_id()},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _snapshot(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    return (await client.get(f"/api/v1/runs/{run_id}")).json()


async def _steps(api_context: Any, run_id: str) -> list[StepRun]:
    _, context = api_context
    async with context.database.session() as session:
        from sqlalchemy import select

        return list(
            (
                await session.scalars(select(StepRun).where(StepRun.run_id == run_id))
            ).all()
        )


async def _attempts(api_context: Any, run_id: str) -> list[StepAttempt]:
    _, context = api_context
    async with context.database.session() as session:
        from sqlalchemy import select

        return list(
            (
                await session.scalars(
                    select(StepAttempt)
                    .join(StepRun, StepAttempt.step_run_id == StepRun.id)
                    .where(StepRun.run_id == run_id)
                    .order_by(StepAttempt.attempt_no)
                )
            ).all()
        )


async def test_pause_keeps_the_run_id_and_resume_does_not_redo_a_finished_step(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = GatedRuntime(context.runtime)
    run_id = await _start(client, api_context, gate)
    await asyncio.wait_for(gate.entered.wait(), timeout=10)

    # The in-flight action is visible, and the stop is accepted immediately.
    first = await _snapshot(client, run_id)
    assert first["status"] == "RUNNING"
    pause = await client.post(
        f"/api/v1/runs/{run_id}/pause",
        json={"revision": first["revision"], "reason": "先停下来看看"},
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{first["revision"]}"'},
    )
    assert pause.status_code == 200, pause.text
    paused = pause.json()
    assert paused["status"] in {"PAUSING", "PAUSED"}
    assert paused["draining"] is True or paused["status"] == "PAUSED"

    gate.release.set()
    status = await context.executor.wait(run_id, max_wait=60)
    assert status == "PAUSED"
    stopped = await _snapshot(client, run_id)
    assert stopped["id"] == run_id
    assert stopped["root_run_id"] == run_id
    executed_before = {
        step.step_id: step
        for step in await _steps(api_context, run_id)
        if step.status == "SUCCEEDED"
    }
    attempts_before = {
        attempt.step_run_id: attempt.attempt_no for attempt in await _attempts(api_context, run_id)
    }
    assert executed_before, "the paused run must have finished at least one step"

    # Resume continues on the same run with a new epoch.
    resumed = await client.post(
        f"/api/v1/runs/{run_id}/resume",
        json={"revision": stopped["revision"]},
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{stopped["revision"]}"'},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["id"] == run_id
    assert resumed.json()["epoch"] > stopped["epoch"]
    final_status = await context.executor.wait(run_id, max_wait=60)
    assert final_status == "SUCCEEDED"

    final = await _snapshot(client, run_id)
    assert final["status"] == "SUCCEEDED"
    assert final["artifact_ids"], "the resumed run still delivers its document"

    # Previously succeeded steps were not executed again.
    steps_after = {step.step_id: step for step in await _steps(api_context, run_id)}
    for step_id, step in executed_before.items():
        assert steps_after[step_id].status == "SUCCEEDED"
        assert steps_after[step_id].selected_transition == step.selected_transition
    first_attempts = [
        attempt for attempt in await _attempts(api_context, run_id)
        if attempt.step_run_id in attempts_before
    ]
    assert len(first_attempts) == len(attempts_before)


async def test_cancel_converges_and_a_late_result_cannot_succeed(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = GatedRuntime(context.runtime)
    run_id = await _start(client, api_context, gate)
    await asyncio.wait_for(gate.entered.wait(), timeout=10)

    current = await _snapshot(client, run_id)
    cancel = await client.post(
        f"/api/v1/runs/{run_id}/cancel",
        json={"revision": current["revision"], "confirmed": True, "reason": "不需要了"},
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{current["revision"]}"'},
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] in {"CANCELLING", "CANCELLED"}
    assert cancel.json()["in_flight_actions"], "the in-flight action must be reported"

    gate.release.set()
    status = await context.executor.wait(run_id, max_wait=60)
    assert status == "CANCELLED"

    final = await _snapshot(client, run_id)
    assert final["status"] == "CANCELLED"
    assert final["artifact_ids"] == []
    assert [item["action"] for item in final["allowed_actions"]] == ["CANCEL"]
    assert final["allowed_actions"][0]["enabled"] is False

    async with context.database.session() as session:
        from sqlalchemy import select

        from personal_agent.adapters.persistence.execution_models import ExecutionEvent

        events = list(
            (
                await session.scalars(
                    select(ExecutionEvent).where(ExecutionEvent.run_id == run_id)
                )
            ).all()
        )
    assert any(event.kind == "late_result.quarantined" for event in events), [
        event.kind for event in events
    ]
    attempts = await _attempts(api_context, run_id)
    assert attempts and all(attempt.status != "SUCCEEDED" for attempt in attempts)


async def test_cancel_requires_an_explicit_confirmation(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = GatedRuntime(context.runtime)
    run_id = await _start(client, api_context, gate)
    await asyncio.wait_for(gate.entered.wait(), timeout=10)
    current = await _snapshot(client, run_id)
    response = await client.post(
        f"/api/v1/runs/{run_id}/cancel",
        json={"revision": current["revision"], "confirmed": False},
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{current["revision"]}"'},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CANCEL_NOT_CONFIRMED"
    gate.release.set()
    await context.executor.wait(run_id, max_wait=60)


async def test_stale_revision_and_missing_precondition_are_rejected(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = GatedRuntime(context.runtime)
    gate.block = False
    run_id = await _start(client, api_context, gate)
    await context.executor.wait(run_id, max_wait=60)
    current = await _snapshot(client, run_id)

    missing = await client.post(
        f"/api/v1/runs/{run_id}/pause",
        json={"revision": current["revision"]},
        headers={"Idempotency-Key": new_id()},
    )
    assert missing.status_code == 428

    stale = await client.post(
        f"/api/v1/runs/{run_id}/pause",
        json={"revision": current["revision"]},
        headers={
            "Idempotency-Key": new_id(),
            "If-Match": f'"{current["revision"] + 99}"',
        },
    )
    assert stale.status_code == 412
    assert stale.json()["error"]["code"] == "RUN_REVISION_STALE"

    # A terminal run cannot be paused.
    conflict = await client.post(
        f"/api/v1/runs/{run_id}/pause",
        json={"revision": current["revision"]},
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{current["revision"]}"'},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "RUN_STATE_INVALID"


async def test_recovery_report_and_history_expose_the_trusted_record(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = GatedRuntime(context.runtime)
    run_id = await _start(client, api_context, gate)
    await asyncio.wait_for(gate.entered.wait(), timeout=10)
    current = await _snapshot(client, run_id)
    await client.post(
        f"/api/v1/runs/{run_id}/pause",
        json={"revision": current["revision"]},
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{current["revision"]}"'},
    )
    gate.release.set()
    await context.executor.wait(run_id, max_wait=60)

    report = (await client.get(f"/api/v1/runs/{run_id}/recovery")).json()
    assert report["status"] == "PAUSED"
    assert report["resumable"] is True
    assert report["blocked_by"] is None
    codes = {check["code"] for check in report["checks"]}
    assert {"VERSION", "RUNTIME", "POLICY", "INPUT", "SIDE_EFFECTS", "ARTIFACTS", "MODEL"} <= codes
    assert all(check["ok"] for check in report["checks"])

    history = (await client.get(f"/api/v1/runs/{run_id}/history")).json()
    assert history["root_run_id"] == run_id
    entry = history["items"][0]
    assert entry["id"] == run_id
    assert entry["steps"], "history must include step transitions"
    assert any(step["selected_transition"] for step in entry["steps"])

    # Resume once more so the run is terminal and no task is left running.
    paused = await _snapshot(client, run_id)
    await client.post(
        f"/api/v1/runs/{run_id}/resume",
        json={"revision": paused["revision"]},
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{paused["revision"]}"'},
    )
    assert await context.executor.wait(run_id, max_wait=60) == "SUCCEEDED"
    async with context.database.session() as session:
        row = await session.get(WorkflowRun, run_id)
        assert row is not None
        assert row.status == "SUCCEEDED"
        assert row.epoch >= 2


async def test_unknown_run_controls_return_404(client: httpx.AsyncClient) -> None:
    unknown = "00000000-0000-4000-8000-000000000000"
    for path, body in (
        ("pause", {"revision": 1}),
        ("cancel", {"revision": 1, "confirmed": True}),
        ("resume", {"revision": 1}),
    ):
        response = await client.post(
            f"/api/v1/runs/{unknown}/{path}",
            json=body,
            headers={"Idempotency-Key": new_id(), "If-Match": '"1"'},
        )
        assert response.status_code == 404, (path, response.text)
        assert response.json()["error"]["code"] == "RUN_NOT_FOUND"
    report = await client.get(f"/api/v1/runs/{unknown}/recovery")
    assert report.status_code == 404

"""Run control contract tests (T071).

Pause, cancel, resume, recovery diagnosis and history are exercised against a
deterministic runtime gate: submitted results wait until the test releases them,
so a stop request can arrive before the engine observes the completed action.

The run id never changes, a stop is reported as draining until the in-flight action
returns, and a resumed run continues from the recorded transition instead of redoing
a step that already succeeded.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from personal_agent.adapters.persistence.assistance_models import AssistanceRequest
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
    """Hold a submitted result before it is observed by the engine."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block = True

    async def submit(self, task: Any) -> Any:
        return await self._inner.submit(task)

    async def wait_result(self, submission_id: str) -> Any:
        self.entered.set()
        if self.block:
            await self.release.wait()
        return await self._inner.wait_result(submission_id)

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


# ----------------------------------------------------------------------
# Assistance decisions (T078): the answer has to satisfy the review contract.
# ----------------------------------------------------------------------
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"decision": {"type": "string", "enum": ["CONTINUE", "STOP"]}},
    "required": ["decision"],
    "additionalProperties": False,
}


async def _review_workflow(
    api_context: Any, workflow_definition: dict[str, Any]
) -> tuple[str, str]:
    """Publish a workflow whose condition sends a normal run to a human review."""
    import copy

    _, context = api_context
    definition = copy.deepcopy(workflow_definition)
    check = next(step for step in definition["steps"] if step["id"] == "check")
    # The condition is deterministically false, so a normal run reaches the review.
    check["config"]["expression"] = {
        "op": "eq",
        "left": {"literal": "left"},
        "right": {"literal": "right"},
    }
    review = next(step for step in definition["steps"] if step["id"] == "review")
    review["config"]["prompt"] = "来源冲突无法归类，是否继续？"
    review["config"]["decision_schema"] = REVIEW_SCHEMA
    registry = WorkflowRegistry(context.database, context.clock)
    outcome = await registry.create(
        definition=definition, change_summary="review flow", confirm_executable=True
    )
    return outcome.version.workflow_id, outcome.version.id


async def _start_review_run(
    client: httpx.AsyncClient,
    api_context: Any,
    workflow_definition: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    config, context = api_context
    _workflow_id, version_id = await _review_workflow(api_context, workflow_definition)
    field_values = {
        "topic": {
            "path": "topic",
            "value": "图书馆夜间开放",
            "source": "user",
            "message_id": None,
            "suggestion_id": None,
            "accepted_at": None,
            "explicit_none": False,
        }
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
    response = await client.post(
        "/api/v1/runs",
        json={"confirmation_id": confirmation.id},
        headers={"Idempotency-Key": new_id()},
    )
    assert response.status_code == 201, response.text
    run_id = str(response.json()["id"])
    assert await context.executor.wait(run_id, max_wait=60) == "WAITING_USER"
    items = (await client.get(f"/api/v1/runs/{run_id}/assistance")).json()["items"]
    review = next(item for item in items if item["kind"] == "HUMAN_REVIEW")
    return run_id, review


async def _decide(
    client: httpx.AsyncClient,
    run_id: str,
    review: dict[str, Any],
    **body: Any,
) -> httpx.Response:
    payload = {"revision": review["revision"], "decision": "GRANT_AND_CONTINUE", **body}
    return await client.post(
        f"/api/v1/assistance/{review['id']}/decisions",
        json=payload,
        headers={
            "Idempotency-Key": new_id(),
            "If-Match": f'"{review["revision"]}"',
        },
    )


async def test_human_review_requires_an_answer_that_satisfies_its_contract(
    client: httpx.AsyncClient, api_context: Any, workflow_definition: dict[str, Any]
) -> None:
    run_id, review = await _start_review_run(client, api_context, workflow_definition)
    assert review["decision_schema"] == REVIEW_SCHEMA
    assert "GRANT_AND_CONTINUE" in review["available_decisions"]
    assert "RECONCILE" not in review["available_decisions"]

    # Chatting is recorded but does not resume the run.
    discuss = await _decide(client, run_id, review, decision="DISCUSS", note="先讨论一下")
    assert discuss.status_code == 200, discuss.text
    assert discuss.json()["resolved"] is False
    assert discuss.json()["blocked"] is True
    still_open = (
        await client.get(f"/api/v1/assistance/{review['id']}")
    ).json()
    assert still_open["status"] == "OPEN"
    assert still_open["decisions"][0]["decision"] == "DISCUSS"

    # A write-reconciliation decision is not offered for a review.
    unavailable = await _decide(client, run_id, review, decision="RECONCILE")
    assert unavailable.status_code == 422
    assert unavailable.json()["error"]["code"] == "ASSISTANCE_DECISION_UNAVAILABLE"

    # The answer must satisfy the declared schema.
    invalid = await _decide(
        client, run_id, review, review_result={"decision": "MAYBE"}, continue_authorized=True
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "ASSISTANCE_REVIEW_INVALID"
    missing_key = await _decide(
        client, run_id, review, review_result={}, continue_authorized=True
    )
    assert missing_key.status_code == 422
    assert missing_key.json()["error"]["code"] == "ASSISTANCE_REVIEW_INVALID"
    extra_key = await _decide(
        client,
        run_id,
        review,
        review_result={"decision": "CONTINUE", "extra": 1},
        continue_authorized=True,
    )
    assert extra_key.status_code == 422

    # Answering still needs the separate explicit continue authorization.
    no_continue = await _decide(
        client, run_id, review, review_result={"decision": "CONTINUE"}
    )
    assert no_continue.status_code == 422
    assert no_continue.json()["error"]["code"] == "ASSISTANCE_DECISION_INVALID"

    accepted = await _decide(
        client,
        run_id,
        review,
        review_result={"decision": "CONTINUE"},
        continue_authorized=True,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["resolved"] is True
    assert await api_context[1].executor.wait(run_id, max_wait=60) == "SUCCEEDED"

    closed = (await client.get(f"/api/v1/assistance/{review['id']}")).json()
    assert closed["status"] == "RESOLVED"
    assert closed["decisions"][-1]["review_result"] == {"decision": "CONTINUE"}
    final = await client.get(f"/api/v1/runs/{run_id}")
    assert final.json()["artifact_ids"], "the reviewed run still delivers its document"

    # A closed request cannot be decided twice, even with the current revision.
    again = await _decide(
        client, run_id, closed, review_result={"decision": "CONTINUE"}, continue_authorized=True
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ASSISTANCE_CLOSED"


async def test_pausing_a_review_closes_it_without_resuming(
    client: httpx.AsyncClient, api_context: Any, workflow_definition: dict[str, Any]
) -> None:
    run_id, review = await _start_review_run(client, api_context, workflow_definition)
    paused = await _decide(client, run_id, review, decision="PAUSE", note="先停一下")
    assert paused.status_code == 200, paused.text
    assert paused.json()["run"]["status"] == "PAUSED"
    closed = (await client.get(f"/api/v1/assistance/{review['id']}")).json()
    assert closed["status"] == "RESOLVED"
    assert closed["decisions"][-1]["decision"] == "PAUSE"
    snapshot = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert snapshot["status"] == "PAUSED"


async def test_reconcile_never_trusts_a_client_claim(
    client: httpx.AsyncClient, api_context: Any, workflow_definition: dict[str, Any]
) -> None:
    run_id, review = await _start_review_run(client, api_context, workflow_definition)
    _, context = api_context
    import hashlib

    from personal_agent.adapters.persistence.artifact_models import SideEffectOperation

    async with context.database.session() as session:
        operation = SideEffectOperation(
            id=new_id(),
            root_run_id=run_id,
            run_id=run_id,
            step_attempt_id=None,
            logical_effect_key="document:review-fixture",
            target_ref="recovery/never-written.md",
            expected_before_hash=None,
            intended_after_hash=hashlib.sha256(b"expected").hexdigest(),
            intent_ref="intent:review-fixture",
            status="INTENT",
            observed_hash=None,
            verified_at=None,
            verification_ref=None,
            revision=1,
            created_at=context.clock.now(),
        )
        session.add(operation)
        write_request = AssistanceRequest(
            id=new_id(),
            run_id=run_id,
            kind="WRITE_RECONCILIATION",
            limit_snapshot={},
            consumed_snapshot={},
            completed_steps=[],
            remaining_steps=[review["completed_steps"][0] if review["completed_steps"] else ""],
            observed_problem="写入结果无法核验",
            unknowns=["INTENT"],
            attempted_remedies=[],
            decision_schema_json=None,
            status="OPEN",
            revision=1,
            created_at=context.clock.now(),
        )
        session.add(write_request)
        await session.commit()
        write_id = write_request.id
        write_revision = write_request.revision

    response = await client.post(
        f"/api/v1/assistance/{write_id}/decisions",
        json={"revision": write_revision, "decision": "RECONCILE", "note": "我确认写入成功"},
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{write_revision}"'},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SIDE_EFFECT_UNRESOLVED"
    # The client claim did not close the request either.
    unchanged = (await client.get(f"/api/v1/assistance/{write_id}")).json()
    assert unchanged["status"] == "OPEN"


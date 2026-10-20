"""Branch proposal, validation, confirmation and write reconciliation (T079/T080/T081).

A proposal never moves its start point by itself. The impact is recomputed from the
real requirement difference, a blocked branch refuses to confirm, and one proposal
creates exactly one run that shares the root's ledger and reuses only validated
results before its start point.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
from sqlalchemy import select

from personal_agent.adapters.persistence.artifact_models import SideEffectOperation
from personal_agent.adapters.persistence.budget_models import BudgetLedger
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


async def _started_run(client: httpx.AsyncClient, api_context: Any) -> tuple[str, str]:
    """Start the topic workflow, let it finish, and return (run_id, version_id)."""
    config, context = api_context
    await seed(context.database)
    registry = WorkflowRegistry(context.database, context.clock)
    summaries = await registry.list_summaries()
    version_id = next(
        item["latest_version"]["id"] for item in summaries if item["name"] == TOPIC_WORKFLOW
    )
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
    assert await context.executor.wait(run_id, max_wait=60) == "SUCCEEDED"
    return run_id, version_id


async def _proposal(
    client: httpx.AsyncClient, run_id: str, step_id: str
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/runs/{run_id}/branch-proposals",
        json={"selected_step_id": step_id, "reason": "改一点要求再跑"},
        headers={"Idempotency-Key": new_id()},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _edit_draft(api_context: Any, draft_id: str, values: dict[str, str]) -> None:
    _, context = api_context
    async with context.database.session() as session:
        draft = await session.get(RequirementDraft, draft_id)
        assert draft is not None
        fields = dict(draft.field_values_json)
        for key, value in values.items():
            fields[key] = {
                "path": key,
                "value": value,
                "source": "user",
                "message_id": None,
                "suggestion_id": None,
                "accepted_at": None,
                "explicit_none": False,
            }
        draft.field_values_json = fields
        draft.revision += 1
        draft.updated_at = context.clock.now()
        await session.commit()


async def _branch_confirmation(
    api_context: Any, draft_id: str, version_id: str
) -> Confirmation:
    config, context = api_context
    async with context.database.session() as session:
        draft = await session.get(RequirementDraft, draft_id)
        assert draft is not None
        draft.status = "CONFIRMED"
        snapshot = {
            "fields": dict(draft.field_values_json),
            "preference_order": list(draft.preference_order_json),
            "output_spec": dict(draft.output_spec_json),
        }
        confirmation = Confirmation(
            id=new_id(),
            draft_id=draft.id,
            draft_revision=draft.revision,
            workflow_version_id=version_id,
            summary_hash="sha256:branch-summary",
            input_hash=stable_hash(snapshot),
            budget_policy_hash=config.policy.policy_hash,
            data_policy_version=config.policy.version,
            confirmed_at=context.clock.now(),
            action="BRANCH",
            field_snapshot_json=snapshot,
        )
        session.add(confirmation)
        await session.commit()
        return confirmation


async def _validate(
    client: httpx.AsyncClient, proposal_id: str, step_id: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if step_id is not None:
        body["selected_step_id"] = step_id
    response = await client.post(
        f"/api/v1/branch-proposals/{proposal_id}/validate",
        json=body,
        headers={"Idempotency-Key": new_id()},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_proposal_never_moves_its_start_point(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    run_id, version_id = await _started_run(client, api_context)
    proposal = await _proposal(client, run_id, "merge_findings")
    assert proposal["status"] == "DRAFT"
    assert proposal["draft_id"]

    unchanged = await _validate(client, proposal["id"])
    assert unchanged["proposal"]["status"] == "BLOCKED"
    assert unchanged["proposal"]["changed_paths"] == []
    assert unchanged["analysis"]["blocked_reasons"]

    # Changing the topic makes an earlier collecting step re-run; starting at
    # merge_findings is therefore blocked and only suggests an earlier point.
    await _edit_draft(api_context, proposal["draft_id"], {"topic": "城市夜间公交"})
    changed = await _validate(client, proposal["id"])
    assert changed["proposal"]["status"] == "BLOCKED"
    assert "/topic" in changed["analysis"]["changed_paths"]
    assert changed["analysis"]["suggested_start_step_id"]
    assert changed["analysis"]["suggested_start_step_id"] != "merge_findings"
    assert changed["proposal"]["selected_step_id"] == "merge_findings"

    confirmation = await _branch_confirmation(api_context, proposal["draft_id"], version_id)
    refused = await client.post(
        f"/api/v1/branch-proposals/{proposal['id']}/confirm",
        json={
            "confirmation_id": confirmation.id,
            "effective_step_id": "merge_findings",
            "report_hash": changed["proposal"]["report_hash"],
            "explicit_start_confirmation": True,
        },
        headers={"Idempotency-Key": new_id()},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "PREREQUISITE_INVALID"

    # A different start point than the validated one also needs its own validation.
    moved = await client.post(
        f"/api/v1/branch-proposals/{proposal['id']}/confirm",
        json={
            "confirmation_id": confirmation.id,
            "effective_step_id": "collect_background",
            "report_hash": changed["proposal"]["report_hash"],
            "explicit_start_confirmation": True,
        },
        headers={"Idempotency-Key": new_id()},
    )
    assert moved.status_code == 409
    assert moved.json()["error"]["code"] == "START_POINT_REQUIRES_CONFIRMATION"


async def test_confirm_creates_one_run_that_reuses_validated_results(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    run_id, version_id = await _started_run(client, api_context)
    proposal = await _proposal(client, run_id, "write_report")
    # A new field that no step reads leaves every earlier result reusable.
    await _edit_draft(api_context, proposal["draft_id"], {"audience": "夜间读者"})
    validated = await _validate(client, proposal["id"])
    assert validated["proposal"]["status"] == "READY", validated["proposal"]
    reusable = set(validated["analysis"]["reusable_step_ids"])
    assert {"collect_background", "collect_latest", "merge_findings"} <= reusable
    assert "write_report" not in reusable

    confirmation = await _branch_confirmation(api_context, proposal["draft_id"], version_id)
    confirmed = await client.post(
        f"/api/v1/branch-proposals/{proposal['id']}/confirm",
        json={
            "confirmation_id": confirmation.id,
            "effective_step_id": "write_report",
            "report_hash": validated["proposal"]["report_hash"],
            "explicit_start_confirmation": True,
        },
        headers={"Idempotency-Key": new_id()},
    )
    assert confirmed.status_code == 201, confirmed.text
    branch = confirmed.json()
    assert branch["parent_run_id"] == run_id
    assert branch["root_run_id"] == run_id
    assert branch["fork_step_id"] == "write_report"
    branch_id = branch["id"]

    _, context = api_context
    async with context.database.session() as session:
        parent = await session.get(WorkflowRun, run_id)
        child = await session.get(WorkflowRun, branch_id)
        assert parent is not None and child is not None
        assert child.created_at >= parent.created_at
        # A branch shares the root ledger; it must not get a fresh budget.
        ledgers = (
            await session.scalars(
                select(BudgetLedger).where(BudgetLedger.root_run_id == run_id)
            )
        ).all()
        assert len(ledgers) == 1
        # Reused facts are copied as history, not as new attempts of the branch.
        branch_steps = {
            row.step_id: row
            for row in (
                await session.scalars(select(StepRun).where(StepRun.run_id == branch_id))
            ).all()
        }
        assert branch_steps["merge_findings"].status == "SUCCEEDED"
        assert branch_steps["merge_findings"].skip_reason == "reused from parent"
        attempts = (
            await session.scalars(
                select(StepAttempt)
                .join(StepRun, StepAttempt.step_run_id == StepRun.id)
                .where(StepRun.run_id == branch_id)
            )
        ).all()
        attempted_steps = {row.step_run_id for row in attempts}
        assert branch_steps["merge_findings"].id not in attempted_steps

    # Exactly one run per proposal: a repeated confirm returns the same run.
    again = await client.post(
        f"/api/v1/branch-proposals/{proposal['id']}/confirm",
        json={
            "confirmation_id": confirmation.id,
            "effective_step_id": "write_report",
            "report_hash": validated["proposal"]["report_hash"],
            "explicit_start_confirmation": True,
        },
        headers={"Idempotency-Key": new_id()},
    )
    assert again.status_code in {200, 201}
    assert again.json()["id"] == branch_id

    assert await context.executor.wait(branch_id, max_wait=60) == "SUCCEEDED"
    final = (await client.get(f"/api/v1/runs/{branch_id}")).json()
    assert final["artifact_ids"]


async def test_side_effect_reconciliation_is_verified_by_the_server(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    run_id, _version_id = await _started_run(client, api_context)
    _, context = api_context
    async with context.database.session() as session:
        operation = SideEffectOperation(
            id=new_id(),
            root_run_id=run_id,
            run_id=run_id,
            step_attempt_id=None,
            logical_effect_key="document:reconcile-fixture",
            target_ref="recovery/never-written.md",
            expected_before_hash=None,
            intended_after_hash=hashlib.sha256(b"expected document").hexdigest(),
            intent_ref="intent:reconcile-fixture",
            status="INTENT",
            observed_hash=None,
            verified_at=None,
            verification_ref=None,
            revision=1,
            created_at=context.clock.now(),
        )
        session.add(operation)
        await session.commit()
        operation_id = operation.id

    listing = (await client.get(f"/api/v1/runs/{run_id}/side-effects")).json()
    assert any(item["id"] == operation_id for item in listing["items"])

    reconciled = await client.post(
        f"/api/v1/runs/{run_id}/side-effects/{operation_id}/reconcile",
        json={"evidence_refs": [], "decision_note": "我确认写入了"},
        headers={"Idempotency-Key": new_id()},
    )
    assert reconciled.status_code == 200, reconciled.text
    body = reconciled.json()
    assert body["status"] == "UNKNOWN"
    assert body["resolved"] is False
    assert body["assistance_id"], "an unresolved write must offer a next action"

    assistance = (await client.get(f"/api/v1/runs/{run_id}/assistance")).json()["items"]
    write_request = next(item for item in assistance if item["id"] == body["assistance_id"])
    assert write_request["kind"] == "WRITE_RECONCILIATION"
    assert write_request["status"] == "OPEN"
    assert "RECONCILE" in write_request["available_decisions"]

    # The client claim still cannot close it while the target stays unverifiable.
    decision = await client.post(
        f"/api/v1/runs/{run_id}/assistance/{write_request['id']}/decisions",
        json={
            "revision": write_request["revision"],
            "decision": "RECONCILE",
            "note": "我确认写入了",
        },
        headers={
            "Idempotency-Key": new_id(),
            "If-Match": f'"{write_request["revision"]}"',
        },
    )
    assert decision.status_code == 409
    assert decision.json()["error"]["code"] == "SIDE_EFFECT_UNRESOLVED"


async def test_unknown_proposal_and_start_step_are_rejected(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    run_id, _version_id = await _started_run(client, api_context)
    unknown = await client.get("/api/v1/branch-proposals/00000000-0000-4000-8000-000000000000")
    assert unknown.status_code == 404

    bad_step = await client.post(
        f"/api/v1/runs/{run_id}/branch-proposals",
        json={"selected_step_id": "not_a_step"},
        headers={"Idempotency-Key": new_id()},
    )
    assert bad_step.status_code == 201
    rejected = await client.post(
        f"/api/v1/branch-proposals/{bad_step.json()['id']}/validate",
        json={},
        headers={"Idempotency-Key": new_id()},
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "START_POINT_INVALID"

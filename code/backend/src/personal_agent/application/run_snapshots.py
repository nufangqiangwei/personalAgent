"""Read models for run snapshots and step views."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.adapters.persistence.artifact_models import Artifact
from personal_agent.adapters.persistence.assistance_models import AssistanceRequest
from personal_agent.adapters.persistence.budget_models import BudgetLedger
from personal_agent.adapters.persistence.execution_models import (
    ExecutionEvent,
    StepAttempt,
    StepRun,
    WorkflowRun,
)
from personal_agent.domain.common import iso_z


async def serialize_run(session: AsyncSession, run: WorkflowRun) -> dict[str, Any]:
    ledger = await session.scalar(
        select(BudgetLedger).where(BudgetLedger.root_run_id == run.root_run_id)
    )
    budget = {
        "limits": dict(ledger.limits_json) if ledger else {},
        "consumed": dict(ledger.consumed_json) if ledger else {},
        "reserved": dict(ledger.reserved_json) if ledger else {},
    }
    assistance_ids = list(
        (
            await session.scalars(
                select(AssistanceRequest.id)
                .where(AssistanceRequest.run_id == run.id)
                .order_by(AssistanceRequest.created_at)
            )
        ).all()
    )
    artifact_ids = list(
        (
            await session.scalars(
                select(Artifact.id).where(Artifact.run_id == run.id).order_by(Artifact.created_at)
            )
        ).all()
    )
    last_sequence = await session.scalar(
        select(func.max(ExecutionEvent.sequence)).where(ExecutionEvent.run_id == run.id)
    )
    return {
        "id": run.id,
        "root_run_id": run.root_run_id,
        "parent_run_id": run.parent_run_id,
        "fork_step_id": run.fork_step_id,
        "workflow_version_id": run.workflow_version_id,
        "status": run.status,
        "reason_code": run.reason_code,
        "revision": run.revision,
        "epoch": run.epoch,
        "current_step_id": run.current_step_id,
        "confirmed_input_hash": run.input_hash,
        "last_checkpoint_hash": None,
        "budget": budget,
        "draining": False,
        "in_flight_actions": [],
        "assistance_ids": assistance_ids,
        "completeness": run.completeness,
        "artifact_ids": artifact_ids,
        "last_event_sequence": int(last_sequence or 0),
        "allowed_actions": await _allowed_actions(session, run),
        "created_at": iso_z(run.created_at),
        "updated_at": iso_z(run.updated_at),
    }


async def _allowed_actions(session: AsyncSession, run: WorkflowRun) -> list[dict[str, Any]]:
    from personal_agent.domain.execution import RunStatus

    actions: list[dict[str, Any]] = []
    if run.status in {RunStatus.RUNNING.value, RunStatus.PAUSING.value}:
        actions.append(
            {"action": "PAUSE", "enabled": False, "disabled_reason": "执行控制将在 US9 提供"}
        )
        actions.append(
            {"action": "CANCEL", "enabled": False, "disabled_reason": "执行控制将在 US9 提供"}
        )
    elif run.status == RunStatus.WAITING_USER.value:
        actions.append(
            {"action": "VIEW_ASSISTANCE", "enabled": True, "disabled_reason": None}
        )
    return actions


async def serialize_steps(session: AsyncSession, run_id: str) -> dict[str, Any]:
    steps = (
        await session.scalars(
            select(StepRun).where(StepRun.run_id == run_id).order_by(StepRun.created_at)
        )
    ).all()
    items: list[dict[str, Any]] = []
    for step in steps:
        attempts = (
            await session.scalars(
                select(StepAttempt)
                .where(StepAttempt.step_run_id == step.id)
                .order_by(StepAttempt.attempt_no)
            )
        ).all()
        items.append(
            {
                "step_id": step.step_id,
                "status": step.status,
                "current_attempt_no": step.current_attempt_no,
                "selected_transition": step.selected_transition,
                "skip_reason": step.skip_reason,
                "attempts": [
                    {
                        "attempt_no": attempt.attempt_no,
                        "epoch": attempt.epoch,
                        "status": attempt.status,
                        "started_at": iso_z(attempt.started_at),
                        "ended_at": iso_z(attempt.ended_at) if attempt.ended_at else None,
                        "error_category": attempt.error_category,
                        "output": attempt.output_json,
                    }
                    for attempt in attempts
                ],
            }
        )
    return {"run_id": run_id, "items": items}

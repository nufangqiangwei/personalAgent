"""Branch proposals, side-effect reconciliation and the unified control surface.

A proposal only records a candidate start point. Validating recomputes the impact from
the real requirement difference; confirming needs a ``BRANCH`` confirmation, the final
start point and the matching report hash, and creates exactly one run. Write
reconciliation always re-reads the target on the server: a client cannot declare a
write as applied.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from personal_agent.adapters.persistence.artifact_models import SideEffectOperation
from personal_agent.adapters.persistence.assistance_models import AssistanceRequest
from personal_agent.adapters.persistence.execution_models import WorkflowRun
from personal_agent.api.deps import context, revision_etag
from personal_agent.domain.assistance import AssistanceKind
from personal_agent.domain.common import AppError, iso_z, new_id

router = APIRouter(prefix="/api/v1", tags=["branches"])

RESOLVED_SIDE_EFFECTS = {"APPLIED", "NOT_APPLIED"}


class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_step_id: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=2000)
    changed_paths: list[str] = Field(default_factory=list)


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_step_id: str | None = Field(default=None, max_length=64)
    draft_revision: int | None = None


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_id: str = Field(min_length=1, max_length=36)
    effective_step_id: str = Field(min_length=1, max_length=64)
    report_hash: str = Field(min_length=1, max_length=80)
    explicit_start_confirmation: bool = False


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_refs: list[str] = Field(default_factory=list)
    decision_note: str | None = Field(default=None, max_length=2000)


@router.post("/runs/{run_id}/branch-proposals", status_code=201)
async def create_proposal(
    run_id: str, payload: ProposalRequest, request: Request
) -> dict[str, Any]:
    app_context = context(request)
    return await app_context.branches.create_proposal(
        run_id,
        selected_step_id=payload.selected_step_id,
        reason=payload.reason,
        changed_paths=payload.changed_paths,
    )


@router.get("/branch-proposals/{proposal_id}")
async def get_proposal(proposal_id: str, request: Request) -> dict[str, Any]:
    return await context(request).branches.get(proposal_id)


@router.post("/branch-proposals/{proposal_id}/validate")
async def validate_proposal(
    proposal_id: str, payload: ValidateRequest, request: Request
) -> dict[str, Any]:
    return await context(request).branches.validate(
        proposal_id, selected_step_id=payload.selected_step_id
    )


@router.post("/branch-proposals/{proposal_id}/confirm", status_code=201)
async def confirm_proposal(
    proposal_id: str,
    payload: ConfirmRequest,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    snapshot = await context(request).branches.confirm(
        proposal_id,
        confirmation_id=payload.confirmation_id,
        effective_step_id=payload.effective_step_id,
        report_hash=payload.report_hash,
        explicit_start_confirmation=payload.explicit_start_confirmation,
    )
    response.headers.update(revision_etag(int(snapshot["revision"])))
    return snapshot


# ----------------------------------------------------------------------
@router.get("/runs/{run_id}/side-effects")
async def list_side_effects(run_id: str, request: Request) -> dict[str, Any]:
    app_context = context(request)
    async with app_context.database.session() as session:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            raise AppError(code="RUN_NOT_FOUND", message="run not found", status=404)
        operations = (
            await session.scalars(
                select(SideEffectOperation)
                .where(SideEffectOperation.root_run_id == run.root_run_id)
                .order_by(SideEffectOperation.created_at)
            )
        ).all()
        return {
            "run_id": run_id,
            "root_run_id": run.root_run_id,
            "items": [
                {
                    "id": operation.id,
                    "run_id": operation.run_id,
                    "logical_effect_key": operation.logical_effect_key,
                    "target_ref": operation.target_ref,
                    "status": operation.status,
                    "intended_after_hash": operation.intended_after_hash,
                    "observed_hash": operation.observed_hash,
                    "verified_at": iso_z(operation.verified_at)
                    if operation.verified_at
                    else None,
                }
                for operation in operations
            ],
        }


@router.post("/runs/{run_id}/side-effects/{operation_id}/reconcile")
async def reconcile_side_effect(
    run_id: str,
    operation_id: str,
    payload: ReconcileRequest,
    request: Request,
) -> dict[str, Any]:
    app_context = context(request)
    async with app_context.database.session() as session:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            raise AppError(code="RUN_NOT_FOUND", message="run not found", status=404)
        operation = await session.get(SideEffectOperation, operation_id)
        if operation is None or operation.root_run_id != run.root_run_id:
            raise AppError(
                code="SIDE_EFFECT_NOT_FOUND",
                message="side effect operation not found for this root",
                status=404,
            )
    status = await app_context.writer.reconcile(operation_id)
    resolved = status.value in RESOLVED_SIDE_EFFECTS
    assistance_id = None
    if not resolved:
        assistance_id = await _ensure_write_assistance(app_context, run_id, operation_id)
    return {
        "operation_id": operation_id,
        "status": status.value,
        "resolved": resolved,
        "assistance_id": assistance_id,
        "evidence_refs": payload.evidence_refs,
        "decision_note": payload.decision_note,
    }


async def _ensure_write_assistance(app_context: Any, run_id: str, operation_id: str) -> str:
    """Open (or reuse) a write-reconciliation request so the UI has a next action."""
    async with app_context.database.session() as session:
        existing = await session.scalar(
            select(AssistanceRequest)
            .where(
                AssistanceRequest.run_id == run_id,
                AssistanceRequest.kind == AssistanceKind.WRITE_RECONCILIATION.value,
                AssistanceRequest.status == "OPEN",
            )
            .order_by(AssistanceRequest.created_at.desc())
        )
        if existing is not None:
            return str(existing.id)
        request = AssistanceRequest(
            id=new_id(),
            run_id=run_id,
            kind=AssistanceKind.WRITE_RECONCILIATION.value,
            limit_snapshot={},
            consumed_snapshot={},
            completed_steps=[],
            remaining_steps=[],
            observed_problem="文件写入结果无法确认，需要人工核验目标",
            unknowns=["SIDE_EFFECT_UNRESOLVED"],
            attempted_remedies=[f"reconcile:{operation_id}"],
            decision_schema_json=None,
            status="OPEN",
            revision=1,
            created_at=app_context.clock.now(),
        )
        session.add(request)
        await session.commit()
        return request.id


__all__ = ["router"]

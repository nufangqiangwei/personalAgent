"""Assistance: the only way out of ``WAITING_USER``.

An open request carries what the engine actually observed (limits, consumed,
completed and remaining steps, the problem, the unknowns, what was already tried).
Chat discussion is recorded but never unblocks the run. "Top up" and "continue" are
separate explicit fields, and a client cannot declare a write as applied — the server
re-reads the target itself.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from personal_agent.adapters.persistence.assistance_models import (
    AssistanceRequest,
    UserDecision,
)
from personal_agent.adapters.persistence.budget_models import BudgetLedger
from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.execution_models import WorkflowRun
from personal_agent.application.budget_service import BudgetService
from personal_agent.application.recovery import RecoveryService
from personal_agent.application.stop_control import StopControl
from personal_agent.domain.assistance import AssistanceStatus, UserDecisionType
from personal_agent.domain.common import AppError, ErrorCategory, new_id
from personal_agent.ports.contracts import Clock

ASSISTANCE_DECISION_INVALID = "ASSISTANCE_DECISION_INVALID"


class AssistanceService:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        budget: BudgetService,
        control: StopControl,
        recovery: RecoveryService,
        writer: Any,
    ) -> None:
        self._database = database
        self._clock = clock
        self._budget = budget
        self._control = control
        self._recovery = recovery
        self._writer = writer

    # ------------------------------------------------------------------
    async def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            run = await session.get(WorkflowRun, run_id)
            if run is None:
                raise AppError(code="RUN_NOT_FOUND", message="run not found", status=404)
            rows = (
                await session.scalars(
                    select(AssistanceRequest)
                    .where(AssistanceRequest.run_id == run_id)
                    .order_by(AssistanceRequest.created_at)
                )
            ).all()
            return [await self._serialize(session, row) for row in rows]

    async def get(self, assistance_id: str) -> dict[str, Any]:
        async with self._database.session() as session:
            row = await self._require(session, assistance_id)
            return await self._serialize(session, row)

    # ------------------------------------------------------------------
    async def decide(
        self,
        assistance_id: str,
        *,
        expected_revision: int,
        decision: str,
        approved_increment: dict[str, int] | None = None,
        continue_authorized: bool = False,
        acknowledge_cost: bool = False,
        note: str | None = None,
        evidence_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            choice = UserDecisionType(decision)
        except ValueError as error:
            raise AppError(
                code=ASSISTANCE_DECISION_INVALID,
                message=f"unknown decision {decision!r}",
                status=422,
            ) from error

        async with self._database.session() as session:
            request = await self._require(session, assistance_id)
            if request.revision != expected_revision:
                raise AppError(
                    code="ASSISTANCE_REVISION_STALE",
                    message="the assistance request changed since it was read",
                    category=ErrorCategory.VALIDATION,
                    status=412,
                )
            if request.status != AssistanceStatus.OPEN.value:
                raise AppError(
                    code="ASSISTANCE_CLOSED",
                    message="this assistance request is already closed",
                    status=409,
                    details={"status": request.status},
                )
            run = await session.get(WorkflowRun, request.run_id)
            if run is None:
                raise AppError(code="RUN_NOT_FOUND", message="run not found", status=404)
            run_id = run.id
            root_run_id = run.root_run_id

            increment = dict(approved_increment or {})
            if choice is UserDecisionType.GRANT_AND_CONTINUE:
                if not increment or not continue_authorized or not acknowledge_cost:
                    raise AppError(
                        code=ASSISTANCE_DECISION_INVALID,
                        message=(
                            "continuing needs an explicit top-up, an explicit continue "
                            "authorization and an acknowledgement of the cost"
                        ),
                        status=422,
                        details={
                            "approved_increment": increment,
                            "continue_authorized": continue_authorized,
                            "acknowledge_cost": acknowledge_cost,
                        },
                    )
                ledger = await session.scalar(
                    select(BudgetLedger).where(BudgetLedger.root_run_id == root_run_id)
                )
                if ledger is None:
                    raise AppError(
                        code="BUDGET_LEDGER_MISSING",
                        message="the run has no budget ledger",
                        status=503,
                    )
                await self._budget.grant_run_budget(
                    session,
                    ledger=ledger,
                    increment=increment,
                    operation_id=f"grant:{request.id}",
                    note=note,
                )
                await self._resolve(session, request)
                session.add(
                    self._decision(
                        request,
                        choice,
                        increment=increment,
                        continue_authorized=continue_authorized,
                        evidence_refs=evidence_refs,
                        note=note,
                    )
                )
                run.revision += 1
                await session.commit()
            elif choice is UserDecisionType.RECONCILE:
                # Never trust a client claim: re-read every unresolved target first.
                verified = await self._reconcile_root(root_run_id)
                if not verified:
                    raise AppError(
                        code="SIDE_EFFECT_UNRESOLVED",
                        message=(
                            "the server could not verify the write; the run stays blocked "
                            "until it is reconciled"
                        ),
                        status=409,
                    )
                await self._resolve(session, request)
                session.add(
                    self._decision(
                        request,
                        choice,
                        increment=None,
                        continue_authorized=True,
                        evidence_refs=evidence_refs,
                        note=note,
                    )
                )
                await session.commit()
            elif choice is UserDecisionType.CANCEL:
                await self._resolve(session, request)
                session.add(
                    self._decision(
                        request,
                        choice,
                        increment=None,
                        continue_authorized=False,
                        evidence_refs=evidence_refs,
                        note=note,
                    )
                )
                await session.commit()
                snapshot = await self._control.request_cancel(
                    run_id,
                    expected_revision=run.revision,
                    reason=note or "用户通过求助处置取消",
                    confirmed=True,
                )
                return {
                    "assistance_id": request.id,
                    "decision": choice.value,
                    "resolved": True,
                    "run": snapshot,
                }
            elif choice is UserDecisionType.CHANGE_REQUIREMENTS:
                # Changing requirements is a branch, not a resume of this run.
                session.add(
                    self._decision(
                        request,
                        choice,
                        increment=None,
                        continue_authorized=False,
                        evidence_refs=evidence_refs,
                        note=note,
                    )
                )
                await session.commit()
                return {
                    "assistance_id": request.id,
                    "decision": choice.value,
                    "resolved": False,
                    "branch_required": True,
                    "run_id": run_id,
                }
            else:  # DISCUSS or PAUSE: recorded, still blocked
                session.add(
                    self._decision(
                        request,
                        choice,
                        increment=None,
                        continue_authorized=False,
                        evidence_refs=evidence_refs,
                        note=note,
                    )
                )
                await session.commit()
                return {
                    "assistance_id": request.id,
                    "decision": choice.value,
                    "resolved": False,
                    "blocked": True,
                    "message": "讨论已记录；执行仍保持等待，不会自动继续。",
                }

        # The run may be resumed now that the request is resolved.
        current = await self._run_snapshot(run_id)
        resumed = await self._recovery.resume(
            run_id, expected_revision=int(current["revision"])
        )
        return {
            "assistance_id": assistance_id,
            "decision": choice.value,
            "resolved": True,
            "run": resumed,
        }

    # ------------------------------------------------------------------
    async def _reconcile_root(self, root_run_id: str) -> bool:
        from personal_agent.adapters.persistence.artifact_models import SideEffectOperation

        async with self._database.session() as session:
            operations = (
                await session.scalars(
                    select(SideEffectOperation).where(
                        SideEffectOperation.root_run_id == root_run_id,
                        SideEffectOperation.status.in_(
                            ["INTENT", "UNKNOWN", "PARTIAL", "CONFLICT"]
                        ),
                    )
                )
            ).all()
            operation_ids = [operation.id for operation in operations]
        for operation_id in operation_ids:
            await self._writer.reconcile(operation_id)
        if not operation_ids:
            return True
        async with self._database.session() as session:
            remaining = (
                await session.scalars(
                    select(SideEffectOperation).where(
                        SideEffectOperation.root_run_id == root_run_id,
                        SideEffectOperation.status.in_(
                            ["INTENT", "UNKNOWN", "PARTIAL", "CONFLICT"]
                        ),
                    )
                )
            ).all()
        return not remaining

    async def _run_snapshot(self, run_id: str) -> dict[str, Any]:
        from personal_agent.application.run_snapshots import serialize_run

        async with self._database.session() as session:
            run = await session.get(WorkflowRun, run_id)
            assert run is not None
            return await serialize_run(session, run)

    async def _resolve(self, session: Any, request: AssistanceRequest) -> None:
        request.status = AssistanceStatus.RESOLVED.value
        request.revision += 1

    def _decision(
        self,
        request: AssistanceRequest,
        choice: UserDecisionType,
        *,
        increment: dict[str, int] | None,
        continue_authorized: bool,
        evidence_refs: list[str] | None,
        note: str | None,
    ) -> UserDecision:
        return UserDecision(
            id=new_id(),
            assistance_id=request.id,
            decision=choice.value,
            approved_increment=increment,
            continue_authorized=continue_authorized,
            evidence_refs=list(evidence_refs or []),
            note=note,
            created_at=self._clock.now(),
            revision=1,
        )

    async def _serialize(self, session: Any, row: AssistanceRequest) -> dict[str, Any]:
        decisions = (
            await session.scalars(
                select(UserDecision)
                .where(UserDecision.assistance_id == row.id)
                .order_by(UserDecision.created_at)
            )
        ).all()
        return {
            "id": row.id,
            "run_id": row.run_id,
            "kind": row.kind,
            "status": row.status,
            "revision": row.revision,
            "limit": dict(row.limit_snapshot),
            "consumed": dict(row.consumed_snapshot),
            "completed_steps": list(row.completed_steps),
            "remaining_steps": list(row.remaining_steps),
            "observed_problem": row.observed_problem,
            "unknowns": list(row.unknowns),
            "attempted_remedies": list(row.attempted_remedies),
            "created_at": row.created_at.isoformat().replace("+00:00", "Z"),
            "decisions": [
                {
                    "decision": decision.decision,
                    "continue_authorized": decision.continue_authorized,
                    "approved_increment": decision.approved_increment,
                    "evidence_refs": list(decision.evidence_refs),
                    "note": decision.note,
                    "created_at": decision.created_at.isoformat().replace("+00:00", "Z"),
                }
                for decision in decisions
            ],
        }

    @staticmethod
    async def _require(session: Any, assistance_id: str) -> AssistanceRequest:
        row = await session.get(AssistanceRequest, assistance_id)
        if row is None:
            raise AppError(
                code="ASSISTANCE_NOT_FOUND",
                message="assistance request not found",
                status=404,
            )
        return row

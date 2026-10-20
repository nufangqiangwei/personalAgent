"""Execution coordinator: confirmation consumption, single slot and run creation.

Exactly one non-terminal run may exist. Starting a run consumes a confirmed
snapshot, acquires the singleton execution slot with a conditional update, and
refuses to start while a model request is unresolved. Nothing here advances step
state; the engine owns that.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError

from personal_agent.adapters.harness.model_gateway import ModelGateway
from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.execution_models import (
    ExecutionEvent,
    ExecutionSlot,
    WorkflowRun,
)
from personal_agent.adapters.persistence.requirement_models import (
    Confirmation,
    RequirementDraft,
)
from personal_agent.adapters.persistence.workflow_models import Workflow, WorkflowVersion
from personal_agent.application.budget_service import BudgetService
from personal_agent.application.run_snapshots import serialize_run
from personal_agent.domain.assistance import AssistanceKind  # noqa: F401  (typing aid)
from personal_agent.domain.common import (
    MODEL_BUSY,
    MODEL_OUTCOME_UNKNOWN,
    RUN_BUSY,
    AppError,
    new_id,
    stable_hash,
)
from personal_agent.domain.execution import EventKind, RunStatus
from personal_agent.ports.contracts import Clock


class ExecutionCoordinator:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        gateway: ModelGateway,
        budget: BudgetService,
        *,
        policy_hash: str,
        policy_version: str,
        budget_limits: dict[str, int],
        runtime_fingerprint: str,
        config_fingerprint: str,
    ) -> None:
        self._database = database
        self._clock = clock
        self._gateway = gateway
        self._budget = budget
        self._policy_hash = policy_hash
        self._policy_version = policy_version
        self._budget_limits = budget_limits
        self._runtime_fingerprint = runtime_fingerprint
        self._config_fingerprint = config_fingerprint

    async def start(self, confirmation_id: str) -> dict[str, Any]:
        async with self._database.session() as session:
            existing = await session.scalar(
                select(WorkflowRun).where(WorkflowRun.confirmation_id == confirmation_id)
            )
            if existing is not None:
                return await serialize_run(session, existing)

            confirmation = await session.get(Confirmation, confirmation_id)
            if confirmation is None:
                raise AppError(
                    code="CONFIRMATION_NOT_FOUND",
                    message="confirmation not found",
                    status=404,
                )
            if confirmation.action != "START":
                raise AppError(
                    code="CONFIRMATION_INVALID",
                    message="only a START confirmation can create a run",
                    status=409,
                )
            draft = await session.get(RequirementDraft, confirmation.draft_id)
            if draft is None or draft.status != "CONFIRMED":
                raise AppError(
                    code="CONFIRMATION_STALE",
                    message="the underlying draft is not confirmed",
                    status=409,
                )
            version = await session.get(WorkflowVersion, confirmation.workflow_version_id)
            if version is None or not version.executable:
                raise AppError(
                    code="VERSION_CHANGED",
                    message="the confirmed workflow version is no longer executable",
                    status=409,
                )
            workflow = await session.get(Workflow, version.workflow_id)
            if workflow is None or workflow.disabled_at is not None:
                raise AppError(
                    code="WORKFLOW_DISABLED",
                    message="the workflow is disabled",
                    status=409,
                )

            pending = await self._gateway.non_terminal_request()
            if pending is not None:
                from personal_agent.domain.execution import ModelRequestStatus

                if pending.status == ModelRequestStatus.UNKNOWN.value:
                    raise AppError(
                        code=MODEL_OUTCOME_UNKNOWN,
                        message="an unresolved model request blocks new execution",
                        status=409,
                        details={"request_id": pending.id},
                    )
                raise AppError(
                    code=MODEL_BUSY,
                    message="a model request is still in flight",
                    status=409,
                    details={"request_id": pending.id},
                )

            run_id = new_id()
            slot_before = await session.get(ExecutionSlot, 1)
            # Read the slot facts *before* any rollback: a rollback expires the ORM
            # instance, and touching it afterwards would need IO outside the async
            # greenlet (a real failure under two concurrent starts).
            slot_present = slot_before is not None
            slot_run_id = slot_before.run_id if slot_before else None
            acquired = cast(CursorResult[Any], await session.execute(
                update(ExecutionSlot)
                .where(ExecutionSlot.singleton_id == 1, ExecutionSlot.run_id.is_(None))
                .values(
                    run_id=run_id,
                    epoch=ExecutionSlot.epoch + 1,
                    draining=False,
                    process_instance_id="local",
                    revision=ExecutionSlot.revision + 1,
                )
                .execution_options(synchronize_session=False)
            ))
            if acquired.rowcount != 1:  # type: ignore[attr-defined]
                await session.rollback()
                # A double click is not a second task: if the winning request has
                # already created the run for this confirmation, return it.
                existing_snapshot = await self._wait_for_confirmation(confirmation_id)
                if existing_snapshot is not None:
                    return existing_snapshot
                raise AppError(
                    code=RUN_BUSY,
                    message="another execution already holds the single slot",
                    status=409,
                    details={
                        "rowcount": acquired.rowcount,
                        "slot_present": slot_present,
                        "slot_run_id": slot_run_id,
                    },
                )

            now = self._clock.now()
            run = WorkflowRun(
                id=run_id,
                root_run_id=run_id,
                parent_run_id=None,
                fork_step_id=None,
                workflow_version_id=version.id,
                confirmation_id=confirmation.id,
                input_snapshot_json=dict(confirmation.field_snapshot_json),
                input_hash=confirmation.input_hash,
                config_fingerprint=self._config_fingerprint,
                runtime_fingerprint=self._runtime_fingerprint,
                budget_policy_hash=confirmation.budget_policy_hash,
                data_policy_version=confirmation.data_policy_version,
                status=RunStatus.RUNNING.value,
                reason_code=None,
                current_step_id=None,
                epoch=1,
                revision=1,
                mode="FIXED",
                completeness=None,
                active_elapsed_ms=0,
                cancel_requested=False,
                created_at=now,
                updated_at=now,
            )
            session.add(run)
            await self._budget.ensure_run_ledger(
                session, root_run_id=run_id, limits=self._budget_limits
            )
            session.add(
                ExecutionEvent(
                    id=new_id(),
                    run_id=run_id,
                    draft_id=None,
                    sequence=1,
                    step_run_id=None,
                    attempt_id=None,
                    workflow_version_id=version.id,
                    occurred_at=now,
                    kind=EventKind.RUN_STATE_CHANGED.value,
                    status=run.status,
                    reason_code=None,
                    payload_json={"auto_started": False, "confirmation_id": confirmation.id},
                    payload_ref=None,
                    schema_version=1,
                    provider_event_key=None,
                )
            )
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                async with self._database.session() as retry:
                    existing_run = await retry.scalar(
                        select(WorkflowRun).where(
                            WorkflowRun.confirmation_id == confirmation_id
                        )
                    )
                    if existing_run is not None:
                        return await serialize_run(retry, existing_run)
                raise AppError(
                    code="RUN_CONFLICT",
                    message="the confirmation was consumed concurrently",
                    status=409,
                ) from error
            await session.refresh(run)
            return await serialize_run(session, run)

    @staticmethod
    def fingerprint(*parts: object) -> str:
        return stable_hash({"parts": [str(part) for part in parts]})

    async def _wait_for_confirmation(
        self, confirmation_id: str, *, timeout_seconds: float = 2.0
    ) -> dict[str, Any] | None:
        """Give the competing request a moment to commit the same confirmation."""
        import asyncio

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            existing = await self._run_for_confirmation(confirmation_id)
            if existing is not None:
                return existing
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(0.02)

    async def _run_for_confirmation(self, confirmation_id: str) -> dict[str, Any] | None:
        async with self._database.session() as session:
            run = await session.scalar(
                select(WorkflowRun).where(WorkflowRun.confirmation_id == confirmation_id)
            )
            if run is None:
                return None
            return await serialize_run(session, run)

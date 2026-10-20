"""Recovery: diagnose an interrupted run and resume it explicitly.

Startup only diagnoses. Resuming is an explicit user action that re-verifies the
frozen input, the definition, the runtime/profile fingerprint, the whole root's
writes and every stored artifact hash. An unresolved model outcome or write is never
unlocked automatically; the run keeps waiting for assistance.

A resume appends a new epoch to the *same* run, so the run id and history stay
stable. Succeeded steps are never re-executed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from sqlalchemy import select

from personal_agent.adapters.harness.model_gateway import ModelGateway
from personal_agent.adapters.persistence.artifact_models import (
    Artifact,
    Blob,
    SideEffectOperation,
)
from personal_agent.adapters.persistence.blob_store import BlobStore
from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.execution_models import (
    ExecutionEvent,
    ExecutionSlot,
    StepAttempt,
    StepRun,
    WorkflowRun,
)
from personal_agent.adapters.persistence.uow import cas_update
from personal_agent.adapters.persistence.workflow_models import WorkflowVersion
from personal_agent.application.event_log import event_session
from personal_agent.application.run_snapshots import serialize_run
from personal_agent.domain.common import (
    MODEL_BUSY,
    MODEL_OUTCOME_UNKNOWN,
    RUN_BUSY,
    AppError,
    ErrorCategory,
    new_id,
    stable_hash,
)
from personal_agent.domain.execution import EventKind, RunStatus
from personal_agent.ports.contracts import Clock

UNRESOLVED_SIDE_EFFECTS = {"INTENT", "UNKNOWN", "PARTIAL", "CONFLICT"}


def _check(code: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"code": code, "ok": ok, "detail": detail}


class RecoveryService:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        gateway: ModelGateway,
        store: BlobStore,
        *,
        runtime_fingerprint: str,
        config_fingerprint: str,
        policy_hash: str,
        submit: Callable[[str, bool], None] | None = None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._gateway = gateway
        self._store = store
        self._runtime_fingerprint = runtime_fingerprint
        self._config_fingerprint = config_fingerprint
        self._policy_hash = policy_hash
        self._submit = submit

    # ------------------------------------------------------------------
    async def inspect(self, run_id: str) -> dict[str, Any]:
        async with event_session(self._database) as session:
            run = await self._require(session, run_id)
            checks, blocker = await self._checks(session, run)
            return {
                "run_id": run.id,
                "status": run.status,
                "resumable": run.status
                in {RunStatus.PAUSED.value, RunStatus.INTERRUPTED.value}
                and blocker is None,
                "blocked_by": blocker,
                "checks": checks,
                "epoch": run.epoch,
            }

    async def resume(
        self,
        run_id: str,
        *,
        expected_revision: int,
        allow_waiting_user: bool = False,
    ) -> dict[str, Any]:
        async with event_session(self._database) as session:
            run = await self._require(session, run_id)
            if run.revision != expected_revision:
                raise AppError(
                    code="RUN_REVISION_STALE",
                    message="the execution changed since it was read",
                    category=ErrorCategory.VALIDATION,
                    status=412,
                )
            resumable = {RunStatus.PAUSED.value, RunStatus.INTERRUPTED.value}
            if allow_waiting_user:
                # Only an explicit assistance decision may resume a run that is
                # waiting for the user; a bare resume call may not.
                resumable.add(RunStatus.WAITING_USER.value)
            if run.status not in resumable:
                raise AppError(
                    code="RUN_STATE_INVALID",
                    message="only a paused or interrupted execution can be resumed",
                    category=ErrorCategory.VALIDATION,
                    status=409,
                    details={"status": run.status},
                )
            checks, blocker = await self._checks(session, run)
            if blocker is not None:
                raise AppError(
                    code=blocker,
                    message="the execution cannot resume until this is resolved",
                    category=ErrorCategory.VALIDATION,
                    status=409,
                    details={"checks": checks},
                )

            await self._interrupt_stale_attempts(session, run)
            slot = await session.get(ExecutionSlot, 1)
            if slot is None:
                raise AppError(
                    code="EXECUTION_SLOT_MISSING",
                    message="the execution slot is missing",
                    status=503,
                )
            if slot.run_id is not None and slot.run_id != run.id:
                raise AppError(
                    code=RUN_BUSY,
                    message="another execution holds the single slot",
                    status=409,
                    details={"slot_run_id": slot.run_id},
                )
            # CAS both aggregates: a double resume or a concurrent start can never
            # both acquire the slot.
            await cas_update(
                session, ExecutionSlot, identity={"singleton_id": 1},
                expected_revision=slot.revision,
                values={
                    "run_id": run.id, "epoch": slot.epoch + 1, "draining": False,
                    "process_instance_id": "local",
                },
                stale_code=RUN_BUSY, stale_status=409,
            )
            slot.run_id = run.id
            slot.epoch += 1
            slot.draining = False
            slot.process_instance_id = "local"
            slot.revision += 1

            await cas_update(
                session, WorkflowRun, identity={"id": run.id},
                expected_revision=run.revision,
                values={
                    "status": RunStatus.RUNNING.value, "reason_code": None,
                    "epoch": run.epoch + 1, "cancel_requested": False,
                    "updated_at": self._clock.now(),
                },
                stale_code="RUN_REVISION_STALE", stale_status=412,
            )
            run.status = RunStatus.RUNNING.value
            run.reason_code = None
            run.epoch += 1
            run.revision += 1
            run.cancel_requested = False
            run.updated_at = self._clock.now()
            session.add(
                ExecutionEvent(
                    id=new_id(),
                    run_id=run.id,
                    draft_id=None,
                    sequence=await self._next_sequence(session, run.id),
                    step_run_id=None,
                    attempt_id=None,
                    workflow_version_id=run.workflow_version_id,
                    occurred_at=self._clock.now(),
                    kind=EventKind.RECOVERY_CHECKED.value,
                    status=run.status,
                    reason_code=None,
                    payload_json={"epoch": run.epoch, "checks": checks},
                    payload_ref=None,
                    schema_version=1,
                    provider_event_key=None,
                )
            )
            await session.commit()
            snapshot = await serialize_run(session, run)
        if self._submit is not None:
            self._submit(run_id, True)
        return snapshot

    # ------------------------------------------------------------------
    async def _checks(
        self, session: Any, run: WorkflowRun
    ) -> tuple[list[dict[str, Any]], str | None]:
        checks: list[dict[str, Any]] = []

        version = await session.get(WorkflowVersion, run.workflow_version_id)
        executable = version is not None and bool(version.executable)
        checks.append(
            _check(
                "VERSION",
                executable,
                "confirmed version is available"
                if executable
                else "version is no longer executable",
            )
        )
        runtime_ok = run.runtime_fingerprint == self._runtime_fingerprint
        checks.append(
            _check(
                "RUNTIME",
                runtime_ok,
                "runtime/profile fingerprint matches" if runtime_ok else "running runtime differs",
            )
        )
        policy_ok = (
            run.config_fingerprint == self._config_fingerprint
            and run.budget_policy_hash == self._policy_hash
        )
        checks.append(
            _check(
                "POLICY",
                policy_ok,
                "policy fingerprint matches" if policy_ok else "policy changed since the run froze",
            )
        )
        input_ok = stable_hash(run.input_snapshot_json) == run.input_hash
        checks.append(
            _check(
                "INPUT",
                input_ok,
                "confirmed input is unchanged" if input_ok else "confirmed input was modified",
            )
        )

        operations = list(
            (
                await session.scalars(
                    select(SideEffectOperation).where(
                        SideEffectOperation.root_run_id == run.root_run_id
                    )
                )
            ).all()
        )
        unresolved = [
            operation
            for operation in operations
            if operation.status in UNRESOLVED_SIDE_EFFECTS
        ]
        checks.append(
            _check(
                "SIDE_EFFECTS",
                not unresolved,
                "all root writes are resolved"
                if not unresolved
                else f"{len(unresolved)} write(s) still need verification",
            )
        )

        stored_rows = (
            await session.execute(
                select(Artifact, Blob)
                .join(Blob, Artifact.blob_id == Blob.id)
                .where(Artifact.run_id == run.id)
            )
        ).all()
        integrity_ok = True
        integrity_detail = "stored artifacts match their hashes"
        for artifact, blob in stored_rows:
            # An expired or deleted material stays on disk for the retention window,
            # but it is no longer a usable dependency and must not unlock a resume.
            if artifact.access_state != "AVAILABLE":
                integrity_ok = False
                integrity_detail = (
                    f"artifact {artifact.id} is {artifact.access_state}, not available"
                )
                break
            if blob.deleted_at is not None:
                integrity_ok = False
                integrity_detail = f"blob {blob.id} was deleted"
                break
            if blob.expires_at is not None and blob.expires_at <= self._clock.now():
                integrity_ok = False
                integrity_detail = f"blob {blob.id} has expired"
                break
            try:
                data = await self._store.get(blob.managed_relative_path)
            except FileNotFoundError:
                integrity_ok = False
                integrity_detail = f"blob {blob.id} is missing"
                break
            if hashlib.sha256(data).hexdigest() != blob.sha256:
                integrity_ok = False
                integrity_detail = f"blob {blob.id} does not match its hash"
                break
        checks.append(_check("ARTIFACTS", integrity_ok, integrity_detail))

        from personal_agent.adapters.persistence.assistance_models import AssistanceRequest
        from personal_agent.adapters.persistence.budget_models import BudgetLedger
        from personal_agent.application.budget_service import BudgetService

        open_assistance = await session.scalar(
            select(AssistanceRequest.id)
            .where(AssistanceRequest.run_id == run.id, AssistanceRequest.status == "OPEN")
            .limit(1)
        )
        checks.append(_check(
            "ASSISTANCE",
            open_assistance is None,
            "no assistance request is open"
            if open_assistance is None
            else "an assistance request still needs a user decision",
        ))
        ledger = await session.scalar(
            select(BudgetLedger).where(BudgetLedger.root_run_id == run.root_run_id)
        )
        budget_ok = True
        if ledger is not None:
            domain = BudgetService(self._clock).to_domain(ledger)
            budget_ok = not domain.exhausted
        checks.append(_check(
            "BUDGET",
            budget_ok,
            "the budget allows another action"
            if budget_ok
            else "the budget is exhausted; an explicit top-up is required",
        ))

        # A pending cancel may only be completed as a cancellation; it can never be
        # turned into a resume, not even after a restart.
        cancel_pending = bool(run.cancel_requested)
        checks.append(
            _check(
                "CANCEL",
                not cancel_pending,
                "no cancellation is pending"
                if not cancel_pending
                else "a cancellation is pending; only cancelling can complete it",
            )
        )

        blocker: str | None = None
        if cancel_pending:
            blocker = "CANCEL_PENDING"
        elif not executable:
            blocker = "VERSION_CHANGED"
        elif not runtime_ok:
            blocker = "RUNTIME_INCOMPATIBLE"
        elif not policy_ok:
            blocker = "POLICY_CHANGED"
        elif not input_ok:
            blocker = "INPUT_CHANGED"
        elif unresolved:
            blocker = "SIDE_EFFECT_UNRESOLVED"
        elif not integrity_ok:
            blocker = "ARTIFACT_UNAVAILABLE"
        elif open_assistance is not None:
            blocker = "ASSISTANCE_OPEN"
        elif not budget_ok:
            blocker = "BUDGET_EXHAUSTED"

        if blocker is None:
            pending = await self._gateway.non_terminal_request()
            if pending is not None:
                blocker = (
                    MODEL_OUTCOME_UNKNOWN
                    if pending.status == "UNKNOWN"
                    else MODEL_BUSY
                )
                checks.append(
                    _check(
                        "MODEL",
                        False,
                        f"model request {pending.id} is {pending.status}",
                    )
                )
            else:
                checks.append(_check("MODEL", True, "no model request is unresolved"))
        return checks, blocker

    async def _interrupt_stale_attempts(self, session: Any, run: WorkflowRun) -> None:
        attempts = (
            await session.scalars(
                select(StepAttempt)
                .join(StepRun, StepAttempt.step_run_id == StepRun.id)
                .where(StepRun.run_id == run.id, StepAttempt.status == "RUNNING")
            )
        ).all()
        for attempt in attempts:
            attempt.status = "INTERRUPTED"
            attempt.ended_at = self._clock.now()
            session.add(
                ExecutionEvent(
                    id=new_id(),
                    run_id=run.id,
                    draft_id=None,
                    sequence=await self._next_sequence(session, run.id),
                    step_run_id=attempt.step_run_id,
                    attempt_id=attempt.id,
                    workflow_version_id=run.workflow_version_id,
                    occurred_at=self._clock.now(),
                    kind=EventKind.STEP_STATE_CHANGED.value,
                    status="INTERRUPTED",
                    reason_code="RECOVERY",
                    payload_json={"attempt_id": attempt.id},
                    payload_ref=None,
                    schema_version=1,
                    provider_event_key=None,
                )
            )
        steps = (
            await session.scalars(
                select(StepRun).where(StepRun.run_id == run.id, StepRun.status == "RUNNING")
            )
        ).all()
        for step in steps:
            step.status = "INTERRUPTED"
            step.revision += 1
            step.updated_at = self._clock.now()

    @staticmethod
    async def _require(session: Any, run_id: str) -> WorkflowRun:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            raise AppError(code="RUN_NOT_FOUND", message="run not found", status=404)
        return run

    @staticmethod
    async def _next_sequence(session: Any, run_id: str) -> int:
        from sqlalchemy import func

        value = await session.scalar(
            select(func.max(ExecutionEvent.sequence)).where(ExecutionEvent.run_id == run_id)
        )
        return int(value or 0) + 1

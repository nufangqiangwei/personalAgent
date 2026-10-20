"""Stopping a run: pause, cancel and convergence.

A stop request is an *intent*: it is recorded immediately so the user gets feedback
well inside one second, and the state only becomes final once the current action has
converged. Nothing here advances step results — the engine owns that — but the
convergence rules live in one place so the API and the engine cannot disagree.

Rules:
* ``PAUSE`` goes ``RUNNING -> PAUSING -> PAUSED``; the slot is released only when
  converged and no write is unresolved, so a paused run does not block the app.
* ``CANCEL`` goes to ``CANCELLING`` and ends at ``CANCELLED``; a result that arrives
  afterwards can never turn the run into a success.
* An attempt that is still in flight keeps the run in the draining state. It is not
  reported as stopped until it has actually returned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update

from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.execution_models import (
    ExecutionEvent,
    ExecutionSlot,
    StepAttempt,
    StepRun,
    WorkflowRun,
)
from personal_agent.adapters.persistence.uow import cas_update
from personal_agent.application.event_log import event_session
from personal_agent.application.run_snapshots import serialize_run
from personal_agent.domain.common import AppError, ErrorCategory, new_id
from personal_agent.domain.execution import EventKind, RunStatus, assert_transition
from personal_agent.ports.contracts import Clock


@dataclass
class StopSignal:
    """Process-wide cooperative stop flag for the single active execution."""

    stopping: bool = False


class StopControl:
    def __init__(
        self, database: Database, clock: Clock, *, convergence_seconds: float = 15.0,
    ) -> None:
        self._database = database
        self._clock = clock
        self._convergence_seconds = convergence_seconds

    # ------------------------------------------------------------------
    async def request_pause(
        self, run_id: str, *, expected_revision: int, reason: str | None = None
    ) -> dict[str, Any]:
        async with event_session(self._database) as session:
            run = await self._require(session, run_id)
            if run.revision != expected_revision:
                raise _stale()
            if run.status == RunStatus.PAUSING.value:
                return await serialize_run(session, run)
            if run.status not in {RunStatus.RUNNING.value, RunStatus.WAITING_USER.value}:
                raise AppError(
                    code="RUN_STATE_INVALID",
                    message="only a running or waiting execution can be paused",
                    category=ErrorCategory.VALIDATION,
                    status=409,
                    details={"status": run.status},
                )
            assert_transition(RunStatus(run.status), RunStatus.PAUSING)
            now = self._clock.now()
            await cas_update(
                session, WorkflowRun, identity={"id": run.id},
                expected_revision=run.revision,
                values={"status": RunStatus.PAUSING.value, "updated_at": now},
                stale_code="RUN_REVISION_STALE", stale_status=412,
            )
            run.status = RunStatus.PAUSING.value
            run.revision += 1
            run.updated_at = now
            await self._event(
                session,
                run,
                RunStatus.PAUSING.value,
                reason_code=None,
                payload={"requested": "PAUSE", "reason": reason, "draining": True},
            )
            await session.commit()
        return await self.converge(run_id)

    async def request_cancel(
        self,
        run_id: str,
        *,
        expected_revision: int,
        reason: str | None = None,
        confirmed: bool = True,
    ) -> dict[str, Any]:
        if not confirmed:
            raise AppError(
                code="CANCEL_NOT_CONFIRMED",
                message="cancelling needs an explicit confirmation",
                category=ErrorCategory.VALIDATION,
                status=422,
            )
        async with event_session(self._database) as session:
            run = await self._require(session, run_id)
            if run.revision != expected_revision:
                raise _stale()
            if run.status == RunStatus.CANCELLED.value:
                return await serialize_run(session, run)
            if run.status not in {
                RunStatus.PENDING.value,
                RunStatus.RUNNING.value,
                RunStatus.PAUSING.value,
                RunStatus.PAUSED.value,
                RunStatus.WAITING_USER.value,
                RunStatus.INTERRUPTED.value,
            }:
                raise AppError(
                    code="RUN_STATE_INVALID",
                    message="this execution cannot be cancelled in its current state",
                    category=ErrorCategory.VALIDATION,
                    status=409,
                    details={"status": run.status},
                )
            assert_transition(RunStatus(run.status), RunStatus.CANCELLING)
            now = self._clock.now()
            await cas_update(
                session, WorkflowRun, identity={"id": run.id},
                expected_revision=run.revision,
                values={
                    "status": RunStatus.CANCELLING.value,
                    "cancel_requested": True,
                    "updated_at": now,
                },
                stale_code="RUN_REVISION_STALE", stale_status=412,
            )
            run.status = RunStatus.CANCELLING.value
            run.cancel_requested = True
            run.revision += 1
            run.updated_at = now
            await self._event(
                session,
                run,
                RunStatus.CANCELLING.value,
                reason_code=None,
                payload={"requested": "CANCEL", "reason": reason, "draining": True},
            )
            await session.commit()
        return await self.converge(run_id)

    # ------------------------------------------------------------------
    async def converge(self, run_id: str) -> dict[str, Any]:
        """Move a draining run to its final state once no action is in flight."""
        async with event_session(self._database) as session:
            run = await self._require(session, run_id)
            in_flight = await self._in_flight_count(session, run.id)
            if run.status == RunStatus.PAUSING.value and in_flight == 0:
                now = self._clock.now()
                await cas_update(
                    session, WorkflowRun, identity={"id": run.id},
                    expected_revision=run.revision,
                    values={"status": RunStatus.PAUSED.value, "updated_at": now},
                    stale_code="RUN_REVISION_STALE", stale_status=412,
                )
                run.status = RunStatus.PAUSED.value
                run.revision += 1
                run.updated_at = now
                await self._release_slot(session, run)
                await self._event(
                    session,
                    run,
                    RunStatus.PAUSED.value,
                    reason_code=None,
                    payload={"converged": True, "in_flight": 0},
                )
            elif run.status == RunStatus.CANCELLING.value and in_flight == 0:
                now = self._clock.now()
                await cas_update(
                    session, WorkflowRun, identity={"id": run.id},
                    expected_revision=run.revision,
                    values={"status": RunStatus.CANCELLED.value, "updated_at": now},
                    stale_code="RUN_REVISION_STALE", stale_status=412,
                )
                run.status = RunStatus.CANCELLED.value
                run.revision += 1
                run.updated_at = now
                await self._close_running_steps(session, run)
                await self._release_slot(session, run)
                await self._event(
                    session,
                    run,
                    RunStatus.CANCELLED.value,
                    reason_code=None,
                    payload={"converged": True, "in_flight": 0},
                )
            elif in_flight > 0 and await self._stop_timed_out(session, run):
                # The remote action did not converge in time. Its outcome is
                # unknown, so the run is truthfully interrupted and the slot is
                # retained; only verified evidence may unlock it.
                await self._interrupt_after_timeout(session, run)
            await session.commit()
            snapshot = await serialize_run(session, run)
            snapshot["draining"] = run.status in {
                RunStatus.PAUSING.value,
                RunStatus.CANCELLING.value,
            } or run.reason_code == "STOP_TIMEOUT"
            snapshot["in_flight_actions"] = await self._in_flight_actions(session, run.id)
            return snapshot

    async def _stop_timed_out(self, session: Any, run: WorkflowRun) -> bool:
        requested_at = await session.scalar(
            select(ExecutionEvent.occurred_at)
            .where(
                ExecutionEvent.run_id == run.id,
                ExecutionEvent.kind == EventKind.RUN_STATE_CHANGED.value,
                ExecutionEvent.status.in_([
                    RunStatus.PAUSING.value, RunStatus.CANCELLING.value,
                ]),
            )
            .order_by(ExecutionEvent.sequence.desc())
            .limit(1)
        )
        if requested_at is None:
            requested_at = run.updated_at
        return (
            self._clock.now() - requested_at
        ).total_seconds() >= self._convergence_seconds

    async def _interrupt_after_timeout(
        self, session: Any, run: WorkflowRun,
    ) -> None:
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
            attempt.error_category = "TIMEOUT"
        steps = (
            await session.scalars(
                select(StepRun).where(
                    StepRun.run_id == run.id, StepRun.status == "RUNNING",
                )
            )
        ).all()
        for step in steps:
            step.status = "INTERRUPTED"
            step.revision += 1
            step.updated_at = self._clock.now()
        now = self._clock.now()
        await cas_update(
            session, WorkflowRun, identity={"id": run.id},
            expected_revision=run.revision,
            values={
                "status": RunStatus.INTERRUPTED.value,
                "reason_code": "STOP_TIMEOUT",
                "updated_at": now,
            },
            stale_code="RUN_REVISION_STALE", stale_status=412,
        )
        run.status = RunStatus.INTERRUPTED.value
        run.reason_code = "STOP_TIMEOUT"
        run.revision += 1
        run.updated_at = now
        # The remote outcome is unknown: keep the slot until verified.
        await session.execute(
            update(ExecutionSlot)
            .where(ExecutionSlot.run_id == run.id)
            .values(draining=True, revision=ExecutionSlot.revision + 1)
            .execution_options(synchronize_session=False)
        )
        await self._event(
            session,
            run,
            RunStatus.INTERRUPTED.value,
            reason_code="STOP_TIMEOUT",
            payload={"converged": False, "in_flight": len(attempts)},
        )

    async def observe(self, run_id: str) -> str | None:
        """Return ``pause``/``cancel`` when the engine must stop, else ``None``."""
        async with self._database.session() as session:
            run = await session.get(WorkflowRun, run_id)
            if run is None:
                return None
            if run.status == RunStatus.CANCELLING.value:
                return "cancel"
            if run.status == RunStatus.PAUSING.value:
                return "pause"
            return None

    async def converge_from_engine(self, run_id: str) -> str:
        """Called by the engine between steps: finalize and return the status."""
        snapshot = await self.converge(run_id)
        return str(snapshot["status"])

    # ------------------------------------------------------------------
    @staticmethod
    async def _require(session: Any, run_id: str) -> WorkflowRun:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            raise AppError(code="RUN_NOT_FOUND", message="run not found", status=404)
        return run

    @staticmethod
    async def _in_flight_count(session: Any, run_id: str) -> int:
        rows = (
            await session.scalars(
                select(StepAttempt)
                .join(StepRun, StepAttempt.step_run_id == StepRun.id)
                .where(
                    StepRun.run_id == run_id,
                    StepAttempt.status == "RUNNING",
                )
            )
        ).all()
        return len(rows)

    @staticmethod
    async def _in_flight_actions(session: Any, run_id: str) -> list[dict[str, Any]]:
        rows = (
            await session.scalars(
                select(StepAttempt)
                .join(StepRun, StepAttempt.step_run_id == StepRun.id)
                .where(StepRun.run_id == run_id, StepAttempt.status == "RUNNING")
            )
        ).all()
        return [
            {
                "id": attempt.id,
                "type": "model_request",
                "status": attempt.status,
                "step_run_id": attempt.step_run_id,
            }
            for attempt in rows
        ]

    async def _close_running_steps(self, session: Any, run: WorkflowRun) -> None:
        await session.execute(
            update(StepRun)
            .where(
                StepRun.run_id == run.id,
                StepRun.status.in_(["PENDING", "RUNNING", "WAITING_USER"]),
            )
            .values(
                status="CANCELLED",
                revision=StepRun.revision + 1,
                updated_at=self._clock.now(),
            )
            .execution_options(synchronize_session=False)
        )

    async def _release_slot(self, session: Any, run: WorkflowRun) -> None:
        await session.execute(
            update(ExecutionSlot)
            .where(ExecutionSlot.run_id == run.id)
            .values(
                run_id=None,
                draining=False,
                epoch=ExecutionSlot.epoch + 1,
                revision=ExecutionSlot.revision + 1,
            )
            .execution_options(synchronize_session=False)
        )

    async def _event(
        self,
        session: Any,
        run: WorkflowRun,
        status: str,
        *,
        reason_code: str | None,
        payload: dict[str, Any],
    ) -> None:
        from sqlalchemy import func

        sequence = int(
            await session.scalar(
                select(func.max(ExecutionEvent.sequence)).where(
                    ExecutionEvent.run_id == run.id
                )
            )
            or 0
        ) + 1
        session.add(
            ExecutionEvent(
                id=new_id(),
                run_id=run.id,
                draft_id=None,
                sequence=sequence,
                step_run_id=None,
                attempt_id=None,
                workflow_version_id=run.workflow_version_id,
                occurred_at=self._clock.now(),
                kind=EventKind.RUN_STATE_CHANGED.value,
                status=status,
                reason_code=reason_code,
                payload_json=payload,
                payload_ref=None,
                schema_version=1,
                provider_event_key=None,
            )
        )


def _stale() -> AppError:
    return AppError(
        code="RUN_REVISION_STALE",
        message="the execution changed since it was read",
        category=ErrorCategory.VALIDATION,
        status=412,
    )

"""Execution states and the only legal transitions between them.

Only the engine/coordinator may drive these transitions; repositories, adapters
and the API never advance business state on their own.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from personal_agent.domain.common import AppError, ErrorCategory


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_USER = "WAITING_USER"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    WAITING_USER = "WAITING_USER"
    INTERRUPTED = "INTERRUPTED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class AttemptStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class Completeness(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    NO_QUALIFIED_RESULT = "NO_QUALIFIED_RESULT"


TERMINAL_RUN_STATUSES: Final[frozenset[RunStatus]] = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
)


#: Legal transitions. Everything absent is rejected; there is no "any -> any".
ALLOWED_RUN_TRANSITIONS: Final[dict[RunStatus, frozenset[RunStatus]]] = {
    RunStatus.PENDING: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_USER,
            RunStatus.PAUSING,
            RunStatus.CANCELLING,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.INTERRUPTED,
        }
    ),
    RunStatus.WAITING_USER: frozenset(
        {RunStatus.RUNNING, RunStatus.PAUSING, RunStatus.CANCELLING, RunStatus.FAILED}
    ),
    RunStatus.PAUSING: frozenset({RunStatus.PAUSED, RunStatus.INTERRUPTED, RunStatus.CANCELLING}),
    RunStatus.PAUSED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLING}),
    RunStatus.CANCELLING: frozenset({RunStatus.CANCELLED, RunStatus.INTERRUPTED}),
    RunStatus.INTERRUPTED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLING}),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
}

RESUMABLE_RUN_STATUSES: Final[frozenset[RunStatus]] = frozenset(
    {RunStatus.PAUSED, RunStatus.INTERRUPTED}
)

#: Steps that still count as "an action may be in flight" for draining checks.
IN_FLIGHT_RUN_STATUSES: Final[frozenset[RunStatus]] = frozenset(
    {RunStatus.RUNNING, RunStatus.PAUSING, RunStatus.CANCELLING}
)

LEGACY_INTERRUPTIBLE: Final[frozenset[RunStatus]] = frozenset(
    {RunStatus.RUNNING, RunStatus.PAUSING, RunStatus.CANCELLING}
)


def can_transition(current: RunStatus, target: RunStatus) -> bool:
    return target in ALLOWED_RUN_TRANSITIONS[current]


def assert_transition(current: RunStatus, target: RunStatus) -> None:
    if not can_transition(current, target):
        raise AppError(
            code="RUN_STATE_INVALID",
            message=f"run cannot move from {current} to {target}",
            category=ErrorCategory.VALIDATION,
            status=409,
            details={"current": str(current), "target": str(target)},
        )


def is_terminal(status: RunStatus) -> bool:
    return status in TERMINAL_RUN_STATUSES


def cancel_requested(status: RunStatus) -> bool:
    """A cancelled/cancelling run must never be revived by a late result."""
    return status in {RunStatus.CANCELLING, RunStatus.CANCELLED}


class EventKind(StrEnum):
    """Normalised, reviewable event kinds; reasoning is never an event."""

    RUN_STATE_CHANGED = "run.state_changed"
    STEP_STATE_CHANGED = "step.state_changed"
    ACTION_STARTED = "action.started"
    ACTION_FINISHED = "action.finished"
    ARTIFACT_AVAILABLE = "artifact.available"
    ASSISTANCE_REQUIRED = "assistance.required"
    BUDGET_CHANGED = "budget.changed"
    RECOVERY_CHECKED = "recovery.checked"
    BRANCH_CREATED = "branch.created"
    LATE_RESULT_QUARANTINED = "late_result.quarantined"


class ModelRequestStatus(StrEnum):
    RESERVED = "RESERVED"
    IN_FLIGHT = "IN_FLIGHT"
    COMPLETED = "COMPLETED"
    CANCELLED_CONFIRMED = "CANCELLED_CONFIRMED"
    UNKNOWN = "UNKNOWN"


NON_TERMINAL_MODEL_STATUSES: Final[frozenset[ModelRequestStatus]] = frozenset(
    {
        ModelRequestStatus.RESERVED,
        ModelRequestStatus.IN_FLIGHT,
        ModelRequestStatus.UNKNOWN,
    }
)


def release_slot_allowed(*, converged: bool, unknown_writes: int, draining: bool) -> bool:
    """A slot is released only after convergence and with no unresolved write."""
    return converged and unknown_writes == 0 and not draining

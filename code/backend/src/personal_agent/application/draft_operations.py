"""Analysis operation state helpers (visible, non-retriggering)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.adapters.persistence.operation_models import AnalysisOperation
from personal_agent.domain.common import AppError, iso_z, new_id
from personal_agent.ports.contracts import Clock


class OperationStatus:
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_MODEL = "WAITING_MODEL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


def create_operation(
    session: AsyncSession,
    *,
    clock: Clock,
    draft_id: str,
    message_id: str,
    status: str = OperationStatus.PENDING,
) -> AnalysisOperation:
    now = clock.now()
    operation = AnalysisOperation(
        id=new_id(),
        draft_id=draft_id,
        message_id=message_id,
        status=status,
        result_ref=None,
        error_json=None,
        created_at=now,
        updated_at=now,
        revision=1,
    )
    session.add(operation)
    return operation


def finish_operation(
    operation: AnalysisOperation,
    *,
    clock: Clock,
    status: str,
    result_ref: str | None = None,
    error: AppError | None = None,
) -> None:
    operation.status = status
    operation.result_ref = result_ref
    operation.error_json = (
        {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "category": str(error.category),
        }
        if error is not None
        else None
    )
    operation.updated_at = clock.now()
    operation.revision += 1


def serialize_operation(operation: AnalysisOperation) -> dict[str, Any]:
    return {
        "id": operation.id,
        "status": operation.status,
        "result_ref": operation.result_ref,
        "error": operation.error_json,
        "draft_id": operation.draft_id,
        "message_id": operation.message_id,
        "created_at": iso_z(operation.created_at),
        "updated_at": iso_z(operation.updated_at),
    }

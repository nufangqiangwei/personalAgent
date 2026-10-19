"""The single model gateway.

Every model call — clarification, generation, compaction, retry and document
composition — must pass here. The gateway:

* occupies a persistent request row in a short transaction *before* sending;
* allows only one non-terminal request in the whole application;
* keeps ``UNKNOWN`` (timeout, disconnect, local cancel) as non-terminal, so a
  restart cannot silently free the slot;
* releases the slot only after verified completion evidence;
* never logs or persists prompts/reasoning; the provider credential stays in the
  gateway and is not exposed to the runtime adapter.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.adapters.persistence.budget_models import ModelRequest
from personal_agent.adapters.persistence.database import Database
from personal_agent.domain.common import (
    MODEL_BUSY,
    MODEL_OUTCOME_UNKNOWN,
    AppError,
    ErrorCategory,
    iso_z,
    new_id,
    stable_hash,
)
from personal_agent.domain.execution import (
    NON_TERMINAL_MODEL_STATUSES,
    ModelRequestStatus,
)
from personal_agent.ports.contracts import (
    Clock,
    ModelResponse,
    Usage,
)
from personal_agent.ports.contracts import (
    ModelRequest as ModelRequestSpec,
)


class ModelTransport(Protocol):
    """Provider transport. Only the gateway receives a real implementation."""

    async def complete(self, spec: ModelRequestSpec) -> ModelResponse: ...


@dataclass(frozen=True, slots=True)
class Reservation:
    request_id: str
    reserved_tokens: int


class ModelGateway:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        transport: ModelTransport,
        *,
        runtime_id: str = "gateway",
    ) -> None:
        self._database = database
        self._clock = clock
        self._transport = transport
        self._runtime_id = runtime_id

    # ------------------------------------------------------------------
    async def non_terminal_request(self) -> ModelRequest | None:
        async with self._database.session() as session:
            return await self._find_non_terminal(session)

    async def _find_non_terminal(self, session: AsyncSession) -> ModelRequest | None:
        return await session.scalar(
            select(ModelRequest)
            .where(ModelRequest.status.in_([s.value for s in NON_TERMINAL_MODEL_STATUSES]))
            .order_by(ModelRequest.started_at)
            .limit(1)
        )

    async def has_unknown(self) -> bool:
        request = await self.non_terminal_request()
        return request is not None and request.status == ModelRequestStatus.UNKNOWN.value

    # ------------------------------------------------------------------
    async def reserve(
        self,
        spec: ModelRequestSpec,
        *,
        reserved_tokens: int,
    ) -> Reservation:
        """Occupy the slot persistently; refuses a second in-flight request."""
        if reserved_tokens <= 0:
            raise AppError(
                code="MODEL_RESERVATION_INVALID",
                message="a model request must reserve a positive token upper bound",
                category=ErrorCategory.BUDGET,
                status=422,
            )
        async with self._database.session() as session:
            existing = await self._find_non_terminal(session)
            if existing is not None:
                if existing.status == ModelRequestStatus.UNKNOWN.value:
                    raise AppError(
                        code=MODEL_OUTCOME_UNKNOWN,
                        message=(
                            "a previous model request has an unverified outcome; "
                            "no new request may start"
                        ),
                        category=ErrorCategory.UNKNOWN_OUTCOME,
                        status=409,
                        details={"request_id": existing.id},
                    )
                raise AppError(
                    code=MODEL_BUSY,
                    message="another model request is in flight",
                    category=ErrorCategory.BUDGET,
                    status=409,
                    details={"request_id": existing.id},
                )
            request_id = spec.request_id or new_id()
            row = ModelRequest(
                id=request_id,
                owner_type=spec.owner.kind,
                owner_id=spec.owner.draft_id or spec.owner.run_id or "",
                attempt_id=spec.owner.attempt_id,
                runtime_id=self._runtime_id,
                model_id=spec.model_id,
                status=ModelRequestStatus.RESERVED.value,
                reserved_tokens=reserved_tokens,
                actual_tokens=None,
                request_hash=stable_hash(
                    {
                        "owner": {
                            "run_id": spec.owner.run_id,
                            "attempt_id": spec.owner.attempt_id,
                            "draft_id": spec.owner.draft_id,
                        },
                        "epoch": spec.epoch,
                        "model_id": spec.model_id,
                        "output_limit": spec.output_limit,
                        "message_count": len(spec.messages),
                    }
                ),
                started_at=self._clock.now(),
            )
            session.add(row)
            await session.commit()
            return Reservation(request_id=request_id, reserved_tokens=reserved_tokens)

    async def mark_in_flight(self, request_id: str) -> None:
        async with self._database.session() as session:
            row = await session.get(ModelRequest, request_id)
            if row is None:
                raise AppError(
                    code="MODEL_REQUEST_NOT_FOUND", message="model request not found", status=404
                )
            if row.status != ModelRequestStatus.RESERVED.value:
                raise AppError(
                    code=MODEL_BUSY,
                    message="model request is not in the RESERVED state",
                    status=409,
                    details={"status": row.status},
                )
            row.status = ModelRequestStatus.IN_FLIGHT.value
            await session.commit()

    # ------------------------------------------------------------------
    async def request(
        self,
        spec: ModelRequestSpec,
        *,
        reserved_tokens: int,
        timeout_seconds: float,
    ) -> ModelResponse:
        reservation = await self.reserve(spec, reserved_tokens=reserved_tokens)
        try:
            await self.mark_in_flight(reservation.request_id)
            try:
                async with asyncio.timeout(timeout_seconds):
                    response = await self._transport.complete(spec)
            except TimeoutError:
                await self._record_unknown(
                    reservation.request_id,
                    error=AppError(
                        code="MODEL_TIMEOUT",
                        message="the model request exceeded its timeout",
                        category=ErrorCategory.TIMEOUT,
                        retryable=False,
                        status=504,
                    ),
                )
                return ModelResponse(
                    status="unknown",
                    error=AppError(
                        code="MODEL_TIMEOUT",
                        message=(
                            "the model request did not finish; its outcome stays unknown "
                            "until verified"
                        ),
                        category=ErrorCategory.UNKNOWN_OUTCOME,
                        retryable=False,
                        status=409,
                    ),
                )

            if response.status == "completed":
                await self.complete(
                    reservation.request_id,
                    usage=response.usage,
                    evidence_ref=response.completion_evidence_ref,
                )
                return response
            if response.status == "unknown":
                await self._record_unknown(reservation.request_id, error=response.error)
                return response
            await self._record_unknown(reservation.request_id, error=response.error)
            return response
        except AppError:
            raise
        except Exception as error:  # transport bug: keep the slot, do not guess
            await self._record_unknown(
                reservation.request_id,
                error=AppError(
                    code="MODEL_TRANSPORT_ERROR",
                    message="the model transport failed; outcome is unknown",
                    category=ErrorCategory.UNKNOWN_OUTCOME,
                    details={"type": type(error).__name__},
                ),
            )
            return ModelResponse(
                status="unknown",
                error=AppError(
                    code="MODEL_TRANSPORT_ERROR",
                    message="the model transport failed; outcome is unknown",
                    category=ErrorCategory.UNKNOWN_OUTCOME,
                    status=409,
                ),
            )

    async def _record_unknown(self, request_id: str, *, error: AppError | None) -> None:
        async with self._database.session() as session:
            row = await session.get(ModelRequest, request_id)
            if row is None:
                return
            row.status = ModelRequestStatus.UNKNOWN.value
            row.ended_at = self._clock.now()
            row.error_json = (
                {
                    "category": str(error.category),
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                }
                if error
                else None
            )
            await session.commit()

    async def complete(
        self,
        request_id: str,
        *,
        usage: Usage,
        evidence_ref: str | None,
    ) -> None:
        """Verified completion: the only path that frees the slot."""
        if not evidence_ref:
            raise AppError(
                code="COMPLETION_EVIDENCE_REQUIRED",
                message="a completed request needs a verifiable completion record",
                category=ErrorCategory.INTEGRITY,
                status=409,
            )
        async with self._database.session() as session:
            row = await session.get(ModelRequest, request_id)
            if row is None:
                raise AppError(
                    code="MODEL_REQUEST_NOT_FOUND", message="model request not found", status=404
                )
            if row.status == ModelRequestStatus.COMPLETED.value:
                return
            row.status = ModelRequestStatus.COMPLETED.value
            row.actual_tokens = usage.tokens
            row.completion_evidence_ref = evidence_ref
            row.ended_at = self._clock.now()
            await session.commit()

    async def confirm_cancelled(self, request_id: str, *, evidence_ref: str) -> None:
        """A remote cancellation counts only with evidence; otherwise stay UNKNOWN."""
        if not evidence_ref:
            raise AppError(
                code="COMPLETION_EVIDENCE_REQUIRED",
                message="a cancelled request needs verifiable cancellation evidence",
                category=ErrorCategory.INTEGRITY,
                status=409,
            )
        async with self._database.session() as session:
            row = await session.get(ModelRequest, request_id)
            if row is None:
                raise AppError(
                    code="MODEL_REQUEST_NOT_FOUND", message="model request not found", status=404
                )
            row.status = ModelRequestStatus.CANCELLED_CONFIRMED.value
            row.completion_evidence_ref = evidence_ref
            row.ended_at = self._clock.now()
            await session.commit()

    async def reconcile(self, request_id: str, evidence: dict | None) -> ModelRequestStatus:
        """Re-read the persisted state; absence of evidence never unlocks the slot."""
        async with self._database.session() as session:
            row = await session.get(ModelRequest, request_id)
            if row is None:
                raise AppError(
                    code="MODEL_REQUEST_NOT_FOUND", message="model request not found", status=404
                )
            if evidence and evidence.get("completed") and evidence.get("evidence_ref"):
                row.status = ModelRequestStatus.COMPLETED.value
                row.actual_tokens = int(evidence.get("tokens") or 0)
                row.completion_evidence_ref = str(evidence["evidence_ref"])
                row.ended_at = self._clock.now()
                await session.commit()
            return ModelRequestStatus(row.status)

    async def describe(self, request_id: str) -> dict[str, object]:
        async with self._database.session() as session:
            row = await session.get(ModelRequest, request_id)
            if row is None:
                raise AppError(
                    code="MODEL_REQUEST_NOT_FOUND", message="model request not found", status=404
                )
            return {
                "id": row.id,
                "owner_type": row.owner_type,
                "owner_id": row.owner_id,
                "status": row.status,
                "reserved_tokens": row.reserved_tokens,
                "actual_tokens": row.actual_tokens,
                "started_at": iso_z(row.started_at),
                "ended_at": iso_z(row.ended_at) if row.ended_at else None,
                "completion_evidence_ref": row.completion_evidence_ref,
                "error": row.error_json,
            }


def deadline_from(started: datetime, seconds: float) -> datetime:
    from datetime import timedelta

    return started + timedelta(seconds=seconds)

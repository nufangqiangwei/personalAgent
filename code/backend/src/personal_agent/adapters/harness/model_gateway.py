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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.adapters.persistence.budget_models import (
    BudgetEntry,
    BudgetLedger,
    ModelRequest,
)
from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.execution_models import (
    ExecutionSlot,
    StepAttempt,
    StepRun,
    WorkflowRun,
)
from personal_agent.adapters.persistence.requirement_models import RequirementDraft
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


@dataclass(frozen=True, slots=True)
class ProviderOutcome:
    """A provider reply that keeps its wire payload for the protocol forwarder.

    ``payload`` is transport-owned protocol data. It is never persisted as
    business content: the gateway stores only bounded usage and a completion
    evidence reference. A streaming proxy needs the payload to rebuild the
    provider event stream for the runtime.
    """

    status: str
    payload: dict[str, Any] | None = None
    usage: Usage = field(default_factory=Usage)
    completion_evidence_ref: str | None = None
    error: AppError | None = None


class ModelGateway:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        transport: ModelTransport,
        *,
        runtime_id: str = "gateway",
        allowed_model_ids: set[str] | None = None,
        enforce_owner: bool = False,
    ) -> None:
        self._database = database
        self._clock = clock
        self._transport = transport
        self._runtime_id = runtime_id
        self._allowed_model_ids = (
            frozenset(allowed_model_ids) if allowed_model_ids is not None else None
        )
        self._enforce_owner = enforce_owner

    async def _authorize(
        self, session: AsyncSession, spec: ModelRequestSpec, reserved_tokens: int
    ) -> None:
        if not self._enforce_owner:
            return
        owner = spec.owner
        ledger = None
        if owner.draft_id:
            draft = await session.get(RequirementDraft, owner.draft_id)
            if draft is not None and draft.status in {"COLLECTING", "READY"} and spec.epoch == 1:
                ledger = await session.scalar(
                    select(BudgetLedger).where(BudgetLedger.draft_id == draft.id)
                )
        elif owner.run_id and owner.attempt_id:
            run = await session.get(WorkflowRun, owner.run_id)
            attempt = await session.get(StepAttempt, owner.attempt_id)
            slot = await session.get(ExecutionSlot, 1)
            step = await session.get(StepRun, attempt.step_run_id) if attempt else None
            if (
                run is not None and run.status == "RUNNING" and run.epoch == spec.epoch
                and slot is not None and slot.run_id == run.id and not slot.draining
                and attempt is not None and attempt.status == "RUNNING"
                and attempt.epoch == run.epoch
                and step is not None and step.run_id == run.id
                and step.current_attempt_no == attempt.attempt_no
            ):
                ledger = await session.scalar(
                    select(BudgetLedger).where(BudgetLedger.root_run_id == run.root_run_id)
                )
        if ledger is None:
            raise AppError(
                code="MODEL_OWNER_INVALID",
                message="the owner or execution epoch is no longer valid",
                category=ErrorCategory.PERMISSION, status=403,
            )
        entries = list((await session.scalars(select(BudgetEntry).where(
            BudgetEntry.ledger_id == ledger.id,
            BudgetEntry.operation_id == spec.reservation_id,
        ))).all())
        reservation = next((entry for entry in entries if entry.kind == "RESERVE"), None)
        if (
            reservation is None
            or any(entry.kind in {"SETTLE", "RELEASE"} for entry in entries)
            or reservation.amount_json.get("model_requests", 0) < 1
            or reservation.amount_json.get("tokens", 0) < reserved_tokens
            or reserved_tokens < spec.output_limit
        ):
            raise AppError(
                code="MODEL_RESERVATION_INVALID",
                message="the model request needs an unused reservation belonging to its owner",
                category=ErrorCategory.BUDGET, status=409,
            )

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
        if self._allowed_model_ids is not None and spec.model_id not in self._allowed_model_ids:
            raise AppError(
                code="MODEL_NOT_ALLOWED", message="the model is not configured for this gateway",
                category=ErrorCategory.PERMISSION, status=403,
            )
        if reserved_tokens <= 0:
            raise AppError(
                code="MODEL_RESERVATION_INVALID",
                message="a model request must reserve a positive token upper bound",
                category=ErrorCategory.BUDGET,
                status=422,
            )
        async with self._database.session() as session:
            await self._authorize(session, spec, reserved_tokens)
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
                        "messages": [
                            {"role": message.role, "content": message.content}
                            for message in spec.messages
                        ],
                        "reservation_id": spec.reservation_id,
                    }
                ),
                epoch=spec.epoch,
                reservation_id=spec.reservation_id if self._enforce_owner else None,
                started_at=self._clock.now(),
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                blocker = await self._find_non_terminal(session)
                if blocker is None:
                    raise AppError(
                        code="MODEL_REQUEST_CONFLICT",
                        message="the request identity already exists",
                        category=ErrorCategory.INTEGRITY, status=409,
                    ) from error
                raise AppError(
                    code=(MODEL_OUTCOME_UNKNOWN if blocker.status == "UNKNOWN" else MODEL_BUSY),
                    message="another model request holds the persistent slot",
                    category=ErrorCategory.UNKNOWN_OUTCOME if blocker.status == "UNKNOWN"
                    else ErrorCategory.BUDGET,
                    status=409, details={"request_id": blocker.id},
                ) from error
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
    async def exchange(
        self,
        spec: ModelRequestSpec,
        *,
        reserved_tokens: int,
        timeout_seconds: float,
        send: Callable[[ModelRequestSpec], Awaitable[ProviderOutcome]],
    ) -> ProviderOutcome:
        """Occupy the one persistent slot around an arbitrary provider transport.

        The caller supplies the provider call so a protocol proxy can forward the
        request body it received. Slot, timeout, UNKNOWN retention and verified
        settlement stay here: a forwarder cannot release the slot by itself.
        """
        reservation = await self.reserve(spec, reserved_tokens=reserved_tokens)
        spec = replace(spec, request_id=reservation.request_id)
        try:
            await self.mark_in_flight(reservation.request_id)
            try:
                async with asyncio.timeout(timeout_seconds):
                    outcome = await send(spec)
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
                return ProviderOutcome(
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

            if outcome.status == "completed" or (
                outcome.status == "failed" and outcome.completion_evidence_ref
            ):
                await self.complete(
                    reservation.request_id,
                    usage=outcome.usage,
                    evidence_ref=outcome.completion_evidence_ref,
                )
                return outcome
            await self._record_unknown(reservation.request_id, error=outcome.error)
            return outcome
        except asyncio.CancelledError:
            await asyncio.shield(self._record_unknown(
                reservation.request_id,
                error=AppError(
                    code="MODEL_LOCAL_CANCELLED",
                    message="local cancellation does not confirm the remote outcome",
                    category=ErrorCategory.UNKNOWN_OUTCOME,
                ),
            ))
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
            return ProviderOutcome(
                status="unknown",
                error=AppError(
                    code="MODEL_TRANSPORT_ERROR",
                    message="the model transport failed; outcome is unknown",
                    category=ErrorCategory.UNKNOWN_OUTCOME,
                    status=409,
                ),
            )

    async def request(
        self,
        spec: ModelRequestSpec,
        *,
        reserved_tokens: int,
        timeout_seconds: float,
    ) -> ModelResponse:
        outcome = await self.exchange(
            spec,
            reserved_tokens=reserved_tokens,
            timeout_seconds=timeout_seconds,
            send=self._transport_send,
        )
        text = (outcome.payload or {}).get("text")
        return ModelResponse(
            status=outcome.status,
            content=text if isinstance(text, str) else None,
            usage=outcome.usage,
            completion_evidence_ref=outcome.completion_evidence_ref,
            error=outcome.error,
        )

    async def _transport_send(self, spec: ModelRequestSpec) -> ProviderOutcome:
        response = await self._transport.complete(spec)
        return ProviderOutcome(
            status=response.status,
            payload={"text": response.content} if response.content is not None else None,
            usage=response.usage,
            completion_evidence_ref=response.completion_evidence_ref,
            error=response.error,
        )

    async def _record_unknown(self, request_id: str, *, error: AppError | None) -> None:
        async with self._database.session() as session:
            row = await session.get(ModelRequest, request_id)
            if row is None:
                return
            if row.status in {"COMPLETED", "CANCELLED_CONFIRMED"}:
                return
            row.status = ModelRequestStatus.UNKNOWN.value
            row.ended_at = None
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
            if usage.input_tokens < 0 or usage.output_tokens < 0:
                raise AppError(
                    code="COMPLETION_USAGE_INVALID",
                    message="verified token usage cannot be negative",
                    category=ErrorCategory.INTEGRITY, status=409,
                )
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
                tokens = evidence.get("tokens")
                if type(tokens) is not int or tokens < 0:
                    raise AppError(
                        code="COMPLETION_USAGE_REQUIRED",
                        message="verified completion must include bounded token usage",
                        category=ErrorCategory.INTEGRITY, status=409,
                    )
                row.status = ModelRequestStatus.COMPLETED.value
                row.actual_tokens = tokens
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

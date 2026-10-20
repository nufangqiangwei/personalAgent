"""Regression tests for the persistent model boundary (T104)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from personal_agent.adapters.harness.model_gateway import ModelGateway
from personal_agent.adapters.persistence.budget_models import ModelRequest
from personal_agent.domain.common import AppError, ErrorCategory, new_id
from personal_agent.ports.contracts import (
    ModelMessage,
    ModelResponse,
    Owner,
    Usage,
)
from personal_agent.ports.contracts import ModelRequest as Request


def request(clock: Any) -> Request:
    return Request(
        request_id=new_id(),
        owner=Owner(draft_id=new_id()),
        epoch=1,
        model_id="mock-model",
        messages=[ModelMessage(role="user", content="fixed observation")],
        output_limit=32,
        deadline=clock.now() + timedelta(seconds=5),
        reservation_id=new_id(),
    )


class HeldTransport:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def complete(self, spec: Request) -> ModelResponse:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return ModelResponse(
            status="completed", content="{}", usage=Usage(input_tokens=2),
            completion_evidence_ref=f"transport:{spec.request_id}",
        )


async def test_simultaneous_gateways_cannot_reserve_two_slots(
    database: Any, clock: Any
) -> None:
    transport = HeldTransport()
    barrier = asyncio.Barrier(2)

    class RacingGateway(ModelGateway):
        checked = False

        async def _find_non_terminal(self, session: Any) -> Any:
            result = await super()._find_non_terminal(session)
            if not self.checked:
                self.checked = True
                await asyncio.wait_for(barrier.wait(), timeout=5)
            return result

    gateways = [RacingGateway(database, clock, transport) for _ in range(2)]
    results = await asyncio.gather(
        *(gateway.reserve(request(clock), reserved_tokens=64) for gateway in gateways),
        return_exceptions=True,
    )
    winners = [result for result in results if not isinstance(result, BaseException)]
    assert len(winners) == 1, results
    losers = [result for result in results if isinstance(result, AppError)]
    assert len(losers) == 1 and losers[0].code == "MODEL_BUSY"


async def test_local_cancellation_persists_unknown(database: Any, clock: Any) -> None:
    transport = HeldTransport()
    gateway = ModelGateway(database, clock, transport)
    task = asyncio.create_task(
        gateway.request(request(clock), reserved_tokens=64, timeout_seconds=5)
    )
    await asyncio.wait_for(transport.entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    restarted = ModelGateway(database, clock, transport)
    row = await restarted.non_terminal_request()
    assert row is not None and row.status == "UNKNOWN"
    assert row.ended_at is None, "local cancellation is not proof of remote completion"
    with pytest.raises(AppError, match="unverified outcome"):
        await restarted.reserve(request(clock), reserved_tokens=64)
    assert transport.calls == 1


@pytest.mark.parametrize("failure", ["app_error", "missing_evidence"])
async def test_unproven_transport_completion_keeps_unknown(
    database: Any, clock: Any, failure: str
) -> None:
    class UncertainTransport:
        async def complete(self, spec: Request) -> ModelResponse:
            if failure == "app_error":
                raise AppError(
                    code="CONNECTION_LOST", message="connection lost",
                    category=ErrorCategory.DEPENDENCY,
                )
            return ModelResponse(status="completed", content="{}", usage=Usage(input_tokens=1))

    gateway = ModelGateway(database, clock, UncertainTransport())
    response = await gateway.request(request(clock), reserved_tokens=64, timeout_seconds=5)
    assert response.status == "unknown"
    row = await gateway.non_terminal_request()
    assert row is not None and row.status == "UNKNOWN"
    assert row.actual_tokens is None


async def test_request_identity_includes_content(database: Any, clock: Any) -> None:
    transport = HeldTransport()
    gateway = ModelGateway(database, clock, transport)
    first = request(clock)
    await gateway.reserve(first, reserved_tokens=64)
    await gateway.complete(first.request_id, usage=Usage(input_tokens=2), evidence_ref="fixture:1")
    second = replace(
        first, request_id=new_id(), messages=[ModelMessage(role="user", content="other")]
    )
    await gateway.reserve(second, reserved_tokens=64)
    async with database.session() as session:
        rows = (await session.scalars(select(ModelRequest).order_by(ModelRequest.id))).all()
    assert len({row.request_hash for row in rows}) == 2


async def test_unknown_usage_cannot_be_reconciled_as_zero(database: Any, clock: Any) -> None:
    gateway = ModelGateway(database, clock, HeldTransport())
    spec = request(clock)
    await gateway.reserve(spec, reserved_tokens=64)
    with pytest.raises(AppError) as error:
        await gateway.reconcile(spec.request_id, {"completed": True, "evidence_ref": "fixture:1"})
    assert error.value.code == "COMPLETION_USAGE_REQUIRED"
    assert await gateway.non_terminal_request() is not None


async def test_model_allowlist_rejects_before_transport(database: Any, clock: Any) -> None:
    transport = HeldTransport()
    gateway = ModelGateway(database, clock, transport, allowed_model_ids={"configured-model"})
    with pytest.raises(AppError) as error:
        await gateway.reserve(request(clock), reserved_tokens=64)
    assert error.value.code == "MODEL_NOT_ALLOWED"
    assert transport.calls == 0


def test_owner_cannot_mix_draft_and_run() -> None:
    with pytest.raises(ValueError):
        Owner(draft_id=new_id(), run_id=new_id())


async def test_authorized_gateway_rejects_missing_owner_and_budget(
    database: Any, clock: Any
) -> None:
    transport = HeldTransport()
    gateway = ModelGateway(database, clock, transport, enforce_owner=True)
    with pytest.raises(AppError) as error:
        await gateway.reserve(request(clock), reserved_tokens=64)
    assert error.value.code == "MODEL_OWNER_INVALID"
    assert transport.calls == 0


async def test_verified_failed_response_releases_slot_with_usage(database: Any, clock: Any) -> None:
    class VerifiedFailure:
        async def complete(self, spec: Request) -> ModelResponse:
            return ModelResponse(
                status="failed", usage=Usage(input_tokens=2),
                completion_evidence_ref=f"transport:{spec.request_id}",
                error=AppError(code="SOURCE_UNAVAILABLE", message="source unavailable"),
            )

    gateway = ModelGateway(database, clock, VerifiedFailure())
    response = await gateway.request(request(clock), reserved_tokens=64, timeout_seconds=5)
    assert response.status == "failed"
    assert await gateway.non_terminal_request() is None


async def test_unbound_mock_runtime_does_not_claim_gateway_enforcement(
    mock_runtime: Any, gateway: Any
) -> None:
    assert (await mock_runtime.capabilities()).model_gateway_enforced is False
    mock_runtime.bind_model_gateway(gateway)
    assert (await mock_runtime.capabilities()).model_gateway_enforced is True


def test_live_without_transport_never_falls_back_to_mock(data_dir: Any) -> None:
    from personal_agent.bootstrap.config import load_config
    from personal_agent.bootstrap.lifespan import _transport_for

    config = load_config(mode="live", data_dir=data_dir)
    with pytest.raises(AppError) as error:
        _transport_for(config, object())  # type: ignore[arg-type]
    assert error.value.code == "RUNTIME_INCOMPATIBLE"

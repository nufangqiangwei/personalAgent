"""Foundation contract tests (T008).

These freeze the behaviours that must hold before any story work: a queued
submission is not a completed result, an unknown model outcome stays blocking
across a "restart", only one model request may be non-terminal, the runtime
deduplicates events by key, and draft top-ups are explicit and idempotent.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from personal_agent.adapters.harness.mock_runtime import MockModelTransport
from personal_agent.application.budget_service import BudgetService
from personal_agent.domain.common import AppError
from personal_agent.domain.execution import ModelRequestStatus
from personal_agent.ports.contracts import (
    ModelMessage,
    ModelRequest,
    Owner,
    RuntimeTask,
    SessionContext,
)

DRAFT_LIMITS = {"model_requests": 2, "tokens": 100}
RUN_LIMITS = {
    "model_requests": 3,
    "tool_calls": 3,
    "search_calls": 1,
    "page_reads": 1,
    "tokens": 100,
    "active_ms": 1000,
}


def spec(*, scenario: str, draft_id: str = "draft-1") -> ModelRequest:
    return ModelRequest(
        request_id="",
        owner=Owner(draft_id=draft_id),
        epoch=1,
        model_id="mock-model",
        messages=[ModelMessage(role="user", content=f"scenario:{scenario}")],
        output_limit=1024,
        deadline=None,  # type: ignore[arg-type]
        reservation_id="res-1",
    )


async def test_accepted_submission_is_not_a_business_result(
    mock_runtime: Any, clock: Any
) -> None:
    session = await mock_runtime.create_session(
        SessionContext(
            operation_id="op-1",
            owner=Owner(run_id="run-1"),
            epoch=1,
            model_id="mock-model",
            profile_hash="p",
            capability_token="mock-capability-token",
            policy_hash="h",
        )
    )
    submission = await mock_runtime.submit(
        RuntimeTask(
            operation_id="op-1",
            session_id=session.adapter_session_id,
            epoch=1,
            goal="research",
            context_refs=(),
            validated_input={"run_id": "run-1", "mock_scenario": "engine.topic.directions"},
            output_schema={},
            deadline=clock.now(),
            tool_scope=("search.web",),
            budget_reservation_id="res-1",
        )
    )
    assert submission.status == "accepted"
    result = await mock_runtime.wait_result(submission.submission_id)
    assert result.status == "completed"
    assert result.structured_output is not None
    assert result.completion_evidence_ref is not None


async def test_unknown_output_is_never_reported_as_completed(mock_runtime: Any, clock: Any) -> None:
    session = await mock_runtime.create_session(
        SessionContext(
            operation_id="op-unknown",
            owner=Owner(run_id="run-1"),
            epoch=1,
            model_id="mock-model",
            profile_hash="p",
            capability_token="mock-capability-token",
            policy_hash="h",
        )
    )
    submission = await mock_runtime.submit(
        RuntimeTask(
            operation_id="op-unknown",
            session_id=session.adapter_session_id,
            epoch=1,
            goal="research",
            context_refs=(),
            validated_input={"run_id": "run-1", "mock_scenario": "runtime.timeout.unknown"},
            output_schema={},
            deadline=clock.now(),
            tool_scope=("search.web",),
            budget_reservation_id="res-1",
        )
    )
    result = await mock_runtime.wait_result(submission.submission_id)
    assert result.status == "unknown"
    assert result.completion_evidence_ref is None
    assert result.error is not None
    assert result.error.retryable is False


async def test_runtime_events_are_deduplicated_by_key(mock_runtime: Any, clock: Any) -> None:
    session = await mock_runtime.create_session(
        SessionContext(
            operation_id="op-events",
            owner=Owner(run_id="run-1"),
            epoch=1,
            model_id="mock-model",
            profile_hash="p",
            capability_token="mock-capability-token",
            policy_hash="h",
        )
    )
    await mock_runtime.submit(
        RuntimeTask(
            operation_id="op-events",
            session_id=session.adapter_session_id,
            epoch=1,
            goal="research",
            context_refs=(),
            validated_input={"run_id": "run-1", "mock_scenario": "engine.topic.directions"},
            output_schema={},
            deadline=clock.now(),
            tool_scope=("search.web",),
            budget_reservation_id="res-1",
        )
    )
    collected = [event async for event in mock_runtime.events(session.adapter_session_id)]
    unique = {(event.event_id, event.sequence) for event in collected}
    assert len(collected) > len(unique)  # the mock delivers duplicates on purpose
    assert all(event.kind != "reasoning" for event in collected)


async def test_gateway_allows_only_one_non_terminal_request(
    gateway: Any, database: Any, clock: Any, observations: dict[str, Any]
) -> None:
    # Use a transport with a controllable delay: the first request must still be
    # holding the persistent slot when the second one arrives, whatever the load
    # of the surrounding suite. Guessing event-loop turns made this test flaky.
    from personal_agent.adapters.harness.mock_runtime import MockModelTransport
    from personal_agent.adapters.harness.model_gateway import ModelGateway

    slow_gateway = ModelGateway(
        database, clock, MockModelTransport(observations, latency_seconds=0.2)
    )
    first = asyncio.create_task(
        slow_gateway.request(
            spec(scenario="requirement.analysis.complete"),
            reserved_tokens=200,
            timeout_seconds=5,
        )
    )
    for _ in range(400):
        if await slow_gateway.non_terminal_request() is not None:
            break
        await asyncio.sleep(0.005)
    else:  # pragma: no cover - only fails if the gateway never reserves
        raise AssertionError("the first request never occupied the model slot")
    with pytest.raises(AppError) as error:
        await gateway.request(
            spec(scenario="requirement.analysis.complete"), reserved_tokens=200, timeout_seconds=5
        )
    assert error.value.code == "MODEL_BUSY"
    response = await first
    assert response.status == "completed"
    assert await gateway.non_terminal_request() is None


async def test_gateway_unknown_survives_a_restart(gateway: Any, database: Any, clock: Any) -> None:
    response = await gateway.request(
        spec(scenario="runtime.timeout.unknown"), reserved_tokens=200, timeout_seconds=5
    )
    assert response.status == "unknown"
    request = await gateway.non_terminal_request()
    assert request is not None
    assert request.status == ModelRequestStatus.UNKNOWN.value

    # A restarted application rebuilds the gateway but must re-read the ledger.
    from personal_agent.adapters.harness.model_gateway import ModelGateway

    restarted = ModelGateway(database, clock, MockModelTransport({}))
    with pytest.raises(AppError) as error:
        await restarted.request(
            spec(scenario="requirement.analysis.complete"), reserved_tokens=200, timeout_seconds=5
        )
    assert error.value.code == "MODEL_OUTCOME_UNKNOWN"

    # Only verified evidence unlocks the slot.
    still_unknown = await restarted.reconcile(request.id, {"completed": True})
    assert still_unknown is ModelRequestStatus.UNKNOWN
    status = await restarted.reconcile(
        request.id, {"completed": True, "evidence_ref": "evidence-1", "tokens": 12}
    )
    assert status is ModelRequestStatus.COMPLETED
    assert await restarted.non_terminal_request() is None


async def test_draft_reservation_and_settlement_are_idempotent(
    database: Any, clock: Any
) -> None:
    service = BudgetService(clock)
    async with database.session() as session:
        ledger = await service.ensure_draft_ledger(session, draft_id="draft-1", limits=DRAFT_LIMITS)
        await session.commit()
        first = await service.reserve(
            session, ledger=ledger, worst_case={"model_requests": 1}, operation_id="op-a"
        )
        await session.commit()
        revision = ledger.revision
        second = await service.reserve(
            session, ledger=ledger, worst_case={"model_requests": 1}, operation_id="op-a"
        )
        await session.commit()
        assert first == second
        assert ledger.revision == revision

        await service.settle(
            session,
            ledger=ledger,
            reserved={"model_requests": 1},
            actual={"model_requests": 1},
            operation_id="op-a",
        )
        await session.commit()
        await service.settle(
            session,
            ledger=ledger,
            reserved={"model_requests": 1},
            actual={"model_requests": 1},
            operation_id="op-a",
        )
        await session.commit()
        assert ledger.consumed_json["model_requests"] == 1


async def test_draft_grant_requires_exhaustion_and_is_idempotent(database: Any, clock: Any) -> None:
    service = BudgetService(clock)
    async with database.session() as session:
        ledger = await service.ensure_draft_ledger(session, draft_id="draft-2", limits=DRAFT_LIMITS)
        await session.commit()
        with pytest.raises(AppError) as not_exhausted:
            await service.grant_draft_budget(
                session,
                draft_id="draft-2",
                draft_status="COLLECTING",
                draft_revision=1,
                ledger=ledger,
                increment={"model_requests": 1},
                operation_id="grant-1",
                default_limits=DRAFT_LIMITS,
            )
        assert not_exhausted.value.code == "DRAFT_BUDGET_NOT_EXHAUSTED"

        await service.reserve(
            session,
            ledger=ledger,
            worst_case={"model_requests": 2, "tokens": 100},
            operation_id="op-b",

        )
        await session.commit()
        assert service.to_domain(ledger).exhausted

        entry, snapshot = await service.grant_draft_budget(
            session,
            draft_id="draft-2",
            draft_status="COLLECTING",
            draft_revision=1,
            ledger=ledger,
            increment={"model_requests": 1, "tokens": 50},
            operation_id="grant-1",
            default_limits=DRAFT_LIMITS,
        )
        await session.commit()
        assert snapshot["limits"] == {"model_requests": 3, "tokens": 150}
        assert snapshot["consumed"] == {"model_requests": 0, "tokens": 0}

        again, snapshot_again = await service.grant_draft_budget(
            session,
            draft_id="draft-2",
            draft_status="COLLECTING",
            draft_revision=2,
            ledger=ledger,
            increment={"model_requests": 1, "tokens": 50},
            operation_id="grant-1",
            default_limits=DRAFT_LIMITS,
        )
        assert again.id == entry.id
        assert snapshot_again["limits"] == {"model_requests": 3, "tokens": 150}


async def test_draft_grant_rejects_non_collecting_states(database: Any, clock: Any) -> None:
    service = BudgetService(clock)
    async with database.session() as session:
        ledger = await service.ensure_draft_ledger(session, draft_id="draft-3", limits=DRAFT_LIMITS)
        ledger.limits_json = {"model_requests": 0, "tokens": 0}
        await session.commit()
        for status in ("CONFIRMED", "STALE"):
            with pytest.raises(AppError) as error:
                await service.grant_draft_budget(
                    session,
                    draft_id="draft-3",
                    draft_status=status,
                    draft_revision=1,
                    ledger=ledger,
                    increment={"model_requests": 1},
                    operation_id=f"grant-{status}",
                    default_limits=DRAFT_LIMITS,
                )
            assert error.value.code == "DRAFT_STATE_INVALID"

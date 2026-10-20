"""Per-call model metering: one reservation per Loop call, real settlement."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from personal_agent.adapters.persistence.budget_models import BudgetLedger
from personal_agent.application.budget_service import BudgetService
from personal_agent.application.model_meter import RunModelMeter, RunToolMeter
from personal_agent.domain.common import AppError, new_id
from personal_agent.ports.contracts import Usage

LIMITS = {
    "model_requests": 2, "tokens": 1000, "tool_calls": 5,
    "search_calls": 5, "page_reads": 5,
}


async def _ledger(database: Any, root_run_id: str = "root-run") -> None:
    async with database.session() as session:
        session.add(BudgetLedger(
            id=new_id(), root_run_id=root_run_id, draft_id=None,
            limits_json=dict(LIMITS),
            consumed_json={key: 0 for key in LIMITS},
            reserved_json={key: 0 for key in LIMITS},
            active_ms=0, revision=1,
        ))
        await session.commit()


async def _row(database: Any) -> BudgetLedger:
    async with database.session() as session:
        row = await session.scalar(
            select(BudgetLedger).where(BudgetLedger.root_run_id == "root-run")
        )
    assert row is not None
    return row


async def test_each_call_reserves_separately_and_settles_real_usage(
    database: Any, clock: Any,
) -> None:
    await _ledger(database)
    meter = RunModelMeter(database, BudgetService(clock))
    first = await meter.begin(
        root_run_id="root-run", attempt_id="attempt", output_limit=10, estimated_input=40,
    )
    second = await meter.begin(
        root_run_id="root-run", attempt_id="attempt", output_limit=10, estimated_input=60,
    )
    assert first.reservation_id == "attempt:model:1"
    assert second.reservation_id == "attempt:model:2"
    assert (first.reserved_tokens, second.reserved_tokens) == (50, 70)

    await meter.settle(
        root_run_id="root-run", grant=first,
        usage=Usage(input_tokens=5, output_tokens=2, model_requests=1),
    )
    row = await _row(database)
    assert row.consumed_json["model_requests"] == 1
    # Actual usage above the reservation is charged; below it keeps the reserve.
    assert row.consumed_json["tokens"] == 50
    # The second call is still in flight, so its reservation stays held.
    assert row.reserved_json["model_requests"] == 1
    assert row.reserved_json["tokens"] == 70

    with pytest.raises(AppError) as error:
        await meter.begin(
            root_run_id="root-run", attempt_id="attempt",
            output_limit=10, estimated_input=10,
        )
    assert error.value.code == "BUDGET_ASSISTANCE_REQUIRED"


async def test_release_returns_an_unused_reservation(database: Any, clock: Any) -> None:
    await _ledger(database)
    meter = RunModelMeter(database, BudgetService(clock))
    grant = await meter.begin(
        root_run_id="root-run", attempt_id="attempt", output_limit=10, estimated_input=20,
    )
    await meter.release(root_run_id="root-run", grant=grant)
    row = await _row(database)
    assert row.reserved_json == {
        "model_requests": 0, "tokens": 0, "tool_calls": 0,
        "search_calls": 0, "page_reads": 0,
    }
    assert row.consumed_json["model_requests"] == 0


async def test_tool_calls_reserve_their_own_dimension(database: Any, clock: Any) -> None:
    await _ledger(database)
    meter = RunToolMeter(database, BudgetService(clock))
    page = await meter.begin(
        root_run_id="root-run", attempt_id="attempt", tool_name="http.fetch",
    )
    search = await meter.begin(
        root_run_id="root-run", attempt_id="attempt", tool_name="search.web",
    )
    assert page.reserved == {"tool_calls": 1, "page_reads": 1}
    assert search.reserved == {"tool_calls": 1, "search_calls": 1}
    assert page.reservation_id != search.reservation_id

    await meter.settle(root_run_id="root-run", grant=page)
    # A refused-before-execution call returns its reservation untouched.
    await meter.release(root_run_id="root-run", grant=search)
    row = await _row(database)
    assert row.consumed_json["tool_calls"] == 1
    assert row.consumed_json["page_reads"] == 1
    assert row.consumed_json["search_calls"] == 0
    assert row.reserved_json["tool_calls"] == 0
    assert row.reserved_json["search_calls"] == 0

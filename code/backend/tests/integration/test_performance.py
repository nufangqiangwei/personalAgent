"""Performance baseline for the run read paths at 10,000 execution records (T099).

The fixture seeds the database directly instead of driving 10,000 HTTP requests:
one ``WorkflowRun`` per record plus the rows its foreign keys require (a confirmed
``RequirementDraft`` and its ``Confirmation``) and one ``BudgetLedger``. The newest
records carry steps and attempts so the detail and step endpoints do real work.

All measurements go through the normal ASGI ``client`` fixture (HTTP routing,
middleware, serialization, SQLite); no model call is involved. The thresholds are

* list, detail and steps: p95 < 500 ms over 20 samples;
* pause (stop intent) response: < 1 s on a running run with no in-flight action.

A list that read every row before paginating was the measured bottleneck at this
size; it now pages in SQL. The pre- and post-fix numbers are recorded in
``docs/performance.md``.
"""

from __future__ import annotations

import math
import statistics
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select

from personal_agent.adapters.persistence.budget_models import BudgetLedger
from personal_agent.adapters.persistence.execution_models import (
    StepAttempt,
    StepRun,
    WorkflowRun,
)
from personal_agent.adapters.persistence.requirement_models import (
    Confirmation,
    RequirementDraft,
)
from personal_agent.adapters.persistence.workflow_models import WorkflowVersion
from personal_agent.bootstrap.seed import seed
from personal_agent.domain.common import new_id

BASELINE_RUNS = 10_000
SAMPLES = 20
LIST_DETAIL_STEPS_THRESHOLD = 0.5
STOP_THRESHOLD = 1.0


def _record_id(kind: int, index: int) -> str:
    """Stable 36-character id; kind keeps the four row families distinct."""
    return f"{kind:08d}-0000-4000-8000-{index:012d}"


async def _seed_records(
    api_context: Any,
    count: int,
    *,
    running_indices: frozenset[int] = frozenset(),
) -> None:
    """Bulk-insert ``count`` executions with the minimum required related rows."""
    _config, context = api_context
    await seed(context.database)
    async with context.database.session() as session:
        version_id = await session.scalar(
            select(WorkflowVersion.id)
            .where(WorkflowVersion.executable.is_(True))
            .order_by(WorkflowVersion.saved_at)
            .limit(1)
        )
        assert version_id is not None
        base = datetime(2026, 1, 1, tzinfo=UTC)
        drafts: list[RequirementDraft] = []
        confirmations: list[Confirmation] = []
        runs: list[WorkflowRun] = []
        ledgers: list[BudgetLedger] = []
        for index in range(count):
            when = base + timedelta(milliseconds=index)
            draft_id = _record_id(1, index)
            confirmation_id = _record_id(2, index)
            run_id = _record_id(3, index)
            running = index in running_indices
            drafts.append(
                RequirementDraft(
                    id=draft_id,
                    selected_version_id=version_id,
                    revision=2,
                    status="CONFIRMED",
                    missing_fields_json=[],
                    conflicts_json=[],
                    field_values_json={},
                    preference_order_json=[],
                    output_spec_json={},
                    summary_markdown="",
                    summary_hash="",
                    source_draft_id=None,
                    created_at=when,
                    updated_at=when,
                )
            )
            confirmations.append(
                Confirmation(
                    id=confirmation_id,
                    draft_id=draft_id,
                    draft_revision=1,
                    workflow_version_id=version_id,
                    summary_hash="",
                    input_hash="sha256:baseline",
                    budget_policy_hash="policy",
                    data_policy_version="1",
                    confirmed_at=when,
                    action="START",
                    field_snapshot_json={},
                )
            )
            runs.append(
                WorkflowRun(
                    id=run_id,
                    root_run_id=run_id,
                    parent_run_id=None,
                    fork_step_id=None,
                    workflow_version_id=version_id,
                    confirmation_id=confirmation_id,
                    input_snapshot_json={},
                    input_hash="sha256:baseline",
                    config_fingerprint="config",
                    runtime_fingerprint="runtime",
                    budget_policy_hash="policy",
                    data_policy_version="1",
                    status="RUNNING" if running else "SUCCEEDED",
                    reason_code=None,
                    current_step_id=None if running else "write_report",
                    epoch=1,
                    revision=1,
                    mode="FIXED",
                    completeness=None if running else "COMPLETE",
                    active_elapsed_ms=0,
                    cancel_requested=False,
                    created_at=when,
                    updated_at=when,
                )
            )
            ledgers.append(
                BudgetLedger(
                    id=_record_id(4, index),
                    root_run_id=run_id,
                    draft_id=None,
                    limits_json={"model_requests": 30, "tokens": 100_000},
                    consumed_json={"model_requests": 1, "tokens": 100},
                    reserved_json={},
                    active_ms=0,
                    revision=1,
                )
            )
        # The unit of work cannot order these inserts by itself (no ORM
        # relationships exist), so flush each level explicitly.
        session.add_all(drafts)
        await session.flush()
        session.add_all(confirmations)
        await session.flush()
        session.add_all(runs)
        await session.flush()

        detail_id = _record_id(3, count - 2)
        step_rows: list[StepRun] = []
        attempt_rows: list[StepAttempt] = []
        for offset, step_id in enumerate(
            ("collect_background", "collect_latest", "merge_findings")
        ):
            step_run_id = _record_id(5, offset)
            step_rows.append(
                StepRun(
                    id=step_run_id,
                    run_id=detail_id,
                    step_id=step_id,
                    status="SUCCEEDED",
                    current_attempt_no=1,
                    selected_transition="success",
                    skip_reason=None,
                    revision=2,
                    created_at=base,
                    updated_at=base,
                )
            )
            attempt_rows.append(
                StepAttempt(
                    id=_record_id(6, offset),
                    step_run_id=step_run_id,
                    attempt_no=1,
                    epoch=1,
                    status="SUCCEEDED",
                    input_json={},
                    input_hash="sha256:baseline",
                    output_json={"ok": True},
                    output_hash="sha256:baseline",
                    started_at=base,
                    ended_at=base,
                    error_category=None,
                    retry_reason=None,
                    session_link_id=None,
                )
            )
        session.add_all(step_rows)
        await session.flush()
        session.add_all(attempt_rows)

        session.add_all(ledgers)
        await session.commit()


async def _timed_get(client: httpx.AsyncClient, url: str) -> tuple[httpx.Response, float]:
    started = time.perf_counter()
    response = await client.get(url)
    return response, time.perf_counter() - started


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def _summary(name: str, samples: list[float]) -> str:
    return (
        f"{name}: n={len(samples)} "
        f"p50={statistics.median(samples) * 1000:.0f}ms "
        f"p95={_percentile(samples, 0.95) * 1000:.0f}ms "
        f"min={min(samples) * 1000:.0f}ms max={max(samples) * 1000:.0f}ms"
    )


async def test_run_read_paths_stay_within_500ms_p95_at_10k_records(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    await _seed_records(api_context, BASELINE_RUNS)
    _config, context = api_context
    async with context.database.session() as session:
        total = await session.scalar(select(func.count()).select_from(WorkflowRun))
    assert total == BASELINE_RUNS, f"seeded {total} runs instead of {BASELINE_RUNS}"
    detail_id = _record_id(3, BASELINE_RUNS - 2)

    warmup = await client.get("/api/v1/runs?limit=20")
    assert warmup.status_code == 200, warmup.text
    assert len(warmup.json()["items"]) == 20
    first_cursor = warmup.json()["next_cursor"]
    assert first_cursor
    second_page = (
        await client.get(f"/api/v1/runs?limit=20&cursor={first_cursor}")
    ).json()
    assert len(second_page["items"]) == 20
    assert second_page["next_cursor"]
    assert {item["id"] for item in second_page["items"]}.isdisjoint(
        item["id"] for item in warmup.json()["items"]
    )
    invalid_cursor = await client.get("/api/v1/runs?limit=20&cursor=not-a-number")
    assert invalid_cursor.status_code == 400
    assert invalid_cursor.json()["error"]["code"] == "INVALID_CURSOR"
    await client.get(f"/api/v1/runs/{detail_id}")
    await client.get(f"/api/v1/runs/{detail_id}/steps")

    list_samples: list[float] = []
    for _ in range(SAMPLES):
        response, elapsed = await _timed_get(client, "/api/v1/runs?limit=20")
        assert response.status_code == 200, response.text
        list_samples.append(elapsed)

    detail_samples: list[float] = []
    for _ in range(SAMPLES):
        response, elapsed = await _timed_get(client, f"/api/v1/runs/{detail_id}")
        assert response.status_code == 200, response.text
        assert response.json()["id"] == detail_id
        detail_samples.append(elapsed)

    steps_samples: list[float] = []
    for _ in range(SAMPLES):
        response, elapsed = await _timed_get(client, f"/api/v1/runs/{detail_id}/steps")
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 3
        steps_samples.append(elapsed)

    print("\nT099 10k read paths (no model calls)", flush=True)
    print("  " + _summary("list   ", list_samples), flush=True)
    print("  " + _summary("detail ", detail_samples), flush=True)
    print("  " + _summary("steps  ", steps_samples), flush=True)

    for name, samples in (
        ("list", list_samples),
        ("detail", detail_samples),
        ("steps", steps_samples),
    ):
        p95 = _percentile(samples, 0.95)
        assert p95 < LIST_DETAIL_STEPS_THRESHOLD, (
            f"{name} p95 {p95 * 1000:.0f}ms exceeds "
            f"{LIST_DETAIL_STEPS_THRESHOLD * 1000:.0f}ms at {BASELINE_RUNS} runs"
        )


async def test_pause_returns_within_one_second_without_in_flight_action(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    count = 5
    await _seed_records(api_context, count, running_indices=frozenset(range(count)))

    samples: list[float] = []
    for index in range(count):
        run_id = _record_id(3, index)
        snapshot = (await client.get(f"/api/v1/runs/{run_id}")).json()
        assert snapshot["status"] == "RUNNING"
        assert snapshot["in_flight_actions"] == [], "the stop fixture must have no in-flight action"

        started = time.perf_counter()
        response = await client.post(
            f"/api/v1/runs/{run_id}/pause",
            json={"revision": snapshot["revision"]},
            headers={
                "Idempotency-Key": new_id(),
                "If-Match": f'"{snapshot["revision"]}"',
            },
        )
        elapsed = time.perf_counter() - started
        assert response.status_code == 200, response.text
        paused = response.json()
        assert paused["status"] == "PAUSED"
        assert paused["draining"] is False
        samples.append(elapsed)

    print("\nT099 stop intent (pause, no in-flight action)", flush=True)
    print("  " + _summary("pause  ", samples), flush=True)
    assert max(samples) < STOP_THRESHOLD, (
        f"pause took {max(samples) * 1000:.0f}ms, over the {STOP_THRESHOLD * 1000:.0f}ms budget"
    )

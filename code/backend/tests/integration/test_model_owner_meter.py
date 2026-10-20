"""Production owner path: per-call reservations are authorized for a live run.

The mock engine holds a real attempt RUNNING in the execution slot. A model call
is then issued through the production ``enforce_owner`` gateway with the meter's
per-call reservation, exactly as the CLI proxy does. This proves the second
Loop call is not rejected by the attempt-level reservation check.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select

from personal_agent.adapters.harness.model_gateway import ModelGateway, ProviderOutcome
from personal_agent.adapters.persistence.budget_models import BudgetLedger
from personal_agent.adapters.persistence.execution_models import StepAttempt, WorkflowRun
from personal_agent.adapters.persistence.requirement_models import (
    Confirmation,
    RequirementDraft,
)
from personal_agent.application.budget_service import BudgetService
from personal_agent.application.model_meter import RunModelMeter
from personal_agent.application.workflow_registry import WorkflowRegistry
from personal_agent.bootstrap.seed import seed
from personal_agent.domain.common import new_id, stable_hash
from personal_agent.ports.contracts import ModelMessage, Owner, Usage
from personal_agent.ports.contracts import ModelRequest as ModelRequestSpec

TOPIC_WORKFLOW = "公开主题资料研究"


class HeldRuntime:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def submit(self, task: Any) -> Any:
        self.entered.set()
        await self.release.wait()
        return await self._inner.submit(task)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _confirmation(api_context: Any, topic: str) -> str:
    config, context = api_context
    await seed(context.database)
    registry = WorkflowRegistry(context.database, context.clock)
    summaries = await registry.list_summaries()
    version_id = next(
        item["latest_version"]["id"] for item in summaries if item["name"] == TOPIC_WORKFLOW
    )
    field_values = {
        "topic": {
            "path": "topic", "value": topic, "source": "user", "message_id": None,
            "suggestion_id": None, "accepted_at": None, "explicit_none": False,
        }
    }
    snapshot = {"fields": field_values, "preference_order": [], "output_spec": {}}
    async with context.database.session() as session:
        draft = RequirementDraft(
            id=new_id(), selected_version_id=version_id, revision=2, status="CONFIRMED",
            missing_fields_json=[], conflicts_json=[], field_values_json=field_values,
            preference_order_json=[], output_spec_json={}, summary_markdown="",
            summary_hash="sha256:summary", source_draft_id=None,
            created_at=context.clock.now(), updated_at=context.clock.now(),
        )
        session.add(draft)
        await session.flush()
        confirmation = Confirmation(
            id=new_id(), draft_id=draft.id, draft_revision=1,
            workflow_version_id=version_id, summary_hash="sha256:summary",
            input_hash=stable_hash(snapshot), budget_policy_hash=config.policy.policy_hash,
            data_policy_version=config.policy.version, confirmed_at=context.clock.now(),
            action="START", field_snapshot_json=snapshot,
        )
        session.add(confirmation)
        await session.commit()
        return confirmation.id


class FinishedTransport:
    async def complete(self, spec: Any) -> ProviderOutcome:
        return ProviderOutcome(
            status="completed", payload={"text": "{}"},
            usage=Usage(input_tokens=2, output_tokens=1, model_requests=1),
            completion_evidence_ref=f"owner:{spec.request_id}",
        )


async def test_per_call_reservation_is_authorized_on_a_running_attempt(
    client: httpx.AsyncClient, api_context: Any,
) -> None:
    _, context = api_context
    held = HeldRuntime(context.runtime)
    context.engine._deps.runtime = held  # type: ignore[attr-defined]
    confirmation_id = await _confirmation(api_context, "图书馆夜间开放")
    started = await client.post(
        "/api/v1/runs", json={"confirmation_id": confirmation_id},
        headers={"Idempotency-Key": new_id()},
    )
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    await asyncio.wait_for(held.entered.wait(), timeout=10)

    async with context.database.session() as session:
        run = await session.get(WorkflowRun, run_id)
        attempt = await session.scalar(
            select(StepAttempt).where(
                StepAttempt.status == "RUNNING", StepAttempt.epoch == 1,
            ).order_by(StepAttempt.started_at.desc())
        )
    assert run is not None and run.status == "RUNNING"
    assert attempt is not None and attempt.epoch == run.epoch

    meter = RunModelMeter(context.database, BudgetService(context.clock))
    grant = await meter.begin(
        root_run_id=run.root_run_id, attempt_id=attempt.id,
        output_limit=10, estimated_input=20,
    )
    transport = FinishedTransport()
    gateway = ModelGateway(
        context.database, context.clock, transport,
        enforce_owner=True, allowed_model_ids={"mock-deepseek-phase1"},
    )
    spec = ModelRequestSpec(
        request_id=new_id(),
        owner=Owner(run_id=run.id, attempt_id=attempt.id),
        epoch=run.epoch,
        model_id="mock-deepseek-phase1",
        messages=[ModelMessage(role="user", content="fixed")],
        output_limit=10,
        deadline=context.clock.now() + timedelta(seconds=5),
        reservation_id=grant.reservation_id,
    )
    outcome = await gateway.exchange(
        spec, reserved_tokens=grant.reserved_tokens, timeout_seconds=5,
        send=transport.complete,
    )
    assert outcome.status == "completed", outcome.error
    await meter.settle(root_run_id=run.root_run_id, grant=grant, usage=outcome.usage)

    async with context.database.session() as session:
        ledger = await session.scalar(
            select(BudgetLedger).where(BudgetLedger.root_run_id == run.root_run_id)
        )
    assert ledger is not None
    # This call settled; the engine's own attempt reservation is still held.
    assert ledger.consumed_json["model_requests"] == 1
    assert ledger.reserved_json["model_requests"] == 1

    held.release.set()
    assert await context.executor.wait(run_id, max_wait=60) == "SUCCEEDED"
    async with context.database.session() as session:
        ledger = await session.scalar(
            select(BudgetLedger).where(BudgetLedger.root_run_id == run.root_run_id)
        )
    assert ledger is not None and ledger.consumed_json["model_requests"] >= 2
    assert ledger.reserved_json["model_requests"] == 0

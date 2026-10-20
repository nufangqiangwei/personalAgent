"""Serial execution and single-branch guarantees (T073).

Every case counts the downstream facts instead of trusting the UI: at most one
execution branch may advance at a time, at most one non-terminal model request may
exist, and a stop, chat message or second click may never start another action.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import replace
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from personal_agent.adapters.persistence.budget_models import ModelRequest
from personal_agent.adapters.persistence.execution_models import (
    StepAttempt,
    WorkflowRun,
)
from personal_agent.adapters.persistence.requirement_models import (
    Confirmation,
    RequirementDraft,
)
from personal_agent.application.workflow_registry import WorkflowRegistry
from personal_agent.bootstrap.seed import seed
from personal_agent.domain.common import AppError, ErrorCategory, new_id, stable_hash
from personal_agent.ports.contracts import ModelMessage, Owner
from personal_agent.ports.contracts import ModelRequest as ModelRequestSpec

TOPIC_WORKFLOW = "公开主题资料研究"


class SerialGate:
    """Counts submissions and can hold the first one open or fail it once."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block = True
        self.submits = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self.fail_first = False
        self._failures = 0

    async def submit(self, task: Any) -> Any:
        self.submits += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.fail_first and self._failures == 0:
                self._failures += 1
                raise AppError(
                    code="MODEL_TIMEOUT",
                    message="the model call timed out",
                    category=ErrorCategory.TIMEOUT,
                    status=504,
                )
            self.entered.set()
            if self.block:
                await self.release.wait()
            return await self._inner.submit(task)
        finally:
            self.concurrent -= 1

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
            "path": "topic",
            "value": topic,
            "source": "user",
            "message_id": None,
            "suggestion_id": None,
            "accepted_at": None,
            "explicit_none": False,
        }
    }
    snapshot = {"fields": field_values, "preference_order": [], "output_spec": {}}
    async with context.database.session() as session:
        draft = RequirementDraft(
            id=new_id(),
            selected_version_id=version_id,
            revision=2,
            status="CONFIRMED",
            missing_fields_json=[],
            conflicts_json=[],
            field_values_json=field_values,
            preference_order_json=[],
            output_spec_json={},
            summary_markdown="",
            summary_hash="sha256:summary",
            source_draft_id=None,
            created_at=context.clock.now(),
            updated_at=context.clock.now(),
        )
        session.add(draft)
        await session.flush()
        confirmation = Confirmation(
            id=new_id(),
            draft_id=draft.id,
            draft_revision=1,
            workflow_version_id=version_id,
            summary_hash="sha256:summary",
            input_hash=stable_hash(snapshot),
            budget_policy_hash=config.policy.policy_hash,
            data_policy_version=config.policy.version,
            confirmed_at=context.clock.now(),
            action="START",
            field_snapshot_json=snapshot,
        )
        session.add(confirmation)
        await session.commit()
        return confirmation.id


async def _start(client: httpx.AsyncClient, confirmation_id: str) -> httpx.Response:
    return await client.post(
        "/api/v1/runs",
        json={"confirmation_id": confirmation_id},
        headers={"Idempotency-Key": new_id()},
    )


async def _count(api_context: Any, model: Any) -> int:
    _, context = api_context
    async with context.database.session() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _running_attempts(api_context: Any) -> int:
    _, context = api_context
    async with context.database.session() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(StepAttempt)
                .where(StepAttempt.status == "RUNNING")
            )
            or 0
        )


async def test_double_click_creates_one_run_and_one_model_request(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = SerialGate(context.runtime)
    context.engine._deps.runtime = gate  # type: ignore[attr-defined]
    confirmation_id = await _confirmation(api_context, "图书馆夜间开放")

    first, second = await asyncio.gather(
        _start(client, confirmation_id), _start(client, confirmation_id)
    )
    assert first.status_code in {200, 201}, first.text
    assert second.status_code in {200, 201}, second.text
    assert first.json()["id"] == second.json()["id"]
    assert await _count(api_context, WorkflowRun) == 1

    await asyncio.wait_for(gate.entered.wait(), timeout=10)
    gate.release.set()
    assert await context.executor.wait(first.json()["id"], max_wait=60) == "SUCCEEDED"
    assert gate.submits >= 1
    assert gate.max_concurrent == 1
    assert await _count(api_context, WorkflowRun) == 1


async def test_research_model_requests_have_attempts_and_nonempty_downstream_samples(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    confirmation_id = await _confirmation(api_context, "图书馆夜间开放")
    started = await _start(client, confirmation_id)
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    assert await context.executor.wait(run_id, max_wait=60) == "SUCCEEDED"
    async with context.database.session() as session:
        requests = list((await session.scalars(
            select(ModelRequest).where(
                ModelRequest.owner_type == "RUN", ModelRequest.owner_id == run_id
            )
        )).all())
        attempts = set((await session.scalars(select(StepAttempt.id))).all())
    assert requests, "a successful research must contain actual downstream model requests"
    assert all(row.attempt_id in attempts for row in requests)
    assert all(row.status == "COMPLETED" and row.completion_evidence_ref for row in requests)
    assert all(row.ended_at is not None for row in requests)

    downstream = context.gateway._transport.calls
    assert downstream, "submit counters cannot substitute for actual model calls"
    assert {row.id for row in requests} <= {call.request_id for call in downstream}


async def test_actual_downstream_is_occupied_and_rejects_second_request(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context

    class DownstreamGate:
        def __init__(self, inner: Any) -> None:
            self.inner = inner
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.calls: list[Any] = []
            self.active = 0
            self.max_active = 0

        async def complete(self, spec: Any) -> Any:
            self.calls.append(spec)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.entered.set()
            try:
                await self.release.wait()
                return await self.inner.complete(spec)
            finally:
                self.active -= 1

    transport = DownstreamGate(context.gateway._transport)
    context.gateway._transport = transport
    confirmation_id = await _confirmation(api_context, "图书馆夜间开放")
    started = await _start(client, confirmation_id)
    assert started.status_code == 201, started.text
    await asyncio.wait_for(transport.entered.wait(), timeout=10)
    occupied = await context.gateway.non_terminal_request()
    assert occupied is not None and occupied.status == "IN_FLIGHT"
    assert occupied.owner_id == started.json()["id"]
    original = transport.calls[0]
    assert occupied.attempt_id == original.owner.attempt_id
    for changed in (
        replace(original, request_id=new_id(), epoch=original.epoch + 1),
        replace(original, request_id=new_id(), owner=Owner(
            run_id=original.owner.run_id, attempt_id=new_id()
        )),
    ):
        with pytest.raises(AppError) as invalid_owner:
            await context.gateway.request(
                changed, reserved_tokens=occupied.reserved_tokens, timeout_seconds=5
            )
        assert invalid_owner.value.code == "MODEL_OWNER_INVALID"
    with pytest.raises(AppError) as invalid_reservation:
        await context.gateway.request(
            replace(original, request_id=new_id(), reservation_id=new_id()),
            reserved_tokens=occupied.reserved_tokens, timeout_seconds=5,
        )
    assert invalid_reservation.value.code == "MODEL_RESERVATION_INVALID"
    with pytest.raises(AppError) as blocked:
        await context.gateway.request(
            replace(original, request_id=new_id()), reserved_tokens=occupied.reserved_tokens,
            timeout_seconds=5,
        )
    assert blocked.value.code == "MODEL_BUSY"
    assert len(transport.calls) == 1
    transport.release.set()
    assert await context.executor.wait(started.json()["id"], max_wait=60) == "SUCCEEDED"
    assert len(transport.calls) >= 2
    assert transport.max_active == 1
    completed_calls = len(transport.calls)
    with pytest.raises(AppError):
        await context.gateway.request(
            replace(original, request_id=new_id()), reserved_tokens=occupied.reserved_tokens,
            timeout_seconds=5,
        )
    assert len(transport.calls) == completed_calls

async def test_second_task_is_refused_while_the_slot_is_held(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = SerialGate(context.runtime)
    context.engine._deps.runtime = gate  # type: ignore[attr-defined]
    first_confirmation = await _confirmation(api_context, "图书馆夜间开放")
    second_confirmation = await _confirmation(api_context, "城市夜间公交")

    started = await _start(client, first_confirmation)
    assert started.status_code == 201, started.text
    await asyncio.wait_for(gate.entered.wait(), timeout=10)

    refused = await _start(client, second_confirmation)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "RUN_BUSY"
    assert await _count(api_context, WorkflowRun) == 1
    assert gate.submits == 1

    gate.release.set()
    assert await context.executor.wait(started.json()["id"], max_wait=60) == "SUCCEEDED"

    # Once the slot is free the second confirmed task may start.
    started_second = await _start(client, second_confirmation)
    assert started_second.status_code == 201, started_second.text
    gate.block = False
    gate.release.set()
    assert await context.executor.wait(started_second.json()["id"], max_wait=60) == "SUCCEEDED"
    assert await _count(api_context, WorkflowRun) == 2


async def test_branch_confirmation_is_refused_while_another_execution_runs(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = SerialGate(context.runtime)
    gate.block = False
    context.engine._deps.runtime = gate  # type: ignore[attr-defined]
    parent_confirmation = await _confirmation(api_context, "图书馆夜间开放")
    parent = await _start(client, parent_confirmation)
    assert parent.status_code == 201, parent.text
    parent_id = parent.json()["id"]
    assert await context.executor.wait(parent_id, max_wait=60) == "SUCCEEDED"

    # Validate a branch that only adds an unread field, so it is READY.
    proposal = await client.post(
        f"/api/v1/runs/{parent_id}/branch-proposals",
        json={"selected_step_id": "write_report"},
        headers={"Idempotency-Key": new_id()},
    )
    assert proposal.status_code == 201, proposal.text
    proposal_id = proposal.json()["id"]
    draft_id = proposal.json()["draft_id"]
    version_id = (await client.get(f"/api/v1/runs/{parent_id}")).json()["workflow_version_id"]
    async with context.database.session() as session:
        draft = await session.get(RequirementDraft, draft_id)
        assert draft is not None
        fields = dict(draft.field_values_json)
        fields["output_structure"] = {
            "path": "output_structure",
            "value": "checklist",
            "source": "user",
            "message_id": None,
            "suggestion_id": None,
            "accepted_at": None,
            "explicit_none": False,
        }
        draft.field_values_json = fields
        draft.revision += 1
        await session.commit()
    validated = await client.post(
        f"/api/v1/branch-proposals/{proposal_id}/validate",
        json={},
        headers={"Idempotency-Key": new_id()},
    )
    assert validated.json()["proposal"]["status"] == "READY", validated.text
    async with context.database.session() as session:
        draft = await session.get(RequirementDraft, draft_id)
        assert draft is not None
        draft.status = "CONFIRMED"
        snapshot = {
            "fields": dict(draft.field_values_json),
            "preference_order": [],
            "output_spec": {},
        }
        branch_confirmation = Confirmation(
            id=new_id(),
            draft_id=draft.id,
            draft_revision=draft.revision,
            workflow_version_id=version_id,
            summary_hash="sha256:branch",
            input_hash=stable_hash(snapshot),
            budget_policy_hash=context.config.policy.policy_hash,
            data_policy_version=context.config.policy.version,
            confirmed_at=context.clock.now(),
            action="BRANCH",
            field_snapshot_json=snapshot,
        )
        session.add(branch_confirmation)
        await session.commit()

    # While a second task holds the slot, the branch cannot take it.
    gate.block = True
    second_confirmation = await _confirmation(api_context, "城市夜间公交")
    running = await _start(client, second_confirmation)
    assert running.status_code == 201, running.text
    await asyncio.wait_for(gate.entered.wait(), timeout=10)

    refused = await client.post(
        f"/api/v1/branch-proposals/{proposal_id}/confirm",
        json={
            "confirmation_id": branch_confirmation.id,
            "effective_step_id": "write_report",
            "report_hash": validated.json()["proposal"]["report_hash"],
            "explicit_start_confirmation": True,
        },
        headers={"Idempotency-Key": new_id()},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "RUN_BUSY"
    assert await _count(api_context, WorkflowRun) == 2  # parent + running task
    still_ready = (await client.get(f"/api/v1/branch-proposals/{proposal_id}")).json()
    assert still_ready["status"] == "READY"
    assert still_ready["created_run_id"] is None

    gate.release.set()
    assert await context.executor.wait(running.json()["id"], max_wait=60) == "SUCCEEDED"


async def test_chat_while_running_never_adds_a_model_request(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = SerialGate(context.runtime)
    context.engine._deps.runtime = gate  # type: ignore[attr-defined]
    confirmation_id = await _confirmation(api_context, "图书馆夜间开放")
    run = await _start(client, confirmation_id)
    assert run.status_code == 201, run.text
    await asyncio.wait_for(gate.entered.wait(), timeout=10)
    submits_before_chat = gate.submits

    version_id = (await client.get(f"/api/v1/runs/{run.json()['id']}")).json()[
        "workflow_version_id"
    ]
    draft = await client.post(
        "/api/v1/requirements",
        json={"workflow_version_id": version_id},
        headers={"Idempotency-Key": new_id()},
    )
    assert draft.status_code == 201, draft.text
    message = await client.post(
        f"/api/v1/requirements/{draft.json()['id']}/messages",
        json={"text": "补充：希望包含夜间交通"},
        headers={"Idempotency-Key": new_id()},
    )
    assert message.status_code == 202, message.text
    assert message.json()["analysis_operation_id"]
    # Chatting never starts a second execution branch of the run.
    assert gate.submits == submits_before_chat
    assert gate.max_concurrent == 1

    gate.release.set()
    assert await context.executor.wait(run.json()["id"], max_wait=60) == "SUCCEEDED"
    assert await _count(api_context, WorkflowRun) == 1


async def test_retry_after_timeout_never_overlaps_model_requests(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = SerialGate(context.runtime)
    gate.block = False
    gate.fail_first = True
    context.engine._deps.runtime = gate  # type: ignore[attr-defined]
    confirmation_id = await _confirmation(api_context, "图书馆夜间开放")
    started = await _start(client, confirmation_id)
    assert started.status_code == 201, started.text
    assert await context.executor.wait(started.json()["id"], max_wait=60) == "SUCCEEDED"

    assert gate.submits >= 2, "the timeout must be retried"
    assert gate.max_concurrent == 1
    assert await _count(api_context, WorkflowRun) == 1
    async with context.database.session() as session:
        requests = list((await session.scalars(select(ModelRequest))).all())
    # Every request finished before the next one started: never two in flight.
    spans = [(row.started_at, row.ended_at) for row in requests if row.ended_at is not None]
    assert len(spans) >= 2, "an empty sample cannot prove serial model execution"
    assert len(context.gateway._transport.calls) == len(requests)
    spans.sort()
    for (_started_at, previous_end), (next_start, _ended_at) in itertools.pairwise(spans):
        assert previous_end <= next_start


async def test_stop_with_an_in_flight_action_starts_nothing_new(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = SerialGate(context.runtime)
    context.engine._deps.runtime = gate  # type: ignore[attr-defined]
    confirmation_id = await _confirmation(api_context, "图书馆夜间开放")
    started = await _start(client, confirmation_id)
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    await asyncio.wait_for(gate.entered.wait(), timeout=10)
    submits_before = gate.submits
    current = (await client.get(f"/api/v1/runs/{run_id}")).json()

    pause = await client.post(
        f"/api/v1/runs/{run_id}/pause",
        json={"revision": current["revision"]},
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{current["revision"]}"'},
    )
    assert pause.status_code == 200, pause.text
    assert pause.json()["draining"] is True
    assert await _running_attempts(api_context) == 1

    gate.release.set()
    assert await context.executor.wait(run_id, max_wait=60) == "PAUSED"
    assert gate.submits == submits_before
    assert await _running_attempts(api_context) == 0
    assert await _count(api_context, WorkflowRun) == 1


async def test_unknown_request_after_restart_blocks_a_new_run(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    confirmation_id = await _confirmation(api_context, "图书馆夜间开放")
    from personal_agent.adapters.harness.mock_runtime import MockModelTransport
    from personal_agent.adapters.harness.model_gateway import ModelGateway

    # Deliberately seed a stranded low-level request, as if a previous process
    # died. The application's strict owner checks must stay enabled.
    previous = ModelGateway(context.database, context.clock, MockModelTransport({
        "model": {"scenarios": {"runtime.timeout.unknown": {"status": "unknown"}}}
    }))
    unknown = await previous.request(
        ModelRequestSpec(
            request_id="",
            owner=Owner(draft_id="draft-restart"),
            epoch=1,
            model_id="mock-model",
            messages=[ModelMessage(role="user", content="scenario:runtime.timeout.unknown")],
            output_limit=1024,
            deadline=None,  # type: ignore[arg-type]
            reservation_id="res-1",
        ),
        reserved_tokens=200,
        timeout_seconds=5,
    )
    assert unknown.status == "unknown"

    # A "restarted" application still reads the persisted outcome.
    restarted = ModelGateway(context.database, context.clock, MockModelTransport({}))
    assert await restarted.has_unknown() is True
    blocked = await _start(client, confirmation_id)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "MODEL_OUTCOME_UNKNOWN"
    assert await _count(api_context, WorkflowRun) == 0
    assert await _count(api_context, StepAttempt) == 0

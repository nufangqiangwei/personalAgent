"""Attempt facts and reuse for recovery (T109).

The attempt records the exact input and dependency hashes it ran with, the local
session link exists before submit, and the Output attempt is only successful once
the artifact was written. Reused evidence and sources are read back for resume.
"""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import func, select

from personal_agent.adapters.persistence.artifact_models import Artifact
from personal_agent.adapters.persistence.execution_models import (
    ExecutionEvent,
    StepAttempt,
    WorkflowRun,
)
from personal_agent.adapters.persistence.requirement_models import (
    Confirmation,
    RequirementDraft,
)
from personal_agent.adapters.persistence.session_models import SessionLink
from personal_agent.application.workflow_registry import WorkflowRegistry
from personal_agent.bootstrap.seed import seed
from personal_agent.domain.common import AppError, ErrorCategory, new_id, stable_hash

TOPIC_WORKFLOW = "公开主题资料研究"


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


async def _start(client: httpx.AsyncClient, confirmation_id: str) -> str:
    started = await client.post(
        "/api/v1/runs", json={"confirmation_id": confirmation_id},
        headers={"Idempotency-Key": new_id()},
    )
    assert started.status_code == 201, started.text
    return str(started.json()["id"])


async def _attempts(context: Any) -> list[StepAttempt]:
    async with context.database.session() as session:
        return list((await session.scalars(
            select(StepAttempt).order_by(StepAttempt.started_at)
        )).all())


async def test_attempt_records_real_input_and_session_link(
    client: httpx.AsyncClient, api_context: Any,
) -> None:
    _, context = api_context
    confirmation_id = await _confirmation(api_context, "图书馆夜间开放")
    run_id = await _start(client, confirmation_id)
    assert await context.executor.wait(run_id, max_wait=60) == "SUCCEEDED"

    attempts = await _attempts(context)
    async with context.database.session() as session:
        links = list((await session.scalars(
            select(SessionLink).order_by(SessionLink.created_at)
        )).all())
    linked = {link.attempt_id: link for link in links}
    assert linked, "an agent attempt must persist its session link before submit"
    for attempt in attempts:
        if attempt.id not in linked:
            continue
        link = linked[attempt.id]
        assert attempt.session_link_id == link.id
        assert link.run_id and link.adapter_session_id and link.runtime_id
        assert link.status in {"SUBMITTED", "UNKNOWN", "STOPPED"}
        assert isinstance(attempt.input_json["step"], str) and attempt.input_json["step"]
        assert "inputs" in attempt.input_json and "dependencies" in attempt.input_json
        assert attempt.input_hash == stable_hash(attempt.input_json)

    research = [item for item in attempts if item.id in linked and item.status == "SUCCEEDED"]
    assert research and research[-1].output_json
    assert research[-1].output_json.get("__evidence__")
    assert research[-1].output_json.get("__sources__")

    # A resumed run reads the evidence and sources back instead of only the dict.
    context.engine._sources = []
    context.engine._evidence_summary = {}
    async with context.database.session() as session:
        run = await session.get(WorkflowRun, run_id)
    assert run is not None
    await context.engine._restore_reuse(run)
    assert context.engine._sources and context.engine._evidence_summary


async def test_republishing_after_a_crash_keeps_one_artifact(
    client: httpx.AsyncClient, api_context: Any,
) -> None:
    from personal_agent.application.workflow_registry import WorkflowRegistry

    _, context = api_context
    confirmation_id = await _confirmation(api_context, "图书馆夜间开放")
    run_id = await _start(client, confirmation_id)
    assert await context.executor.wait(run_id, max_wait=60) == "SUCCEEDED"
    async with context.database.session() as session:
        run = await session.get(WorkflowRun, run_id)
        before = int(await session.scalar(
            select(func.count()).select_from(Artifact)
        ) or 0)
    assert run is not None
    _, definition = await WorkflowRegistry(
        context.database, context.clock,
    ).definition_of(run.workflow_version_id)
    output_step = next(step for step in definition.steps if step.type == "Output")
    inputs = {
        path: entry.get("value")
        for path, entry in (run.input_snapshot_json.get("fields") or {}).items()
    }
    # A crash after the file was published re-enters delivery for the same logical
    # effect; the writer must return the existing artifact, not duplicate it.
    await context.engine._restore_reuse(run)
    await context.artifacts.deliver(
        run=run, definition=definition, step=output_step, inputs=inputs,
        evidence=context.engine._evidence_summary, sources=context.engine._sources,
    )
    async with context.database.session() as session:
        after = int(await session.scalar(select(func.count()).select_from(Artifact)) or 0)
    assert after == before == 1


async def test_runtime_action_events_are_consumed_without_reasoning(
    client: httpx.AsyncClient, api_context: Any,
) -> None:
    _, context = api_context
    confirmation_id = await _confirmation(api_context, "图书馆夜间开放")
    run_id = await _start(client, confirmation_id)
    assert await context.executor.wait(run_id, max_wait=60) == "SUCCEEDED"
    async with context.database.session() as session:
        events = list((await session.scalars(
            select(ExecutionEvent).where(
                ExecutionEvent.run_id == run_id,
                ExecutionEvent.kind == "action.started",
            )
        )).all())
    assert events, "the engine must consume the adapter's action events"
    payloads = [dict(item.payload_json or {}) for item in events]
    assert all("direction_ids" in payload for payload in payloads)
    assert "PRIVATE" not in repr(payloads)


async def test_output_attempt_fails_when_the_artifact_cannot_be_written(
    client: httpx.AsyncClient, api_context: Any,
) -> None:
    _, context = api_context
    original = context.engine._deps.artifacts

    class FailingArtifacts:
        async def deliver(self, **kwargs: Any) -> None:
            raise AppError(
                code="ARTIFACT_WRITE_FAILED", message="the artifact could not be written",
                category=ErrorCategory.DEPENDENCY, status=503,
            )

        def __getattr__(self, name: str) -> Any:
            return getattr(original, name)

    context.engine._deps.artifacts = FailingArtifacts()  # type: ignore[assignment]
    try:
        confirmation_id = await _confirmation(api_context, "图书馆夜间开放")
        run_id = await _start(client, confirmation_id)
        assert await context.executor.wait(run_id, max_wait=60) == "FAILED"
        attempts = await _attempts(context)
        output_attempt = attempts[-1]
        assert output_attempt.status == "FAILED"
        assert output_attempt.error_category == ErrorCategory.DEPENDENCY.value
        assert output_attempt.ended_at is not None
        # No later attempt may claim success for a document that was never written.
        assert all(item.status != "SUCCEEDED" for item in attempts[-1:])
    finally:
        context.engine._deps.artifacts = original  # type: ignore[assignment]

"""Fixed-observation research integration (T062).

Eight deterministic cases exercise the real engine, budget ledger, managed
writes and evidence rules offline: normal runs for both seed workflows, unknown
model outcome, bounded retries followed by the explicit exhausted path, partial
delivery, a model attempt to redirect control, duplicate runtime events and a
missing direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from personal_agent.adapters.persistence.artifact_models import Artifact, Blob
from personal_agent.adapters.persistence.assistance_models import AssistanceRequest
from personal_agent.adapters.persistence.execution_models import (
    ExecutionSlot,
    StepAttempt,
    StepRun,
    WorkflowRun,
)
from personal_agent.adapters.persistence.requirement_models import (
    Confirmation,
    RequirementDraft,
)
from personal_agent.application.workflow_registry import WorkflowRegistry
from personal_agent.bootstrap.seed import seed
from personal_agent.domain.common import new_id, stable_hash


@dataclass
class RunState:
    run: WorkflowRun
    steps: list[StepRun]
    attempts: list[StepAttempt]
    artifacts: list[Artifact]
    markdown: str
    assistance: list[AssistanceRequest]
    slot_run_id: str | None


async def _fast_sleep(_seconds: float) -> None:
    return None


async def prepare_confirmation(
    api_context: Any, workflow_name: str, inputs: dict[str, Any]
) -> str:
    config, context = api_context
    await seed(context.database)
    registry = WorkflowRegistry(context.database, context.clock)
    summaries = await registry.list_summaries()
    item = next(entry for entry in summaries if entry["name"] == workflow_name)
    version_id = item["latest_version"]["id"]
    fields = {
        key: {
            "path": key,
            "value": value,
            "source": "user",
            "message_id": None,
            "suggestion_id": None,
            "accepted_at": None,
            "explicit_none": False,
        }
        for key, value in inputs.items()
    }
    snapshot = {"fields": fields, "preference_order": [], "output_spec": {}}
    async with context.database.session() as session:
        draft = RequirementDraft(
            id=new_id(),
            selected_version_id=version_id,
            revision=2,
            status="CONFIRMED",
            missing_fields_json=[],
            conflicts_json=[],
            field_values_json=fields,
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


async def execute(
    api_context: Any, workflow_name: str, inputs: dict[str, Any]
) -> tuple[str, str]:
    _, context = api_context
    context.engine._deps.sleep = _fast_sleep  # type: ignore[attr-defined]
    confirmation_id = await prepare_confirmation(api_context, workflow_name, inputs)
    snapshot = await context.coordinator.start(confirmation_id)
    context.executor.submit(snapshot["id"])
    status = await context.executor.wait(snapshot["id"], max_wait=60)
    return snapshot["id"], status


async def state_of(api_context: Any, run_id: str) -> RunState:
    _, context = api_context
    async with context.database.session() as session:
        run = await session.get(WorkflowRun, run_id)
        assert run is not None
        steps = list(
            (
                await session.scalars(
                    select(StepRun).where(StepRun.run_id == run_id).order_by(StepRun.created_at)
                )
            ).all()
        )
        attempts = list(
            (
                await session.scalars(
                    select(StepAttempt)
                    .join(StepRun, StepAttempt.step_run_id == StepRun.id)
                    .where(StepRun.run_id == run_id)
                    .order_by(StepAttempt.started_at)
                )
            ).all()
        )
        artifacts = list(
            (await session.scalars(select(Artifact).where(Artifact.run_id == run_id))).all()
        )
        assistance = list(
            (
                await session.scalars(
                    select(AssistanceRequest).where(AssistanceRequest.run_id == run_id)
                )
            ).all()
        )
        slot = await session.get(ExecutionSlot, 1)
        blob_id = artifacts[0].blob_id if artifacts else None
        blob = await session.get(Blob, blob_id) if blob_id else None
        markdown = ""
        if blob is not None:
            path = context.config.artifacts_dir / blob.managed_relative_path
            markdown = path.read_text(encoding="utf-8")
        return RunState(
            run=run,
            steps=steps,
            attempts=attempts,
            artifacts=artifacts,
            markdown=markdown,
            assistance=assistance,
            slot_run_id=slot.run_id if slot else None,
        )


async def test_technology_workflow_completes_with_a_qualified_candidate(
    api_context: Any,
) -> None:
    run_id, status = await execute(
        api_context,
        "技术选型对比",
        {"topic": "AI 编程助手", "mock_scenario": "engine.tech.complete"},
    )
    assert status == "SUCCEEDED"
    state = await state_of(api_context, run_id)
    assert state.run.completeness == "COMPLETE"
    assert state.artifacts and state.artifacts[0].format == "markdown"
    assert "候选 A" in state.markdown
    assert "QUALIFIED" in state.markdown
    step_ids = [step.step_id for step in state.steps]
    assert "collect_official" in step_ids and "write_comparison" in step_ids
    assert "review_gaps" not in step_ids or next(
        step for step in state.steps if step.step_id == "review_gaps"
    ).status == "SKIPPED"


async def test_topic_workflow_completes_with_both_directions(api_context: Any) -> None:
    run_id, status = await execute(
        api_context, "公开主题资料研究", {"topic": "图书馆夜间开放"}
    )
    assert status == "SUCCEEDED"
    state = await state_of(api_context, run_id)
    assert state.run.completeness == "COMPLETE"
    assert "背景与基本事实" in state.markdown
    assert "最新进展" in state.markdown


async def test_unknown_model_outcome_waits_and_keeps_the_slot(api_context: Any) -> None:
    run_id, status = await execute(
        api_context,
        "技术选型对比",
        {"topic": "x", "mock_scenario": "runtime.timeout.unknown"},
    )
    assert status == "WAITING_USER"
    state = await state_of(api_context, run_id)
    assert state.run.reason_code == "MODEL_OUTCOME_UNKNOWN"
    assert any(item.kind == "RUNTIME_UNCERTAIN" for item in state.assistance)
    assert state.slot_run_id == run_id
    assert state.artifacts == []


async def test_bounded_retries_follow_the_explicit_exhausted_path(
    api_context: Any,
) -> None:
    run_id, status = await execute(
        api_context,
        "技术选型对比",
        {"topic": "x", "mock_scenario": "runtime.dependency_failure"},
    )
    assert status == "WAITING_USER"
    state = await state_of(api_context, run_id)
    failed_step = next(step for step in state.steps if step.step_id == "collect_official")
    assert failed_step.status == "FAILED"
    attempts = [item for item in state.attempts if item.attempt_no >= 1]
    assert len(attempts) >= 3
    review = next(step for step in state.steps if step.step_id == "review_gaps")
    assert review.status == "WAITING_USER"
    assert any(item.kind == "HUMAN_REVIEW" for item in state.assistance)


async def test_partial_delivery_is_labelled_partial(api_context: Any) -> None:
    run_id, status = await execute(
        api_context,
        "公开主题资料研究",
        {"topic": "图书馆夜间开放", "mock_scenario": "engine.topic.missing_direction"},
    )
    assert status == "SUCCEEDED"
    state = await state_of(api_context, run_id)
    assert state.run.completeness == "PARTIAL"
    assert "完成程度：PARTIAL" in state.markdown
    assert "缺口" in state.markdown


async def test_model_text_cannot_redirect_the_declared_path(api_context: Any) -> None:
    run_id, status = await execute(
        api_context,
        "技术选型对比",
        {"topic": "AI 编程助手", "mock_scenario": "engine.tech.redirect"},
    )
    assert status == "SUCCEEDED"
    state = await state_of(api_context, run_id)
    step_ids = {step.step_id for step in state.steps}
    assert "skip_to_output" not in step_ids
    # The declared success path still runs the condition and the output step.
    assert "check_gaps" in step_ids
    assert "write_comparison" in step_ids


async def test_duplicate_runtime_events_do_not_duplicate_business_results(
    api_context: Any,
) -> None:
    run_id, status = await execute(api_context, "公开主题资料研究", {"topic": "重复事件"})
    assert status == "SUCCEEDED"
    state = await state_of(api_context, run_id)
    assert len(state.artifacts) == 1
    per_step: dict[str, int] = {}
    for attempt in state.attempts:
        per_step[attempt.step_run_id] = per_step.get(attempt.step_run_id, 0) + 1
    assert all(count == 1 for count in per_step.values())


async def test_replaying_the_same_observation_produces_the_same_decisions(
    api_context: Any,
) -> None:
    first_id, first_status = await execute(
        api_context, "技术选型对比", {"topic": "可重放", "mock_scenario": "engine.tech.complete"}
    )
    second_id, second_status = await execute(
        api_context, "技术选型对比", {"topic": "可重放", "mock_scenario": "engine.tech.complete"}
    )
    assert first_status == second_status == "SUCCEEDED"
    first = await state_of(api_context, first_id)
    second = await state_of(api_context, second_id)
    assert [step.step_id for step in first.steps] == [
        step.step_id for step in second.steps
    ]
    assert first.artifacts[0].manifest_hash == second.artifacts[0].manifest_hash
    assert first.markdown == second.markdown


async def test_slot_is_released_after_a_terminal_run(api_context: Any) -> None:
    run_id, status = await execute(api_context, "公开主题资料研究", {"topic": "槽位释放"})
    assert status == "SUCCEEDED"
    state = await state_of(api_context, run_id)
    assert state.slot_run_id is None


async def test_every_executed_step_is_recorded_as_succeeded_with_its_transition(
    api_context: Any,
) -> None:
    """Resume and history depend on a truthful record of what already ran.

    A finished step must not stay ``RUNNING``, and the transition it selected has to
    be persisted so a resumed run can continue from it without redoing the step.
    """
    run_id, status = await execute(api_context, "公开主题资料研究", {"topic": "步骤记录"})
    assert status == "SUCCEEDED"
    state = await state_of(api_context, run_id)
    executed = [step for step in state.steps if step.status != "SKIPPED"]
    assert executed, "a completed run must have executed steps"
    assert all(step.status == "SUCCEEDED" for step in executed), [
        (step.step_id, step.status) for step in executed
    ]
    non_terminal = [step for step in executed if step.step_id != "write_report"]
    assert non_terminal and all(step.selected_transition for step in non_terminal)

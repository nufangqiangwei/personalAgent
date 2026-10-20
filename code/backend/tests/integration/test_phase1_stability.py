"""Phase-1 stability acceptance: two workflows x three inputs x two runs (T130).

The twelve runs use the normal coordinator/executor path and the committed mock
observations; each must produce one accessible document. Failure and timeout
convergence are asserted separately; the remaining control cases are linked from
docs/acceptance-matrix.md.
"""

from __future__ import annotations

import hashlib
from typing import Any

from test_fixed_research import state_of

from personal_agent.adapters.persistence.requirement_models import (
    Confirmation,
    RequirementDraft,
)
from personal_agent.application.workflow_registry import WorkflowRegistry
from personal_agent.bootstrap.seed import seed
from personal_agent.domain.common import new_id, stable_hash

CASES = [
    (
        "技术选型对比",
        {"topic": "AI 编程助手", "mock_scenario": "engine.tech.complete"},
        {"structure": "report"},
    ),
    (
        "技术选型对比",
        {"topic": "代码助手对比", "mock_scenario": "engine.tech.redirect"},
        {"structure": "checklist"},
    ),
    (
        "技术选型对比",
        {"topic": "IDE 插件选型", "mock_scenario": "engine.tech.complete"},
        {"structure": "comparison"},
    ),
    ("公开主题资料研究", {"topic": "图书馆夜间开放"}, {"structure": "report"}),
    ("公开主题资料研究", {"topic": "城市公共图书馆夜间开放"}, {"structure": "report"}),
    ("公开主题资料研究", {"topic": "社区图书馆延时服务"}, {"structure": "report"}),
]

VALID_COMPLETENESS = {"COMPLETE", "PARTIAL", "NO_QUALIFIED_RESULT"}


async def _execute(
    api_context: Any,
    workflow_name: str,
    inputs: dict[str, Any],
    output_spec: dict[str, Any],
) -> tuple[str, str]:
    _, context = api_context
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
    snapshot = {
        "fields": fields,
        "preference_order": [],
        "output_spec": output_spec,
    }
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
            output_spec_json=output_spec,
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
            budget_policy_hash=context.config.policy.policy_hash,
            data_policy_version=context.config.policy.version,
            confirmed_at=context.clock.now(),
            action="START",
            field_snapshot_json=snapshot,
        )
        session.add(confirmation)
        await session.commit()
        confirmation_id = confirmation.id
    context.engine._deps.sleep = _fast_sleep
    run = await context.coordinator.start(confirmation_id)
    context.executor.submit(run["id"])
    status = await context.executor.wait(run["id"], max_wait=60)
    return run["id"], status


async def _fast_sleep(_seconds: float) -> None:
    return None


async def test_twelve_runs_produce_an_accessible_document(api_context: Any) -> None:
    seen_artifacts: set[str] = set()
    by_workflow: dict[str, list[str]] = {}
    for workflow_name, inputs, output_spec in CASES:
        for _repetition in range(2):
            run_id, status = await _execute(api_context, workflow_name, inputs, output_spec)
            assert status == "SUCCEEDED", (workflow_name, inputs, status)
            state = await state_of(api_context, run_id)
            assert state.run.completeness in VALID_COMPLETENESS
            assert len(state.artifacts) == 1, state.artifacts
            artifact = state.artifacts[0]
            assert artifact.id not in seen_artifacts
            seen_artifacts.add(artifact.id)
            assert state.markdown.strip()
            if workflow_name == "公开主题资料研究":
                assert inputs["topic"] in state.markdown
            assert state.run.workflow_version_id
            by_workflow.setdefault(workflow_name, []).append(state.markdown)

            # Re-reading a committed run must not duplicate its document or
            # change the recorded version.
            again = await state_of(api_context, run_id)
            assert [item.id for item in again.artifacts] == [artifact.id]
            assert again.run.workflow_version_id == state.run.workflow_version_id

    # Different inputs must not collapse to one document per workflow.
    for workflow_name, documents in by_workflow.items():
        first_of_each = documents[0::2]
        hashes = {
            hashlib.sha256(markdown.encode("utf-8")).hexdigest()
            for markdown in first_of_each
        }
        assert len(hashes) == 3, workflow_name


async def test_timeout_converges_without_a_fabricated_result(api_context: Any) -> None:
    run_id, status = await _execute(
        api_context,
        "技术选型对比",
        {"topic": "x", "mock_scenario": "runtime.timeout.unknown"},
        {},
    )
    assert status == "WAITING_USER"
    state = await state_of(api_context, run_id)
    assert state.run.reason_code == "MODEL_OUTCOME_UNKNOWN"
    assert not state.artifacts
    assert any(item.kind == "RUNTIME_UNCERTAIN" for item in state.assistance)


async def test_dependency_failure_converges_without_a_fabricated_result(
    api_context: Any,
) -> None:
    run_id, status = await _execute(
        api_context,
        "技术选型对比",
        {"topic": "x", "mock_scenario": "runtime.dependency_failure"},
        {},
    )
    assert status == "WAITING_USER"
    state = await state_of(api_context, run_id)
    assert state.run.status == "WAITING_USER"
    assert not state.artifacts

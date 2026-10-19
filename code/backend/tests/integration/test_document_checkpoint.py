"""Document checkpoint integration (T070).

The delivered document and the run state must be one consistent fact: the
artifact's manifest hash is the hash of the stored document, the blob hash is the
hash of the stored bytes, and re-opening the document returns exactly those bytes
without re-running anything. A partial document stays partial; when the completion
policy forbids partial delivery the document is still retained but the run must
not be reported as successful.
"""

from __future__ import annotations

import copy
import hashlib
from typing import Any

from sqlalchemy import select

from personal_agent.adapters.persistence.artifact_models import Artifact, Blob
from personal_agent.adapters.persistence.execution_models import WorkflowRun
from personal_agent.adapters.persistence.requirement_models import (
    Confirmation,
    RequirementDraft,
)
from personal_agent.application.workflow_registry import WorkflowRegistry
from personal_agent.bootstrap.seed import seed
from personal_agent.domain.common import new_id, stable_hash

TOPIC_WORKFLOW = "公开主题资料研究"


async def _fast_sleep(_seconds: float) -> None:
    return None


async def _seed(api_context: Any) -> WorkflowRegistry:
    _, context = api_context
    await seed(context.database)
    return WorkflowRegistry(context.database, context.clock)


async def _latest_version(api_context: Any, workflow_name: str) -> dict[str, Any]:
    registry = await _seed(api_context)
    summaries = await registry.list_summaries()
    entry = next(item for item in summaries if item["name"] == workflow_name)
    return dict(entry["latest_version"])


async def _confirm(
    api_context: Any,
    version_id: str,
    fields: dict[str, Any],
    *,
    output_spec: dict[str, Any] | None = None,
) -> str:
    config, context = api_context
    field_values = {
        key: {
            "path": key,
            "value": value,
            "source": "user",
            "message_id": None,
            "suggestion_id": None,
            "accepted_at": None,
            "explicit_none": False,
        }
        for key, value in fields.items()
    }
    snapshot = {
        "fields": field_values,
        "preference_order": [],
        "output_spec": output_spec or {},
    }
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
            output_spec_json=output_spec or {},
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


async def _execute(api_context: Any, confirmation_id: str) -> tuple[str, str]:
    _, context = api_context
    context.engine._deps.sleep = _fast_sleep  # type: ignore[attr-defined]
    snapshot = await context.coordinator.start(confirmation_id)
    run_id = str(snapshot["id"])
    context.executor.submit(run_id)
    status = await context.executor.wait(run_id, max_wait=60)
    return run_id, status


async def _run(
    api_context: Any,
    workflow_name: str,
    fields: dict[str, Any],
    *,
    output_spec: dict[str, Any] | None = None,
    version_id: str | None = None,
) -> tuple[str, str]:
    resolved = version_id or (await _latest_version(api_context, workflow_name))["id"]
    confirmation_id = await _confirm(api_context, resolved, fields, output_spec=output_spec)
    return await _execute(api_context, confirmation_id)


async def _load(api_context: Any, run_id: str) -> tuple[WorkflowRun, Artifact, Blob, bytes]:
    _, context = api_context
    async with context.database.session() as session:
        run = await session.get(WorkflowRun, run_id)
        assert run is not None
        artifact = (
            await session.scalars(select(Artifact).where(Artifact.run_id == run_id))
        ).one()
        blob = await session.get(Blob, artifact.blob_id)
        assert blob is not None
        stored = context.config.artifacts_dir / blob.managed_relative_path
        return run, artifact, blob, stored.read_bytes()


async def test_saved_document_reopens_from_the_persisted_checkpoint(
    api_context: Any,
) -> None:
    _, context = api_context

    run_id, status = await _run(api_context, TOPIC_WORKFLOW, {"topic": "图书馆夜间开放"})
    assert status == "SUCCEEDED"
    run, artifact, blob, stored = await _load(api_context, run_id)

    # The run status, the artifact and the document tell the same story.
    assert run.completeness == "COMPLETE"
    assert artifact.completeness == run.completeness
    assert artifact.access_state == "AVAILABLE"
    document = artifact.document_json
    assert document["schema_version"] == 1
    assert document["completeness"] == run.completeness
    assert document["title"]
    assert document["sources"], "a delivered document must reference its sources"
    assert document["gaps"] == []
    assert all(
        direction["status"] in {"covered", "missing"} for direction in document["directions"]
    )

    # The manifest hash identifies the stored document, not just the run.
    assert stable_hash(document) == artifact.manifest_hash

    # The blob hash identifies the stored bytes.
    assert hashlib.sha256(stored).hexdigest() == blob.sha256
    assert blob.size_bytes == len(stored)
    assert blob.managed_relative_path == blob.managed_relative_path.strip()
    assert ":" not in blob.managed_relative_path
    assert not blob.managed_relative_path.startswith(("/", "\\"))
    assert ".." not in blob.managed_relative_path

    # Re-opening returns exactly the same bytes and creates nothing new.
    opened, first_metadata = await context.artifacts.content(artifact.id)
    reopened, second_metadata = await context.artifacts.content(artifact.id)
    assert opened == reopened == stored
    assert first_metadata["sha256"] == second_metadata["sha256"] == blob.sha256

    listed = await context.artifacts.list_for_run(run_id)
    assert [item["id"] for item in listed] == [artifact.id]
    detail = await context.artifacts.get(artifact.id)
    assert detail["document"] == artifact.document_json
    assert detail["completeness"] == artifact.completeness == "COMPLETE"
    assert detail["workflow_version_id"] == run.workflow_version_id


async def test_checklist_structure_follows_the_confirmed_output_spec(
    api_context: Any,
) -> None:
    _, context = api_context
    run_id, status = await _run(
        api_context,
        TOPIC_WORKFLOW,
        {"topic": "图书馆夜间开放"},
        output_spec={"structure": "checklist"},
    )
    assert status == "SUCCEEDED"
    run, artifact, _, stored = await _load(api_context, run_id)
    assert run.completeness == "COMPLETE"
    assert artifact.structure == "checklist"
    body = stored.decode("utf-8")
    assert "检查清单" in body
    content, metadata = await context.artifacts.content(artifact.id)
    assert metadata["structure"] == "checklist"
    assert content == stored


async def test_partial_delivery_keeps_run_and_document_consistent(
    api_context: Any,
) -> None:
    run_id, status = await _run(
        api_context,
        TOPIC_WORKFLOW,
        {"topic": "图书馆夜间开放", "mock_scenario": "engine.topic.missing_direction"},
    )
    assert status == "SUCCEEDED"
    run, artifact, blob, stored = await _load(api_context, run_id)

    assert run.completeness == "PARTIAL"
    assert artifact.completeness == "PARTIAL"
    assert run.reason_code is None
    document = artifact.document_json
    assert document["completeness"] == "PARTIAL"
    assert document["gaps"], "a partial document must state its gaps"
    missing = [item for item in document["directions"] if item["status"] == "missing"]
    assert missing and all(item["gaps"] for item in missing)
    body = stored.decode("utf-8")
    assert "完成程度：PARTIAL" in body
    assert "完成程度：COMPLETE" not in body
    assert hashlib.sha256(stored).hexdigest() == blob.sha256


async def test_strict_completion_policy_retains_the_document_but_fails_the_run(
    api_context: Any,
) -> None:
    _, context = api_context
    registry = await _seed(api_context)
    latest = await _latest_version(api_context, TOPIC_WORKFLOW)
    _, definition = await registry.definition_of(latest["id"])
    # ``from`` is a Python keyword, so the transition rule is dumped by alias.
    strict = copy.deepcopy(definition.model_dump(by_alias=True))
    strict["version"] = int(latest["version_no"]) + 1
    strict["description"] = "严格完成策略：不允许部分交付时不得标记成功。"
    strict["completion_policy"]["allow_partial_document"] = False

    outcome = await registry.save_version(
        workflow_id=strict["workflow_id"],
        base_version_id=latest["id"],
        definition=strict,
        change_summary="禁止部分交付",
        confirm_executable=True,
    )
    strict_version_id = outcome.version.id

    # The same observation that yields a partial document under the default policy
    # must become NO_QUALIFIED_RESULT once partial delivery is not allowed.
    run_id, status = await _run(
        api_context,
        TOPIC_WORKFLOW,
        {"topic": "图书馆夜间开放", "mock_scenario": "engine.topic.missing_direction"},
        version_id=strict_version_id,
    )
    run, artifact, _, stored = await _load(api_context, run_id)

    assert status == "FAILED"
    assert run.status == "FAILED"
    assert run.reason_code == "NO_QUALIFIED_RESULT"
    assert run.completeness == "NO_QUALIFIED_RESULT"
    assert artifact.completeness == "NO_QUALIFIED_RESULT"
    assert artifact.access_state == "AVAILABLE", "the partial document must be retained"
    # The retained document is still readable but never claims success.
    content, metadata = await context.artifacts.content(artifact.id)
    assert content == stored
    assert metadata["completeness"] == "NO_QUALIFIED_RESULT"
    body = stored.decode("utf-8")
    assert "完成程度：NO_QUALIFIED_RESULT" in body
    assert "完成程度：COMPLETE" not in body

"""Recovery integration tests (T072).

Startup only diagnoses; resuming is an explicit action that re-verifies the frozen
run. These cases cover a finished versus unfinished step, every unresolved write
state (including a file that landed but was never recorded, and one edited after the
fact), an expired or deleted material, an incompatible version, a pending cancel and a
late model result. Nothing here may unlock an unknown outcome automatically.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from personal_agent.adapters.persistence.artifact_models import (
    Artifact,
    Blob,
    SideEffectOperation,
)
from personal_agent.adapters.persistence.execution_models import (
    ExecutionEvent,
    StepAttempt,
    StepRun,
    WorkflowRun,
)
from personal_agent.adapters.persistence.requirement_models import (
    Confirmation,
    RequirementDraft,
)
from personal_agent.adapters.persistence.workflow_models import WorkflowVersion
from personal_agent.application.workflow_registry import WorkflowRegistry
from personal_agent.bootstrap.seed import seed
from personal_agent.domain.common import AppError, new_id, stable_hash

TOPIC_WORKFLOW = "公开主题资料研究"


class GatedRuntime:
    """Hold a submitted result before the engine can commit its completion."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block = True

    async def submit(self, task: Any) -> Any:
        return await self._inner.submit(task)

    async def wait_result(self, submission_id: str) -> Any:
        self.entered.set()
        if self.block:
            await self.release.wait()
        return await self._inner.wait_result(submission_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _confirmation(api_context: Any, fields: dict[str, Any]) -> tuple[str, str]:
    config, context = api_context
    await seed(context.database)
    registry = WorkflowRegistry(context.database, context.clock)
    summaries = await registry.list_summaries()
    version_id = next(
        item["latest_version"]["id"] for item in summaries if item["name"] == TOPIC_WORKFLOW
    )
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
        return confirmation.id, version_id


async def _start(client: httpx.AsyncClient, api_context: Any, gate: GatedRuntime) -> str:
    _, context = api_context
    context.engine._deps.runtime = gate  # type: ignore[attr-defined]
    confirmation_id, _version_id = await _confirmation(api_context, {"topic": "图书馆夜间开放"})
    response = await client.post(
        "/api/v1/runs",
        json={"confirmation_id": confirmation_id},
        headers={"Idempotency-Key": new_id()},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _snapshot(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    return (await client.get(f"/api/v1/runs/{run_id}")).json()


async def _pause(
    client: httpx.AsyncClient, api_context: Any, gate: GatedRuntime, run_id: str
) -> dict[str, Any]:
    _, context = api_context
    await asyncio.wait_for(gate.entered.wait(), timeout=10)
    current = await _snapshot(client, run_id)
    response = await client.post(
        f"/api/v1/runs/{run_id}/pause",
        json={"revision": current["revision"], "reason": "recovery drill"},
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{current["revision"]}"'},
    )
    assert response.status_code == 200, response.text
    gate.release.set()
    assert await context.executor.wait(run_id, max_wait=60) == "PAUSED"
    return await _snapshot(client, run_id)


async def _run_report(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/runs/{run_id}/recovery")
    assert response.status_code == 200, response.text
    return response.json()


async def _resume(client: httpx.AsyncClient, run_id: str, revision: int) -> httpx.Response:
    return await client.post(
        f"/api/v1/runs/{run_id}/resume",
        json={"revision": revision},
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{revision}"'},
    )


async def _steps(api_context: Any, run_id: str) -> dict[str, StepRun]:
    _, context = api_context
    async with context.database.session() as session:
        rows = (
            await session.scalars(select(StepRun).where(StepRun.run_id == run_id))
        ).all()
        return {row.step_id: row for row in rows}


async def _attempt_counts(api_context: Any, run_id: str) -> dict[str, int]:
    _, context = api_context
    steps = await _steps(api_context, run_id)
    async with context.database.session() as session:
        rows = (
            await session.scalars(
                select(StepAttempt)
                .join(StepRun, StepAttempt.step_run_id == StepRun.id)
                .where(StepRun.run_id == run_id)
            )
        ).all()
        by_step: dict[str, int] = {}
        for row in rows:
            step_id = next(
                (item.step_id for item in steps.values() if item.id == row.step_run_id), "?"
            )
            by_step[step_id] = by_step.get(step_id, 0) + 1
        return by_step


async def _add_artifact(
    api_context: Any, run_id: str, version_id: str, body: bytes = b"# report\n"
) -> tuple[str, str]:
    """Attach a stored artifact to a run, as a previous continuation would have."""
    _, context = api_context
    relative = f"recovery/{new_id()}.md"
    stored = await context.writer.store.put(relative, body)
    blob_id = new_id()
    artifact_id = new_id()
    now = context.clock.now()
    async with context.database.session() as session:
        session.add(
            Blob(
                id=blob_id,
                managed_relative_path=relative,
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                media_type="text/markdown",
                created_at=now,
                expires_at=None,
                deleted_at=None,
            )
        )
        await session.flush()
        session.add(
            Artifact(
                id=artifact_id,
                run_id=run_id,
                attempt_id=None,
                workflow_version_id=version_id,
                operation_id=new_id(),
                blob_id=blob_id,
                kind="DOCUMENT",
                format="markdown",
                structure="report",
                completeness="COMPLETE",
                manifest_hash="sha256:recovery-fixture",
                document_json={},
                created_at=now,
                access_state="AVAILABLE",
            )
        )
        await session.commit()
    return artifact_id, relative


async def _add_operation(
    api_context: Any,
    run_id: str,
    *,
    key: str,
    target: str,
    intended: bytes,
    previous: bytes | None,
    status: str = "INTENT",
) -> str:
    _, context = api_context
    async with context.database.session() as session:
        operation = SideEffectOperation(
            id=new_id(),
            root_run_id=run_id,
            run_id=run_id,
            step_attempt_id=None,
            logical_effect_key=key,
            target_ref=target,
            expected_before_hash=(
                hashlib.sha256(previous).hexdigest() if previous is not None else None
            ),
            intended_after_hash=hashlib.sha256(intended).hexdigest(),
            intent_ref=f"intent:{key}",
            status=status,
            observed_hash=None,
            verified_at=None,
            verification_ref=None,
            revision=1,
            created_at=context.clock.now(),
        )
        session.add(operation)
        await session.commit()
        return operation.id


# ----------------------------------------------------------------------
async def test_unfinished_step_is_resumed_without_redoing_a_finished_one(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = GatedRuntime(context.runtime)
    run_id = await _start(client, api_context, gate)
    paused = await _pause(client, api_context, gate, run_id)

    report = await _run_report(client, run_id)
    assert report["status"] == "PAUSED"
    assert report["resumable"] is True
    assert report["blocked_by"] is None
    checks = {check["code"]: check["ok"] for check in report["checks"]}
    assert all(checks.values()), checks

    before = await _steps(api_context, run_id)
    finished = {
        step_id: row for step_id, row in before.items() if row.status == "SUCCEEDED"
    }
    assert finished, "the paused run must have completed at least one step"
    counts_before = await _attempt_counts(api_context, run_id)

    resumed = await _resume(client, run_id, paused["revision"])
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["id"] == run_id
    assert await context.executor.wait(run_id, max_wait=60) == "SUCCEEDED"

    after = await _steps(api_context, run_id)
    counts_after = await _attempt_counts(api_context, run_id)
    for step_id, row in finished.items():
        assert after[step_id].status == "SUCCEEDED"
        assert after[step_id].selected_transition == row.selected_transition
        assert counts_after[step_id] == counts_before[step_id]


@pytest.mark.parametrize("status", ["INTENT", "PARTIAL", "UNKNOWN", "CONFLICT"])
async def test_unresolved_write_blocks_resume(
    client: httpx.AsyncClient, api_context: Any, status: str
) -> None:
    _, context = api_context
    gate = GatedRuntime(context.runtime)
    run_id = await _start(client, api_context, gate)
    paused = await _pause(client, api_context, gate, run_id)
    await _add_operation(
        api_context,
        run_id,
        key=f"document:{status}",
        target=f"recovery/{status.lower()}.md",
        intended=b"intended document",
        previous=None,
        status=status,
    )

    report = await _run_report(client, run_id)
    checks = {check["code"]: check for check in report["checks"]}
    assert checks["SIDE_EFFECTS"]["ok"] is False
    assert report["resumable"] is False
    assert report["blocked_by"] == "SIDE_EFFECT_UNRESOLVED"

    response = await _resume(client, run_id, paused["revision"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SIDE_EFFECT_UNRESOLVED"


async def test_a_file_that_landed_but_was_never_recorded_is_reconciled(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = GatedRuntime(context.runtime)
    run_id = await _start(client, api_context, gate)
    paused = await _pause(client, api_context, gate, run_id)

    target = "recovery/written-but-not-committed.md"
    content = b"# the write actually landed\n"
    await context.writer.store.put(target, content)
    operation_id = await _add_operation(
        api_context,
        run_id,
        key="document:landed",
        target=target,
        intended=content,
        previous=None,
    )

    blocked = await _run_report(client, run_id)
    assert blocked["blocked_by"] == "SIDE_EFFECT_UNRESOLVED"

    resolved = await context.writer.reconcile(operation_id)
    assert resolved.value == "APPLIED"
    report = await _run_report(client, run_id)
    assert report["resumable"] is True
    assert report["blocked_by"] is None
    assert (await _resume(client, run_id, paused["revision"])).status_code == 200
    assert await context.executor.wait(run_id, max_wait=60) == "SUCCEEDED"


async def test_a_write_that_never_landed_is_safe_to_retry(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = GatedRuntime(context.runtime)
    run_id = await _start(client, api_context, gate)
    paused = await _pause(client, api_context, gate, run_id)

    target = "recovery/unchanged.md"
    previous = b"# previous content\n"
    await context.writer.store.put(target, previous)
    operation_id = await _add_operation(
        api_context,
        run_id,
        key="document:not-applied",
        target=target,
        intended=b"# new content that never landed\n",
        previous=previous,
    )
    resolved = await context.writer.reconcile(operation_id)
    assert resolved.value == "NOT_APPLIED"

    report = await _run_report(client, run_id)
    assert report["resumable"] is True
    assert report["blocked_by"] is None
    assert (await _resume(client, run_id, paused["revision"])).status_code == 200
    assert await context.executor.wait(run_id, max_wait=60) == "SUCCEEDED"


async def test_a_target_edited_later_becomes_a_conflict(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = GatedRuntime(context.runtime)
    run_id = await _start(client, api_context, gate)
    paused = await _pause(client, api_context, gate, run_id)

    target = "recovery/edited-later.md"
    await context.writer.store.put(target, b"# someone else's edit\n")
    operation_id = await _add_operation(
        api_context,
        run_id,
        key="document:edited",
        target=target,
        intended=b"# our intended document\n",
        previous=b"# previous content\n",
    )
    resolved = await context.writer.reconcile(operation_id)
    assert resolved.value == "CONFLICT"

    report = await _run_report(client, run_id)
    assert report["blocked_by"] == "SIDE_EFFECT_UNRESOLVED"
    response = await _resume(client, run_id, paused["revision"])
    assert response.status_code == 409


async def test_a_changed_artifact_hash_blocks_resume(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = GatedRuntime(context.runtime)
    run_id = await _start(client, api_context, gate)
    paused = await _pause(client, api_context, gate, run_id)
    _artifact_id, relative = await _add_artifact(api_context, run_id, paused["workflow_version_id"])

    report = await _run_report(client, run_id)
    assert {check["code"]: check["ok"] for check in report["checks"]}["ARTIFACTS"] is True

    await context.writer.store.put(relative, b"# silently rewritten\n")
    report = await _run_report(client, run_id)
    assert report["blocked_by"] == "ARTIFACT_UNAVAILABLE"
    assert (await _resume(client, run_id, paused["revision"])).status_code == 409


async def test_an_expired_or_deleted_material_blocks_resume(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = GatedRuntime(context.runtime)
    run_id = await _start(client, api_context, gate)
    paused = await _pause(client, api_context, gate, run_id)
    artifact_id, relative = await _add_artifact(api_context, run_id, paused["workflow_version_id"])

    # The lifecycle policy expired the material: the file may still exist, but no
    # longer counts as an available dependency.
    async with context.database.session() as session:
        artifact = await session.get(Artifact, artifact_id)
        assert artifact is not None
        blob = await session.get(Blob, artifact.blob_id)
        assert blob is not None
        blob.expires_at = context.clock.now()
        artifact.access_state = "EXPIRED"
        await session.commit()

    report = await _run_report(client, run_id)
    assert report["blocked_by"] == "ARTIFACT_UNAVAILABLE"
    assert (await _resume(client, run_id, paused["revision"])).status_code == 409

    # A deleted material is equally unavailable, even if the bytes happen to remain.
    async with context.database.session() as session:
        artifact = await session.get(Artifact, artifact_id)
        assert artifact is not None
        artifact.access_state = "DELETED"
        blob = await session.get(Blob, artifact.blob_id)
        assert blob is not None
        blob.deleted_at = context.clock.now()
        await session.commit()
    assert (await _run_report(client, run_id))["blocked_by"] == "ARTIFACT_UNAVAILABLE"
    assert await context.writer.store.exists(relative)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("version", False, "VERSION_CHANGED"),
        ("runtime_fingerprint", "other-runtime", "RUNTIME_INCOMPATIBLE"),
        ("config_fingerprint", "other-config", "POLICY_CHANGED"),
        ("budget_policy_hash", "sha256:other-policy", "POLICY_CHANGED"),
    ],
)
async def test_a_changed_definition_or_environment_blocks_resume(
    client: httpx.AsyncClient, api_context: Any, field: str, value: Any, code: str
) -> None:
    _, context = api_context
    gate = GatedRuntime(context.runtime)
    run_id = await _start(client, api_context, gate)
    paused = await _pause(client, api_context, gate, run_id)

    async with context.database.session() as session:
        if field == "version":
            version = await session.get(WorkflowVersion, paused["workflow_version_id"])
            assert version is not None
            version.executable = bool(value)
        else:
            run = await session.get(WorkflowRun, run_id)
            assert run is not None
            setattr(run, field, value)
        await session.commit()

    report = await _run_report(client, run_id)
    assert report["resumable"] is False
    assert report["blocked_by"] == code
    response = await _resume(client, run_id, paused["revision"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == code


async def test_modified_input_blocks_resume(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = GatedRuntime(context.runtime)
    run_id = await _start(client, api_context, gate)
    paused = await _pause(client, api_context, gate, run_id)

    async with context.database.session() as session:
        run = await session.get(WorkflowRun, run_id)
        assert run is not None
        snapshot = dict(run.input_snapshot_json)
        snapshot["topic"] = "被篡改的主题"
        run.input_snapshot_json = snapshot
        await session.commit()

    report = await _run_report(client, run_id)
    assert report["blocked_by"] == "INPUT_CHANGED"
    assert (await _resume(client, run_id, paused["revision"])).status_code == 409


async def test_a_pending_cancel_only_completes_the_cancellation(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = GatedRuntime(context.runtime)
    run_id = await _start(client, api_context, gate)
    await _pause(client, api_context, gate, run_id)

    # An interrupted cancel leaves cancel_requested set; it must not be resumable.
    async with context.database.session() as session:
        run = await session.get(WorkflowRun, run_id)
        assert run is not None
        run.status = "INTERRUPTED"
        run.cancel_requested = True
        run.revision += 1
        revision = run.revision
        await session.commit()

    report = await _run_report(client, run_id)
    assert report["resumable"] is False
    assert report["blocked_by"] == "CANCEL_PENDING"
    refused = await _resume(client, run_id, revision)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "CANCEL_PENDING"

    cancelled = await client.post(
        f"/api/v1/runs/{run_id}/cancel",
        json={"revision": revision, "confirmed": True, "reason": "完成取消"},
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{revision}"'},
    )
    assert cancelled.status_code == 200, cancelled.text
    # Converging an interrupted cancel needs no engine task: the cancellation is
    # finished by the control path itself.
    assert cancelled.json()["status"] == "CANCELLED"
    final = await _snapshot(client, run_id)
    assert final["status"] == "CANCELLED"
    assert final["artifact_ids"] == []
    assert (await _run_report(client, run_id))["resumable"] is False


async def test_a_late_result_after_cancel_is_evidence_not_success(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    gate = GatedRuntime(context.runtime)
    run_id = await _start(client, api_context, gate)
    await asyncio.wait_for(gate.entered.wait(), timeout=10)
    current = await _snapshot(client, run_id)
    response = await client.post(
        f"/api/v1/runs/{run_id}/cancel",
        json={"revision": current["revision"], "confirmed": True},
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{current["revision"]}"'},
    )
    assert response.status_code == 200, response.text
    gate.release.set()
    assert await context.executor.wait(run_id, max_wait=60) == "CANCELLED"

    async with context.database.session() as session:
        events = (
            await session.scalars(
                select(ExecutionEvent).where(ExecutionEvent.run_id == run_id)
            )
        ).all()
        attempts = (
            await session.scalars(
                select(StepAttempt)
                .join(StepRun, StepAttempt.step_run_id == StepRun.id)
                .where(StepRun.run_id == run_id)
            )
        ).all()
    assert any(event.kind == "late_result.quarantined" for event in events)
    assert attempts and all(attempt.status != "SUCCEEDED" for attempt in attempts)

    report = await _run_report(client, run_id)
    assert report["resumable"] is False
    final = await _snapshot(client, run_id)
    assert (await _resume(client, run_id, final["revision"])).status_code == 409


async def test_two_resumes_cannot_acquire_the_same_epoch(
    client: httpx.AsyncClient, api_context: Any,
) -> None:
    _, context = api_context
    gate = GatedRuntime(context.runtime)
    run_id = await _start(client, api_context, gate)
    paused = await _pause(client, api_context, gate, run_id)

    async def attempt() -> Any:
        return await context.recovery.resume(
            run_id, expected_revision=paused["revision"],
        )

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    successes = [item for item in results if not isinstance(item, BaseException)]
    failures = [item for item in results if isinstance(item, AppError)]
    assert len(successes) == 1, results
    assert failures and failures[0].code in {
        "RUN_REVISION_STALE", "REVISION_STALE", "RUN_BUSY", "RUN_STATE_INVALID",
    }
    assert await context.executor.wait(run_id, max_wait=60) == "SUCCEEDED"

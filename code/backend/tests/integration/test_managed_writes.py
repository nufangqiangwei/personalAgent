"""Managed-write fault injection (T009).

Covers intent-before-write, publish-before-commit, retry idempotency, cross-branch
effect identity, path traversal and reconciliation of a write whose outcome is not
obvious. Every test uses a temporary workspace only.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from personal_agent.adapters.persistence.artifact_models import Artifact, SideEffectOperation
from personal_agent.adapters.persistence.blob_store import BlobStore, ManagedRootError
from personal_agent.adapters.persistence.execution_models import WorkflowRun
from personal_agent.adapters.persistence.workflow_models import Workflow, WorkflowVersion
from personal_agent.application.managed_writes import ArtifactDraft, ManagedWriter
from personal_agent.domain.artifacts import SideEffectStatus
from personal_agent.domain.common import AppError

CONTENT = b"# Report\n\nAll required sections are present.\n"
DRAFT = ArtifactDraft(
    workflow_version_id="version-1",
    completeness="COMPLETE",
    manifest_hash="sha256:manifest",
    document={"schema_version": 1, "sections": []},
)


async def make_run(
    database: Any, clock: Any, *, run_id: str, root_run_id: str | None = None
) -> None:
    async with database.session() as session:
        if await session.get(Workflow, "workflow-1") is None:
            session.add(
                Workflow(
                    id="workflow-1",
                    latest_executable_version_id=None,
                    disabled_at=None,
                    disabled_reason=None,
                    created_at=clock.now(),
                    revision=1,
                )
            )
            await session.flush()
        if await session.get(WorkflowVersion, "version-1") is None:
            session.add(
                WorkflowVersion(
                    id="version-1",
                    workflow_id="workflow-1",
                    version_no=1,
                    definition_json={"schema_version": "1"},
                    definition_hash="sha256:version-1",
                    schema_version="1",
                    saved_at=clock.now(),
                    change_summary="test version",
                    executable=True,
                    validation_report_json={"valid": True, "issues": []},
                    revision=1,
                )
            )
            await session.flush()
        session.add(
            WorkflowRun(
                id=run_id,
                root_run_id=root_run_id or run_id,
                parent_run_id=None if root_run_id is None else "parent",
                fork_step_id=None,
                workflow_version_id="version-1",
                confirmation_id=f"confirmation-{run_id}",
                input_snapshot_json={"schema_version": 1},
                input_hash="sha256:input",
                config_fingerprint="config",
                runtime_fingerprint="runtime",
                budget_policy_hash="sha256:budget",
                data_policy_version="1",
                status="RUNNING",
                reason_code=None,
                current_step_id=None,
                epoch=1,
                revision=1,
                mode="FIXED",
                completeness=None,
                active_elapsed_ms=0,
                cancel_requested=False,
                created_at=clock.now(),
                updated_at=clock.now(),
            )
        )
        await session.commit()


def writer(database: Any, data_dir: Any, clock: Any) -> ManagedWriter:
    return ManagedWriter(database, BlobStore(data_dir / "artifacts"), clock)


async def test_publish_records_intent_and_verified_artifact(
    database: Any, data_dir: Any, clock: Any
) -> None:
    await make_run(database, clock, run_id="run-1")
    managed = writer(database, data_dir, clock)
    operation = await managed.begin(
        root_run_id="run-1",
        run_id="run-1",
        attempt_id="attempt-1",
        logical_effect_key="output:step-6",
        target_ref="run-1/report.md",
        intended_content=CONTENT,
    )
    assert operation.status == SideEffectStatus.INTENT.value

    result = await managed.publish(
        operation_id=operation.id,
        run_id="run-1",
        workflow_version_id="version-1",
        content=CONTENT,
        draft=DRAFT,
    )
    assert result.status is SideEffectStatus.APPLIED
    assert (data_dir / "artifacts" / "run-1" / "report.md").exists()

    async with database.session() as session:
        artifact = await session.get(Artifact, result.artifact_id)
        assert artifact is not None
        assert artifact.completeness == "COMPLETE"
        assert artifact.access_state == "AVAILABLE"
        stored_operation = await session.get(SideEffectOperation, operation.id)
        assert stored_operation.status == SideEffectStatus.APPLIED.value
        assert stored_operation.observed_hash == result.sha256


async def test_repeated_publish_reuses_the_artifact(
    database: Any, data_dir: Any, clock: Any
) -> None:
    await make_run(database, clock, run_id="run-1")
    managed = writer(database, data_dir, clock)
    operation = await managed.begin(
        root_run_id="run-1",
        run_id="run-1",
        attempt_id="attempt-1",
        logical_effect_key="output:step-6",
        target_ref="run-1/report.md",
        intended_content=CONTENT,
    )
    first = await managed.publish(
        operation_id=operation.id,
        run_id="run-1",
        workflow_version_id="version-1",
        content=CONTENT,
        draft=DRAFT,
    )
    second = await managed.publish(
        operation_id=operation.id,
        run_id="run-1",
        workflow_version_id="version-1",
        content=CONTENT,
        draft=DRAFT,
    )
    assert first.artifact_id == second.artifact_id
    async with database.session() as session:
        artifacts = (await session.scalars(select(Artifact))).all()
        assert len(artifacts) == 1


async def test_same_effect_with_different_content_is_refused(
    database: Any, data_dir: Any, clock: Any
) -> None:
    await make_run(database, clock, run_id="run-1")
    managed = writer(database, data_dir, clock)
    await managed.begin(
        root_run_id="run-1",
        run_id="run-1",
        attempt_id="attempt-1",
        logical_effect_key="output:step-6",
        target_ref="run-1/report.md",
        intended_content=CONTENT,
    )
    with pytest.raises(AppError) as error:
        await managed.begin(
            root_run_id="run-1",
            run_id="run-1",
            attempt_id="attempt-2",  # a new attempt id is not new authorization
            logical_effect_key="output:step-6",
            target_ref="run-1/report.md",
            intended_content=b"# Different\n",
        )
    assert error.value.code == "SIDE_EFFECT_UNRESOLVED"


async def test_cross_branch_lookup_uses_the_root_and_effect_key(
    database: Any, data_dir: Any, clock: Any
) -> None:
    await make_run(database, clock, run_id="root-run")
    await make_run(database, clock, run_id="branch-run", root_run_id="root-run")
    managed = writer(database, data_dir, clock)
    first = await managed.begin(
        root_run_id="root-run",
        run_id="root-run",
        attempt_id="attempt-1",
        logical_effect_key="output:step-6",
        target_ref="root-run/report.md",
        intended_content=CONTENT,
    )
    same = await managed.begin(
        root_run_id="root-run",
        run_id="branch-run",
        attempt_id="attempt-9",
        logical_effect_key="output:step-6",
        target_ref="root-run/report.md",
        intended_content=CONTENT,
    )
    assert same.id == first.id


async def test_failure_after_file_publish_leaves_no_success_record(
    database: Any, data_dir: Any, clock: Any
) -> None:
    await make_run(database, clock, run_id="run-1")
    managed = writer(database, data_dir, clock)
    operation = await managed.begin(
        root_run_id="run-1",
        run_id="run-1",
        attempt_id="attempt-1",
        logical_effect_key="output:step-6",
        target_ref="run-1/report.md",
        intended_content=CONTENT,
    )

    async def failing_attach(session: Any, artifact: Artifact) -> None:
        raise RuntimeError("business commit failed after the file was published")

    with pytest.raises(RuntimeError):
        await managed.publish(
            operation_id=operation.id,
            run_id="run-1",
            workflow_version_id="version-1",
            content=CONTENT,
            draft=DRAFT,
            attach=failing_attach,
        )

    async with database.session() as session:
        artifacts = (await session.scalars(select(Artifact))).all()
        assert artifacts == []
        stored = await session.get(SideEffectOperation, operation.id)
        assert stored is not None
        assert stored.status == SideEffectStatus.INTENT.value
    # The orphan file may remain; it must be reconciled, not assumed complete.
    assert (data_dir / "artifacts" / "run-1" / "report.md").exists()


async def test_failure_before_publish_leaves_only_an_intent(
    database: Any, data_dir: Any, clock: Any
) -> None:
    await make_run(database, clock, run_id="run-1")
    managed = writer(database, data_dir, clock)
    operation = await managed.begin(
        root_run_id="run-1",
        run_id="run-1",
        attempt_id="attempt-1",
        logical_effect_key="output:step-6",
        target_ref="run-1/report.md",
        intended_content=CONTENT,
    )

    async def broken_put(relative_path: str, data: bytes) -> None:
        raise OSError("disk full")

    managed.store.put = broken_put  # type: ignore[method-assign]
    with pytest.raises(OSError):
        await managed.publish(
            operation_id=operation.id,
            run_id="run-1",
            workflow_version_id="version-1",
            content=CONTENT,
            draft=DRAFT,
        )
    async with database.session() as session:
        assert (await session.scalars(select(Artifact))).all() == []
        stored = await session.get(SideEffectOperation, operation.id)
        assert stored.status == SideEffectStatus.INTENT.value
    assert not (data_dir / "artifacts" / "run-1" / "report.md").exists()


async def test_reconcile_classifies_the_target_without_guessing(
    database: Any, data_dir: Any, clock: Any
) -> None:
    await make_run(database, clock, run_id="run-1")
    managed = writer(database, data_dir, clock)
    operation = await managed.begin(
        root_run_id="run-1",
        run_id="run-1",
        attempt_id="attempt-1",
        logical_effect_key="output:step-6",
        target_ref="run-1/report.md",
        intended_content=CONTENT,
    )
    # A missing file alone is not proof that nothing was written.
    assert await managed.reconcile(operation.id) is SideEffectStatus.UNKNOWN

    # Reconcile also covers a second, independently published operation.
    second = await managed.begin(
        root_run_id="run-1",
        run_id="run-1",
        attempt_id="attempt-2",
        logical_effect_key="output:step-7",
        target_ref="run-1/report-2.md",
        intended_content=CONTENT,
    )
    await managed.publish(
        operation_id=second.id,
        run_id="run-1",
        workflow_version_id="version-1",
        content=CONTENT,
        draft=DRAFT,
    )
    assert await managed.reconcile(second.id) is SideEffectStatus.APPLIED

    # A later manual change to the target is a conflict, not a success.
    await managed.store.put("run-1/report-2.md", b"# Changed by hand\n")
    assert await managed.reconcile(second.id) is SideEffectStatus.CONFLICT


def test_blob_store_rejects_escapes_and_absolute_paths(tmp_path: Any) -> None:
    store = BlobStore(tmp_path / "artifacts")
    for path in ("../escape.md", "a/../../escape.md", "/etc/passwd", "C:/Windows/system.ini"):
        with pytest.raises(ManagedRootError):
            store.resolve(path)
    assert store.resolve("run-1/report.md").is_relative_to(store.root)

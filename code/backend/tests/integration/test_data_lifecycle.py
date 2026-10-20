"""Data lifecycle integration tests (T089).

A deletion is immediate for reads and reuse while the physical cleanup is asynchronous.
A shared blob survives while another available artifact needs it, tombstones keep no
content, retention expires business content and destroys old sessions, and deleting a
reused result invalidates the cross-branch reference.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select

from personal_agent.adapters.persistence.artifact_models import Artifact, Blob
from personal_agent.adapters.persistence.branch_models import BranchProposal, ReusedResult
from personal_agent.adapters.persistence.lifecycle_models import (
    BackupManifest,
    DeletionRecord,
)
from personal_agent.adapters.persistence.session_models import SessionLink
from personal_agent.application.data_lifecycle import DataLifecycleService
from personal_agent.bootstrap.seed import seed
from personal_agent.domain.common import new_id, stable_hash


async def _artifact_fixture(
    api_context: Any,
    *,
    body: bytes = b"# report\n\ncontent to delete\n",
    run_id: str | None = None,
) -> tuple[str, str, str]:
    """Create a committed run and one available artifact; return (run, artifact, blob)."""
    config, context = api_context
    await seed(context.database)
    from personal_agent.application.workflow_registry import WorkflowRegistry

    registry = WorkflowRegistry(context.database, context.clock)
    summaries = await registry.list_summaries()
    version_id = next(
        item["latest_version"]["id"] for item in summaries if item["name"] == "公开主题资料研究"
    )
    from personal_agent.adapters.persistence.execution_models import WorkflowRun
    from personal_agent.adapters.persistence.requirement_models import (
        Confirmation,
        RequirementDraft,
    )

    field_values = {
        "topic": {
            "path": "topic",
            "value": "图书馆夜间开放",
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
        await session.flush()
        run = WorkflowRun(
            id=run_id or new_id(),
            root_run_id=run_id or new_id(),
            parent_run_id=None,
            fork_step_id=None,
            workflow_version_id=version_id,
            confirmation_id=confirmation.id,
            input_snapshot_json=snapshot,
            input_hash=confirmation.input_hash,
            config_fingerprint="config",
            runtime_fingerprint="runtime",
            budget_policy_hash=config.policy.policy_hash,
            data_policy_version=config.policy.version,
            status="SUCCEEDED",
            reason_code=None,
            current_step_id=None,
            epoch=1,
            revision=1,
            mode="FIXED",
            completeness="COMPLETE",
            active_elapsed_ms=0,
            cancel_requested=False,
            created_at=context.clock.now(),
            updated_at=context.clock.now(),
        )
        run.root_run_id = run.id
        session.add(run)
        await session.commit()
        run_id = run.id

    relative = f"lifecycle/{new_id()}.md"
    stored = await context.writer.store.put(relative, body)
    blob_id = new_id()
    artifact_id = new_id()
    async with context.database.session() as session:
        session.add(
            Blob(
                id=blob_id,
                managed_relative_path=relative,
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                media_type="text/markdown",
                created_at=context.clock.now(),
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
                manifest_hash="sha256:lifecycle",
                document_json={},
                created_at=context.clock.now(),
                access_state="AVAILABLE",
            )
        )
        await session.commit()
    return run_id, artifact_id, blob_id


async def _delete(
    client: httpx.AsyncClient, item_ids: list[str], *, acknowledge: bool = True
) -> httpx.Response:
    return await client.post(
        "/api/v1/deletions",
        json={"item_ids": item_ids, "acknowledge_resume_impact": acknowledge},
        headers={"Idempotency-Key": new_id()},
    )


async def test_deletion_takes_effect_before_the_physical_cleanup(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    run_id, artifact_id, blob_id = await _artifact_fixture(api_context)
    listed = (await client.get("/api/v1/data-items")).json()["items"]
    assert any(item["id"] == artifact_id and item["deletable"] for item in listed)

    refused = await _delete(client, [artifact_id], acknowledge=False)
    assert refused.status_code == 422

    response = await _delete(client, [artifact_id])
    assert response.status_code == 202, response.text
    record = response.json()
    assert record["status"] in {"REQUESTED", "COMPLETED"}
    assert record["status_url"].endswith(record["id"])
    # The content is not readable anymore, even before the file is removed.
    assert (await client.get(f"/api/v1/artifacts/{artifact_id}")).status_code == 410
    assert (
        await client.get(f"/api/v1/artifacts/{artifact_id}/content")
    ).status_code == 410

    for _ in range(100):
        current = (await client.get(record['status_url'])).json()
        if current['status'] in {'COMPLETED', 'PARTIAL', 'FAILED'}:
            break
        await __import__('asyncio').sleep(0.02)
    async with context.database.session() as session:
        artifact = await session.get(Artifact, artifact_id)
        blob = await session.get(Blob, blob_id)
        stored = await session.get(DeletionRecord, record["id"])
        assert artifact is not None and artifact.access_state == "DELETED"
        assert blob is not None and blob.deleted_at is not None
        assert stored is not None and stored.status == "COMPLETED"
        # The tombstone records ids and hashes only, never the deleted content.
        assert "content to delete" not in str(stored.affected_ids)
        assert "content to delete" not in (stored.tombstone_hash or "")
    assert not await context.writer.store.exists(
        await _blob_relative(api_context, blob_id)
    )
    page = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert page["status"] == "SUCCEEDED"  # deleting a document does not rewrite history


async def _blob_relative(api_context: Any, blob_id: str) -> str:
    _, context = api_context
    async with context.database.session() as session:
        blob = await session.get(Blob, blob_id)
        assert blob is not None
        return blob.managed_relative_path


async def test_a_shared_blob_survives_while_another_artifact_needs_it(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    run_id, first_artifact, blob_id = await _artifact_fixture(api_context)
    relative = await _blob_relative(api_context, blob_id)
    async with context.database.session() as session:
        blob = await session.get(Blob, blob_id)
        assert blob is not None
        second = Artifact(
            id=new_id(),
            run_id=run_id,
            attempt_id=None,
            workflow_version_id="version",
            operation_id=new_id(),
            blob_id=blob.id,
            kind="DOCUMENT",
            format="markdown",
            structure="report",
            completeness="COMPLETE",
            manifest_hash="sha256:second",
            document_json={},
            created_at=context.clock.now(),
            access_state="AVAILABLE",
        )
        session.add(second)
        await session.commit()
        second_id = second.id

    await _delete(client, [first_artifact])
    async with context.database.session() as session:
        blob = await session.get(Blob, blob_id)
        assert blob is not None and blob.deleted_at is None
    assert await context.writer.store.exists(relative)

    await _delete(client, [second_id])
    async with context.database.session() as session:
        blob = await session.get(Blob, blob_id)
        assert blob is not None and blob.deleted_at is not None


async def test_deleting_a_reused_result_invalidates_the_branch_reference(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    run_id, artifact_id, _blob_id = await _artifact_fixture(api_context)
    branch_id = run_id
    async with context.database.session() as session:
        session.add(
            BranchProposal(
                id=new_id(),
                parent_run_id=run_id,
                root_run_id=run_id,
                fork_step_id="write_report",
                status="CONFIRMED",
                draft_id=None,
                source_draft_id=None,
                changed_paths=[],
                input_reads_closure=[],
                affected_step_ids=[],
                reusable_step_ids=["collect_background"],
                invalidated_artifact_ids=[],
                blocked_reasons=[],
                report_hash="sha256:report",
                input_hash=None,
                confirmation_id=None,
                created_run_id=None,
                revision=1,
                created_at=context.clock.now(),
                updated_at=context.clock.now(),
            )
        )
        session.add(
            ReusedResult(
                id=new_id(),
                run_id=branch_id,
                source_run_id=run_id,
                step_id="write_report",
                artifact_id=artifact_id,
                manifest_hash="sha256:reuse",
                created_at=context.clock.now(),
            )
        )
        await session.commit()

    assert (await _delete(client, [artifact_id])).status_code == 202
    async with context.database.session() as session:
        remaining = (
            await session.scalars(
                select(ReusedResult).where(ReusedResult.artifact_id == artifact_id)
            )
        ).all()
    assert remaining == []


async def test_retention_expires_business_content_and_destroys_old_sessions(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    run_id, artifact_id, _blob_id = await _artifact_fixture(api_context)
    old = context.clock.now() - timedelta(days=200)
    async with context.database.session() as session:
        artifact = await session.get(Artifact, artifact_id)
        assert artifact is not None
        artifact.created_at = old
        session.add(
            SessionLink(
                id=new_id(),
                operation_id=new_id(),
                run_id=run_id,
                attempt_id=new_id(),
                draft_id=None,
                adapter_session_id="session-old",
                provider_session_id=None,
                runtime_id="mock",
                provider="mock",
                sdk_version="0.2.1rc2",
                runtime_version="0.2.1rc2",
                profile_hash="profile",
                capability_hash="capability",
                store_ref=None,
                status="READY",
                revision=1,
                created_at=old,
            )
        )
        session.add(
            BackupManifest(
                id=new_id(),
                status="VERIFIED",
                format_version=1,
                schema_version="0014",
                app_version="0.1.0",
                policy_version="1",
                db_revision="0014",
                manifest_hash=f"sha256:{new_id()}",
                blob_hashes=[],
                blob_count=0,
                total_bytes=0,
                blob_hashes_digest="sha256:empty",
                deletion_tombstone_hash="sha256:none",
                unknown_model_request=False,
                verification={},
                usable=True,
                secret_free=True,
                created_at=old,
                verified_at=old,
                expires_at=old,
                deleted_at=None,
            )
        )
        await session.commit()

    lifecycle = DataLifecycleService(
        context.database,
        context.clock,
        context.writer.store,
        retention=context.config.policy.retention,
        data_dir=context.config.data_dir,
    )
    counts = await lifecycle.apply_retention()
    assert counts["expired_artifacts"] >= 1
    assert counts["expired_backups"] >= 1
    assert (await client.get(f"/api/v1/artifacts/{artifact_id}")).status_code == 410
    async with context.database.session() as session:
        sessions = (await session.scalars(select(SessionLink))).all()
        backups = (await session.scalars(select(BackupManifest))).all()
    assert all(item.adapter_session_id != "session-old" for item in sessions)
    assert all(backup.status == "EXPIRED" for backup in backups)

"""Backup, restore and migration safety (T096, backend half).

A backup is verified before it is usable. Restoring keeps the maintenance mode, applies
the current deletion tombstones, keeps restored executions interrupted and never
auto-resumes them. An incompatible backup or a tampered manifest is refused without
touching live data; a failed migration falls back to the verified backup.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import select

from personal_agent.adapters.persistence.artifact_models import Artifact, Blob
from personal_agent.adapters.persistence.execution_models import WorkflowRun
from personal_agent.adapters.persistence.lifecycle_models import BackupManifest
from personal_agent.adapters.persistence.requirement_models import (
    Confirmation,
    RequirementDraft,
)
from personal_agent.application.workflow_registry import WorkflowRegistry
from personal_agent.bootstrap.seed import seed
from personal_agent.domain.common import new_id, stable_hash


async def _seed_marker(api_context: Any) -> tuple[str, str]:
    """A completed run with one readable artifact and one stale RUNNING run."""
    config, context = api_context
    await seed(context.database)
    registry = WorkflowRegistry(context.database, context.clock)
    summaries = await registry.list_summaries()
    version_id = next(
        item["latest_version"]["id"] for item in summaries if item["name"] == "公开主题资料研究"
    )
    field_values = {
        "topic": {
            "path": "topic",
            "value": "恢复演练",
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
            id=new_id(),
            root_run_id="",
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

    relative = f"restore/{new_id()}.md"
    stored = await context.writer.store.put(relative, b"# keep me\n")
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
                manifest_hash="sha256:restore",
                document_json={},
                created_at=context.clock.now(),
                access_state="AVAILABLE",
            )
        )
        await session.commit()
    return run_id, artifact_id


async def _enter_maintenance(client: httpx.AsyncClient) -> int:
    status = (await client.get("/api/v1/maintenance/status")).json()
    assert status["mode"] == "NORMAL"
    entered = await client.post(
        "/api/v1/maintenance/enter",
        json={"reason": "演练", "explicit_confirm": True},
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{status["revision"]}"'},
    )
    assert entered.status_code == 202, entered.text
    current = (await client.get("/api/v1/maintenance/status")).json()
    assert current["mode"] == "MAINTENANCE"
    assert current["quiescent"] is True
    return int(current["revision"])


async def _create_backup(client: httpx.AsyncClient) -> dict[str, Any]:
    created = await client.post(
        "/api/v1/maintenance/backups",
        json={"reason": "演练备份", "explicit_backup": True},
        headers={"Idempotency-Key": new_id()},
    )
    assert created.status_code == 202, created.text
    backup_id = created.json()["backup_id"]
    backup = (await client.get(f"/api/v1/maintenance/backups/{backup_id}")).json()
    assert backup["status"] == "VERIFIED", backup
    assert backup["usable"] is True
    assert backup["secret_free"] is True
    return backup


async def _mark_schema(api_context: Any, version: str) -> None:
    """The test database is built from metadata; record the schema revision it implies."""
    from sqlalchemy import text

    _, context = api_context
    async with context.database.session() as session:
        await session.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        await session.execute(text("DELETE FROM alembic_version"))
        await session.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:version)"),
            {"version": version},
        )
        await session.commit()


async def test_maintenance_transitions_are_explicit_and_paced(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    from personal_agent.bootstrap.lifespan import migration_head

    await _mark_schema(api_context, migration_head() or "0014")
    revision = await _enter_maintenance(client)

    # Exiting needs If-Match and an explicit confirmation.
    missing = await client.post(
        "/api/v1/maintenance/exit",
        json={"expected_revision": revision, "explicit_confirm": True},
        headers={"Idempotency-Key": new_id()},
    )
    assert missing.status_code == 428
    refused = await client.post(
        "/api/v1/maintenance/exit",
        json={"expected_revision": revision, "explicit_confirm": False},
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{revision}"'},
    )
    assert refused.status_code == 422
    exited = await client.post(
        "/api/v1/maintenance/exit",
        json={"expected_revision": revision, "explicit_confirm": True},
        headers={"Idempotency-Key": new_id(), "If-Match": f'"{revision}"'},
    )
    assert exited.status_code == 200, exited.text
    assert exited.json()["mode"] == "NORMAL"


async def test_backup_restore_applies_tombstones_and_keeps_runs_interrupted(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    run_id, artifact_id = await _seed_marker(api_context)
    # Mark the snapshotted run as if it was interrupted by a crash before the backup;
    # a restore must leave it INTERRUPTED and must never resume it.
    async with context.database.session() as session:
        run = await session.get(WorkflowRun, run_id)
        assert run is not None
        run.status = "RUNNING"
        await session.commit()
    await _enter_maintenance(client)
    backup = await _create_backup(client)

    # A deletion after the backup must still win after restoring.
    deletion = await client.post(
        "/api/v1/deletions",
        json={"item_ids": [artifact_id], "acknowledge_resume_impact": True},
        headers={"Idempotency-Key": new_id()},
    )
    assert deletion.status_code == 202, deletion.text

    restored = await client.post(
        "/api/v1/maintenance/restores",
        json={
            "backup_id": backup["id"],
            "manifest_hash": backup["manifest_hash"],
            "explicit_restore": True,
            "acknowledge_interruption": True,
            "acknowledge_lifecycle": True,
        },
        headers={"Idempotency-Key": new_id()},
    )
    assert restored.status_code == 202, restored.text
    body = restored.json()
    assert body["status"] == "COMPLETED"
    assert body["post_restore"]["auto_resumed"] is False
    assert body["post_restore"]["interrupted_runs"] >= 1
    assert body["post_restore"]["tombstones_applied"] >= 1
    status = (await client.get("/api/v1/maintenance/status")).json()
    assert status["mode"] == "MAINTENANCE"
    assert (await client.get(f"/api/v1/artifacts/{artifact_id}")).status_code == 410
    async with context.database.session() as session:
        runs = (await session.scalars(select(WorkflowRun))).all()
    assert any(run.status == "INTERRUPTED" for run in runs)
    assert all(run.status != "RUNNING" for run in runs)


async def test_incompatible_backup_and_tampered_manifest_are_refused(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    _run_id, artifact_id = await _seed_marker(api_context)
    await _enter_maintenance(client)
    backup = await _create_backup(client)

    tampered = await client.post(
        "/api/v1/maintenance/restores",
        json={
            "backup_id": backup["id"],
            "manifest_hash": "sha256:not-the-manifest",
            "explicit_restore": True,
            "acknowledge_interruption": True,
            "acknowledge_lifecycle": True,
        },
        headers={"Idempotency-Key": new_id()},
    )
    assert tampered.status_code == 412
    assert tampered.json()["error"]["code"] == "MANIFEST_HASH_MISMATCH"

    async with context.database.session() as session:
        row = await session.get(BackupManifest, backup["id"])
        assert row is not None
        row.schema_version = "9999"
        await session.commit()
    incompatible = await client.post(
        "/api/v1/maintenance/restores",
        json={
            "backup_id": backup["id"],
            "manifest_hash": backup["manifest_hash"],
            "explicit_restore": True,
            "acknowledge_interruption": True,
            "acknowledge_lifecycle": True,
        },
        headers={"Idempotency-Key": new_id()},
    )
    assert incompatible.status_code == 422
    assert incompatible.json()["error"]["code"] == "BACKUP_INCOMPATIBLE"
    # Live data was not replaced.
    assert (await client.get(f"/api/v1/artifacts/{artifact_id}")).status_code == 200


async def test_failed_migration_restores_the_verified_backup(
    client: httpx.AsyncClient, api_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_id, artifact_id = await _seed_marker(api_context)
    await _enter_maintenance(client)
    backup = await _create_backup(client)

    import personal_agent.application.maintenance as maintenance_module

    def _boom() -> None:
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(maintenance_module, "_run_upgrade", _boom)
    # Pretend the database is behind head so the migration path is attempted.
    await _mark_schema(api_context, "0012")

    response = await client.post(
        "/api/v1/maintenance/migrations",
        json={
            "target_revision": "head",
            "backup_id": backup["id"],
            "explicit_migrate": True,
        },
        headers={"Idempotency-Key": new_id()},
    )
    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "MAINTENANCE_TRANSITION_BLOCKED"
    status = (await client.get("/api/v1/maintenance/status")).json()
    assert status["mode"] == "MAINTENANCE"
    assert (await client.get(f"/api/v1/artifacts/{artifact_id}")).status_code == 200

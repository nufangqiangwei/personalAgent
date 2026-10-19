"""Artifact delivery API contract tests (T063).

The delivered Markdown is reachable through three read endpoints. They must
report the real completeness (a PARTIAL document is never shown as COMPLETE),
serve ``text/markdown`` with a content hash as ETag and a safe filename, and keep
reads side-effect free: opening a document again never restarts research.
Deleted, expired and integrity-failed content must not leak as a successful read.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import httpx
from sqlalchemy import func, select

from personal_agent.adapters.persistence.artifact_models import Artifact, Blob
from personal_agent.adapters.persistence.execution_models import (
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

REPORT_WORKFLOW = "公开主题资料研究"
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.md$")


async def _fast_sleep(_seconds: float) -> None:
    return None


async def _confirm(api_context: Any, workflow_name: str, fields: dict[str, Any]) -> str:
    """Insert the confirmed snapshot directly; the endpoints under test own delivery."""
    config, context = api_context
    await seed(context.database)
    registry = WorkflowRegistry(context.database, context.clock)
    summaries = await registry.list_summaries()
    version_id = next(
        entry["latest_version"]["id"] for entry in summaries if entry["name"] == workflow_name
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
        return confirmation.id


async def _start_run(
    client: httpx.AsyncClient,
    api_context: Any,
    *,
    scenario: str | None = None,
    topic: str = "图书馆夜间开放",
) -> tuple[str, str]:
    _, context = api_context
    context.engine._deps.sleep = _fast_sleep  # type: ignore[attr-defined]
    fields: dict[str, Any] = {"topic": topic}
    if scenario is not None:
        fields["mock_scenario"] = scenario
    confirmation_id = await _confirm(api_context, REPORT_WORKFLOW, fields)
    response = await client.post(
        "/api/v1/runs",
        json={"confirmation_id": confirmation_id},
        headers={"Idempotency-Key": new_id()},
    )
    assert response.status_code == 201, response.text
    run_id = str(response.json()["id"])
    status = await context.executor.wait(run_id, max_wait=60)
    return run_id, status


async def _counts(api_context: Any) -> tuple[int, int, int]:
    _, context = api_context
    async with context.database.session() as session:
        runs = int((await session.scalar(select(func.count()).select_from(WorkflowRun))) or 0)
        steps = int((await session.scalar(select(func.count()).select_from(StepRun))) or 0)
        attempts = int(
            (await session.scalar(select(func.count()).select_from(StepAttempt))) or 0
        )
    return runs, steps, attempts


async def _blob(api_context: Any, artifact_id: str) -> Blob:
    _, context = api_context
    async with context.database.session() as session:
        artifact = await session.get(Artifact, artifact_id)
        assert artifact is not None
        blob = await session.get(Blob, artifact.blob_id)
        assert blob is not None
        return blob


async def test_run_artifacts_endpoint_lists_the_delivered_document(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    run_id, status = await _start_run(client, api_context)
    assert status == "SUCCEEDED"

    response = await client.get(f"/api/v1/runs/{run_id}/artifacts")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    artifact = items[0]
    assert artifact["run_id"] == run_id
    assert artifact["format"] == "markdown"
    assert artifact["kind"]
    assert artifact["structure"] in {"report", "checklist", "comparison"}
    assert artifact["completeness"] in {"COMPLETE", "PARTIAL", "NO_QUALIFIED_RESULT"}
    assert artifact["access_state"] == "AVAILABLE"
    assert artifact["size_bytes"] > 0
    assert re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
    assert artifact["created_at"].endswith("Z")


async def test_artifact_metadata_returns_the_document_contract(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    run_id, _ = await _start_run(client, api_context)
    artifact = (await client.get(f"/api/v1/runs/{run_id}/artifacts")).json()["items"][0]

    response = await client.get(f"/api/v1/artifacts/{artifact['id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == artifact["id"]
    assert body["access_state"] == "AVAILABLE"
    document = body["document"]
    assert document["schema_version"] == 1
    assert document["title"]
    assert document["structure"] == artifact["structure"]
    assert document["completeness"] == artifact["completeness"]
    assert document["completeness"] == "COMPLETE"
    assert document["directions"], "a delivered document must list every research direction"
    for direction in document["directions"]:
        assert direction["status"] in {"covered", "missing"}
        if direction["status"] == "missing":
            assert direction["gaps"]
    assert document["sources"], "a delivered document must reference its sources"
    assert document["gaps"] == []
    # The metadata ETag is the document manifest, the content ETag is the blob hash.
    assert response.headers["etag"].strip('"') == artifact["manifest_hash"]


async def test_content_is_utf8_markdown_with_hash_etag_and_safe_filename(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    run_id, _ = await _start_run(client, api_context)
    artifact = (await client.get(f"/api/v1/runs/{run_id}/artifacts")).json()["items"][0]

    response = await client.get(f"/api/v1/artifacts/{artifact['id']}/content")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/markdown")
    assert "utf-8" in response.headers["content-type"].lower()

    raw = response.content
    text = raw.decode("utf-8")
    assert text.startswith("# ")
    assert "完成程度：COMPLETE" in text

    digest = hashlib.sha256(raw).hexdigest()
    assert digest == artifact["sha256"]
    assert response.headers["etag"] == f'"{digest}"'
    assert response.headers["x-content-sha256"] == digest
    assert response.headers["cache-control"] == "no-store"

    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    filename = disposition.split("filename=", 1)[1].strip().strip('"')
    assert SAFE_FILENAME.fullmatch(filename), filename
    assert "/" not in filename and "\\" not in filename
    assert "\r" not in disposition and "\n" not in disposition
    assert artifact["id"][:8] in filename


async def test_repeated_reads_do_not_restart_research(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    run_id, _ = await _start_run(client, api_context)
    artifact = (await client.get(f"/api/v1/runs/{run_id}/artifacts")).json()["items"][0]
    before = await _counts(api_context)
    budget_before = (await client.get(f"/api/v1/runs/{run_id}")).json()["budget"]

    payloads: list[bytes] = []
    for _ in range(3):
        response = await client.get(f"/api/v1/artifacts/{artifact['id']}/content")
        assert response.status_code == 200, response.text
        payloads.append(response.content)
        # Re-reading the metadata must not create another artifact either.
        await client.get(f"/api/v1/artifacts/{artifact['id']}")
        await client.get(f"/api/v1/runs/{run_id}/artifacts")

    assert len(set(payloads)) == 1
    assert await _counts(api_context) == before
    budget_after = (await client.get(f"/api/v1/runs/{run_id}")).json()["budget"]
    assert budget_after["consumed"] == budget_before["consumed"]
    listing = (await client.get(f"/api/v1/runs/{run_id}/artifacts")).json()["items"]
    assert len(listing) == 1


async def test_deleted_or_expired_artifact_returns_410(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    run_id, _ = await _start_run(client, api_context)
    artifact = (await client.get(f"/api/v1/runs/{run_id}/artifacts")).json()["items"][0]

    for state in ("DELETED", "EXPIRED"):
        async with context.database.session() as session:
            row = await session.get(Artifact, artifact["id"])
            assert row is not None
            row.access_state = state
            await session.commit()

        for path in (
            f"/api/v1/artifacts/{artifact['id']}",
            f"/api/v1/artifacts/{artifact['id']}/content",
        ):
            response = await client.get(path)
            assert response.status_code == 410, (state, path, response.text)
            error = response.json()["error"]
            assert error["code"] == "ARTIFACT_UNAVAILABLE"
            assert error["retryable"] is False
            assert artifact["id"] not in error["message"]

    # Lifecycle state stays visible in the run listing instead of disappearing.
    states = {
        item["id"]: item["access_state"]
        for item in (await client.get(f"/api/v1/runs/{run_id}/artifacts")).json()["items"]
    }
    assert states[artifact["id"]] == "EXPIRED"


async def test_missing_blob_is_reported_as_an_integrity_error(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    run_id, _ = await _start_run(client, api_context)
    artifact = (await client.get(f"/api/v1/runs/{run_id}/artifacts")).json()["items"][0]
    blob = await _blob(api_context, artifact["id"])
    stored = context.config.artifacts_dir / blob.managed_relative_path
    stored.unlink()

    response = await client.get(f"/api/v1/artifacts/{artifact['id']}/content")
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "ARTIFACT_UNAVAILABLE"

    metadata = await client.get(f"/api/v1/artifacts/{artifact['id']}")
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["access_state"] == "INTEGRITY_ERROR"
    listed = (await client.get(f"/api/v1/runs/{run_id}/artifacts")).json()["items"][0]
    assert listed["access_state"] == "INTEGRITY_ERROR"


async def test_tampered_blob_is_reported_as_an_integrity_error(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    run_id, _ = await _start_run(client, api_context)
    artifact = (await client.get(f"/api/v1/runs/{run_id}/artifacts")).json()["items"][0]
    blob = await _blob(api_context, artifact["id"])
    stored = context.config.artifacts_dir / blob.managed_relative_path
    stored.write_bytes(b"# tampered\n")

    response = await client.get(f"/api/v1/artifacts/{artifact['id']}/content")
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "ARTIFACT_UNAVAILABLE"
    metadata = (await client.get(f"/api/v1/artifacts/{artifact['id']}")).json()
    assert metadata["access_state"] == "INTEGRITY_ERROR"


async def test_partial_document_is_never_reported_complete(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    run_id, status = await _start_run(
        client, api_context, scenario="engine.topic.missing_direction"
    )
    assert status == "SUCCEEDED"

    snapshot = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert snapshot["completeness"] == "PARTIAL"

    listed = (await client.get(f"/api/v1/runs/{run_id}/artifacts")).json()["items"][0]
    assert listed["completeness"] == "PARTIAL"

    metadata = (await client.get(f"/api/v1/artifacts/{listed['id']}")).json()
    assert metadata["completeness"] == "PARTIAL"
    assert metadata["document"]["completeness"] == "PARTIAL"
    missing = [d for d in metadata["document"]["directions"] if d["status"] == "missing"]
    assert missing and any(direction["gaps"] for direction in missing)

    body = (await client.get(f"/api/v1/artifacts/{listed['id']}/content")).text
    assert "完成程度：PARTIAL" in body
    assert "完成程度：COMPLETE" not in body
    assert "缺口" in body


async def test_unknown_artifact_and_run_return_structured_404(
    client: httpx.AsyncClient,
) -> None:
    unknown = "00000000-0000-4000-8000-000000000000"
    artifact = await client.get(f"/api/v1/artifacts/{unknown}")
    assert artifact.status_code == 404
    assert artifact.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"
    content = await client.get(f"/api/v1/artifacts/{unknown}/content")
    assert content.status_code == 404
    assert content.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"
    listing = await client.get(f"/api/v1/runs/{unknown}/artifacts")
    assert listing.status_code == 404
    assert listing.json()["error"]["code"] == "RUN_NOT_FOUND"

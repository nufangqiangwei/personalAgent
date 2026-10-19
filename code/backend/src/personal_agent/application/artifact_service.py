"""Artifact delivery service.

It is the only place that renders a research document into Markdown and commits
it through the managed-write protocol. The completeness (and therefore the run
status) is decided from the completion policy, never from the renderer. A partial
document is a successful delivery only when the policy allows partial output, and
it always stays labelled PARTIAL.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select

from personal_agent.adapters.persistence.artifact_models import Artifact, Blob
from personal_agent.adapters.persistence.blob_store import BlobStore
from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.execution_models import (
    ExecutionEvent,
    ExecutionSlot,
    WorkflowRun,
)
from personal_agent.adapters.rendering.markdown import render_document
from personal_agent.application.managed_writes import ArtifactDraft, ManagedWriter
from personal_agent.domain.artifacts import ArtifactState, ArtifactStructure
from personal_agent.domain.common import AppError, iso_z, new_id, stable_hash
from personal_agent.domain.document_content import (
    DocumentContent,
    build_content,
    decide_completeness,
)
from personal_agent.domain.execution import Completeness, EventKind, RunStatus
from personal_agent.ports.contracts import Clock


def _unavailable(message: str, status: int = 410) -> AppError:
    """Lifecycle and integrity failures share one redacted error code."""
    return AppError(code="ARTIFACT_UNAVAILABLE", message=message, status=status)


class ArtifactService:
    def __init__(
        self,
        database: Database,
        writer: ManagedWriter,
        store: BlobStore,
        clock: Clock,
        *,
        language: str = "zh-CN",
        detail: str = "standard",
    ) -> None:
        self._database = database
        self._writer = writer
        self._store = store
        self._clock = clock
        self._language = language
        self._detail = detail

    # ------------------------------------------------------------------
    async def deliver(
        self,
        *,
        run: WorkflowRun,
        definition: Any,
        step: Any,
        inputs: dict[str, Any],
        evidence: dict[str, Any],
        sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        policy = definition.completion_policy
        completeness = decide_completeness(
            required_directions=list(policy.required_direction_ids),
            evidence=evidence,
            allow_partial=policy.allow_partial_document,
        )
        structure = self._structure_for(step, run)
        content = build_content(
            definition_title=definition.name,
            structure=structure,
            completeness=completeness,
            inputs=inputs,
            evidence=evidence,
            sources=sources,
            direction_titles={direction.id: direction.title for direction in definition.directions},
        )
        markdown, document = render_document(
            content, structure=structure, language=self._language, detail=self._detail
        )
        draft = ArtifactDraft(
            workflow_version_id=run.workflow_version_id,
            completeness=completeness,
            manifest_hash=stable_hash(document),
            document=document,
            structure=ArtifactStructure(structure),
        )
        operation = await self._writer.begin(
            root_run_id=run.root_run_id,
            run_id=run.id,
            attempt_id=None,
            logical_effect_key=f"output:{run.id}:{step.id}",
            target_ref=f"{run.root_run_id}/{run.id}-{step.id}.md",
            intended_content=markdown.encode("utf-8"),
        )

        async def attach(session: Any, artifact: Artifact) -> None:
            row = await session.get(WorkflowRun, run.id)
            if row is None:
                return
            row.completeness = completeness
            row.status = (
                RunStatus.SUCCEEDED.value
                if completeness in {Completeness.COMPLETE.value, Completeness.PARTIAL.value}
                else RunStatus.FAILED.value
            )
            row.reason_code = (
                None if row.status == RunStatus.SUCCEEDED.value else "NO_QUALIFIED_RESULT"
            )
            row.current_step_id = step.id
            row.revision += 1
            session.add(
                ExecutionEvent(
                    id=new_id(),
                    run_id=row.id,
                    draft_id=None,
                    sequence=await self._next_sequence(session, row.id),
                    step_run_id=None,
                    attempt_id=None,
                    workflow_version_id=row.workflow_version_id,
                    occurred_at=self._clock.now(),
                    kind=EventKind.ARTIFACT_AVAILABLE.value,
                    status=row.status,
                    reason_code=None,
                    payload_json={"artifact_id": artifact.id, "completeness": completeness},
                    payload_ref=None,
                    schema_version=1,
                    provider_event_key=None,
                )
            )
            slot = await session.get(ExecutionSlot, 1)
            if slot is not None and slot.run_id == row.id:
                slot.run_id = None
                slot.draining = False
                slot.epoch += 1
                slot.revision += 1

        published = await self._writer.publish(
            operation_id=operation.id,
            run_id=run.id,
            workflow_version_id=run.workflow_version_id,
            content=markdown.encode("utf-8"),
            draft=draft,
            attach=attach,
        )
        return {
            "artifact_id": published.artifact_id,
            "operation_id": published.operation_id,
            "completeness": completeness,
            "structure": structure,
            "markdown": markdown,
            "document": document,
        }

    # ------------------------------------------------------------------
    async def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            artifacts = list(
                (
                    await session.scalars(
                        select(Artifact)
                        .where(Artifact.run_id == run_id)
                        .order_by(Artifact.created_at)
                    )
                ).all()
            )
            return [await self._metadata(session, artifact) for artifact in artifacts]

    async def get(self, artifact_id: str) -> dict[str, Any]:
        async with self._database.session() as session:
            artifact = await self._require(session, artifact_id)
            if artifact.access_state in {ArtifactState.DELETED.value, ArtifactState.EXPIRED.value}:
                raise _unavailable("the document is deleted or expired")
            metadata = await self._metadata(session, artifact)
            metadata["document"] = dict(artifact.document_json)
            return metadata

    async def content(self, artifact_id: str) -> tuple[bytes, dict[str, Any]]:
        async with self._database.session() as session:
            artifact = await self._require(session, artifact_id)
            if artifact.access_state in {ArtifactState.DELETED.value, ArtifactState.EXPIRED.value}:
                raise _unavailable("the document is deleted or expired")
            blob = await session.get(Blob, artifact.blob_id)
            if blob is None or blob.deleted_at is not None:
                raise _unavailable("the document content is unavailable")
            try:
                data = await self._store.get(blob.managed_relative_path)
            except FileNotFoundError as error:
                artifact.access_state = ArtifactState.INTEGRITY_ERROR.value
                await session.commit()
                raise _unavailable(
                    "the stored document failed its integrity check", 503
                ) from error

            if hashlib.sha256(data).hexdigest() != blob.sha256:
                artifact.access_state = ArtifactState.INTEGRITY_ERROR.value
                await session.commit()
                raise _unavailable("the stored document does not match its recorded hash", 503)
            metadata = await self._metadata(session, artifact)
            metadata["sha256"] = blob.sha256
            metadata["filename"] = (
                f"personal-agent-{artifact.run_id[:8]}-{artifact.id[:8]}.md"
            )
            return data, metadata

    # ------------------------------------------------------------------
    async def _metadata(self, session: Any, artifact: Artifact) -> dict[str, Any]:
        blob = await session.get(Blob, artifact.blob_id)
        return {
            "id": artifact.id,
            "run_id": artifact.run_id,
            "workflow_version_id": artifact.workflow_version_id,
            "kind": artifact.kind,
            "format": artifact.format,
            "structure": artifact.structure,
            "completeness": artifact.completeness,
            "manifest_hash": artifact.manifest_hash,
            "created_at": iso_z(artifact.created_at),
            "access_state": artifact.access_state,
            "size_bytes": blob.size_bytes if blob else 0,
            "sha256": blob.sha256 if blob else "",
        }

    async def _require(self, session: Any, artifact_id: str) -> Artifact:
        artifact = await session.get(Artifact, artifact_id)
        if artifact is None:
            raise AppError(code="ARTIFACT_NOT_FOUND", message="artifact not found", status=404)
        return artifact

    def _structure_for(self, step: Any, run: WorkflowRun) -> str:
        config = getattr(step, "config", None)
        structure = getattr(config, "structure", None)
        output_spec = run.input_snapshot_json.get("output_spec") or {}
        requested = output_spec.get("structure")
        if isinstance(requested, str) and requested in {"report", "checklist", "comparison"}:
            return requested
        return str(structure or "report")

    @staticmethod
    async def _next_sequence(session: Any, run_id: str) -> int:
        from sqlalchemy import func

        value = await session.scalar(
            select(func.max(ExecutionEvent.sequence)).where(ExecutionEvent.run_id == run_id)
        )
        return int(value or 0) + 1


def document_content_for(
    *,
    definition: Any,
    inputs: dict[str, Any],
    evidence: dict[str, Any],
    sources: list[dict[str, Any]],
    completeness: str,
    structure: str,
) -> DocumentContent:
    return build_content(
        definition_title=definition.name,
        structure=structure,
        completeness=completeness,
        inputs=inputs,
        evidence=evidence,
        sources=sources,
        direction_titles={direction.id: direction.title for direction in definition.directions},
    )

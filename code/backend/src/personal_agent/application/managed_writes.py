"""Managed write protocol: intent -> file publish -> verified commit.

The protocol cannot make the database and filesystem atomic, so it guarantees the
weaker, correct property: a success record never precedes a complete artifact.

1. a short transaction persists ``INTENT`` (target, intended hash, effect key);
2. outside any transaction the content is written atomically and hashed;
3. a short transaction re-checks the published hash, then commits ``APPLIED``,
   the ``Blob``/``Artifact`` rows and the caller's business rows together.

A crash after step 2 leaves an orphan file and an ``INTENT`` row; ``reconcile``
then classifies the target instead of guessing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.adapters.persistence.artifact_models import (
    Artifact,
    Blob,
    SideEffectOperation,
)
from personal_agent.adapters.persistence.blob_store import BlobStore
from personal_agent.adapters.persistence.database import Database
from personal_agent.domain.artifacts import (
    ArtifactFormat,
    ArtifactKind,
    ArtifactState,
    ArtifactStructure,
    SideEffectStatus,
    classify_observation,
)
from personal_agent.domain.common import AppError, ErrorCategory, iso_z, new_id
from personal_agent.ports.contracts import Clock

MEDIA_TYPE_MARKDOWN = "text/markdown; charset=utf-8"


@dataclass(frozen=True, slots=True)
class ArtifactDraft:
    workflow_version_id: str
    completeness: str
    manifest_hash: str
    document: dict
    structure: ArtifactStructure = ArtifactStructure.REPORT
    kind: ArtifactKind = ArtifactKind.DOCUMENT
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PublishResult:
    operation_id: str
    status: SideEffectStatus
    artifact_id: str
    blob_id: str
    relative_path: str
    sha256: str


AttachHook = Callable[[AsyncSession, Artifact], Awaitable[None]]


class ManagedWriter:
    def __init__(self, database: Database, store: BlobStore, clock: Clock) -> None:
        self._database = database
        self._store = store
        self._clock = clock

    @property
    def store(self) -> BlobStore:
        return self._store

    async def begin(
        self,
        *,
        root_run_id: str,
        run_id: str,
        attempt_id: str | None,
        logical_effect_key: str,
        target_ref: str,
        intended_content: bytes,
        expected_before_hash: str | None = None,
    ) -> SideEffectOperation:
        import hashlib

        intended_hash = hashlib.sha256(intended_content).hexdigest()
        async with self._database.session() as session:
            existing = await session.scalar(
                select(SideEffectOperation).where(
                    SideEffectOperation.root_run_id == root_run_id,
                    SideEffectOperation.logical_effect_key == logical_effect_key,
                )
            )
            if existing is not None:
                if existing.intended_after_hash != intended_hash:
                    raise AppError(
                        code="SIDE_EFFECT_UNRESOLVED",
                        message=(
                            "the same logical effect already exists with different content; "
                            "a new attempt id is not a new authorization"
                        ),
                        category=ErrorCategory.INTEGRITY,
                        status=409,
                        details={"operation_id": existing.id, "status": existing.status},
                    )
                return existing
            operation = SideEffectOperation(
                id=new_id(),
                root_run_id=root_run_id,
                run_id=run_id,
                step_attempt_id=attempt_id,
                logical_effect_key=logical_effect_key,
                target_ref=target_ref,
                expected_before_hash=expected_before_hash,
                intended_after_hash=intended_hash,
                intent_ref=f"intent:{logical_effect_key}",
                status=SideEffectStatus.INTENT.value,
                observed_hash=None,
                verified_at=None,
                verification_ref=None,
                revision=1,
                created_at=self._clock.now(),
            )
            session.add(operation)
            await session.commit()
            return operation

    async def publish(
        self,
        *,
        operation_id: str,
        run_id: str,
        workflow_version_id: str,
        content: bytes,
        draft: ArtifactDraft,
        attach: AttachHook | None = None,
    ) -> PublishResult:
        async with self._database.session() as session:
            operation = await session.get(SideEffectOperation, operation_id)
            if operation is None:
                raise AppError(
                    code="SIDE_EFFECT_NOT_FOUND", message="operation not found", status=404
                )
            if operation.status == SideEffectStatus.APPLIED.value:
                artifact = await session.scalar(
                    select(Artifact).where(Artifact.operation_id == operation_id)
                )
                if artifact is None:
                    raise AppError(
                        code="SIDE_EFFECT_UNRESOLVED",
                        message="the operation is APPLIED but its artifact is missing",
                        category=ErrorCategory.INTEGRITY,
                        status=409,
                    )
                blob = await session.get(Blob, artifact.blob_id)
                return PublishResult(
                    operation_id=operation_id,
                    status=SideEffectStatus.APPLIED,
                    artifact_id=artifact.id,
                    blob_id=artifact.blob_id,
                    relative_path=blob.managed_relative_path if blob else "",
                    sha256=blob.sha256 if blob else "",
                )
            if operation.status not in {
                SideEffectStatus.INTENT.value,
                SideEffectStatus.NOT_APPLIED.value,
            }:
                raise AppError(
                    code="SIDE_EFFECT_UNRESOLVED",
                    message="the managed write is not safe to retry",
                    category=ErrorCategory.INTEGRITY,
                    status=409,
                    details={"status": operation.status},
                )
            relative_path = operation.target_ref
            intended_hash = operation.intended_after_hash

        # Step 2: publish the file outside any database transaction.
        stored = await self._store.put(relative_path, content)
        if stored.sha256 != intended_hash:
            raise AppError(
                code="ARTIFACT_INTEGRITY_ERROR",
                message="the published content does not match the intent",
                category=ErrorCategory.INTEGRITY,
                status=500,
            )

        blob_id = new_id()
        artifact_id = new_id()
        try:
            async with self._database.session() as session:
                operation_row = await session.get(SideEffectOperation, operation_id)
                if operation_row is None:
                    raise AppError(
                        code="SIDE_EFFECT_NOT_FOUND", message="operation not found", status=404
                    )
                blob = Blob(
                    id=blob_id,
                    managed_relative_path=stored.relative_path,
                    sha256=stored.sha256,
                    size_bytes=stored.size_bytes,
                    media_type=MEDIA_TYPE_MARKDOWN,
                    created_at=self._clock.now(),
                    expires_at=draft.expires_at,
                    deleted_at=None,
                )
                session.add(blob)
                await session.flush()
                artifact = Artifact(
                    id=artifact_id,
                    run_id=run_id,
                    attempt_id=operation_row.step_attempt_id,
                    workflow_version_id=workflow_version_id,
                    operation_id=operation_id,
                    blob_id=blob_id,
                    kind=draft.kind.value,
                    format=ArtifactFormat.MARKDOWN.value,
                    structure=draft.structure.value,
                    completeness=draft.completeness,
                    manifest_hash=draft.manifest_hash,
                    document_json=draft.document,
                    created_at=self._clock.now(),
                    access_state=ArtifactState.AVAILABLE.value,
                )
                session.add(artifact)
                operation_row.status = SideEffectStatus.APPLIED.value
                operation_row.observed_hash = stored.sha256
                operation_row.verified_at = self._clock.now()
                operation_row.verification_ref = f"blob:{blob_id}"
                operation_row.revision += 1
                if attach is not None:
                    await attach(session, artifact)
                await session.commit()
        except Exception:
            # The file may exist as an orphan; the operation stays INTENT and no
            # artifact row claims success. Orphan cleanup must never delete a
            # file still referenced by a committed artifact.
            raise

        return PublishResult(
            operation_id=operation_id,
            status=SideEffectStatus.APPLIED,
            artifact_id=artifact_id,
            blob_id=blob_id,
            relative_path=stored.relative_path,
            sha256=stored.sha256,
        )

    async def reconcile(self, operation_id: str) -> SideEffectStatus:
        async with self._database.session() as session:
            operation = await session.get(SideEffectOperation, operation_id)
            if operation is None:
                raise AppError(
                    code="SIDE_EFFECT_NOT_FOUND", message="operation not found", status=404
                )
            try:
                observed, _size = await self._store.digest(operation.target_ref)
            except FileNotFoundError:
                observed = None
            status = classify_observation(
                intended_after_hash=operation.intended_after_hash,
                observed_hash=observed,
                previous_hash=operation.expected_before_hash,
            )
            operation.status = status.value
            operation.observed_hash = observed
            operation.verified_at = self._clock.now()
            operation.verification_ref = f"reconcile:{iso_z(self._clock.now())}"
            operation.revision += 1
            await session.commit()
            return status

    async def describe(self, operation_id: str) -> dict[str, object]:
        async with self._database.session() as session:
            operation = await session.get(SideEffectOperation, operation_id)
            if operation is None:
                raise AppError(
                    code="SIDE_EFFECT_NOT_FOUND", message="operation not found", status=404
                )
            return {
                "id": operation.id,
                "root_run_id": operation.root_run_id,
                "run_id": operation.run_id,
                "logical_effect_key": operation.logical_effect_key,
                "target_ref": operation.target_ref,
                "status": operation.status,
                "intended_after_hash": operation.intended_after_hash,
                "observed_hash": operation.observed_hash,
                "verified_at": iso_z(operation.verified_at) if operation.verified_at else None,
                "verification_ref": operation.verification_ref,
            }


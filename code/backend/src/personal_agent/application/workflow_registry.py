"""Workflow registry: validation, immutable versions and the latest pointer.

The registry is the only writer of workflow versions. It never infers the latest
executable version from the version number, reuses a version when the content hash
did not change, and refuses a save whose editing baseline moved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.uow import cas_update
from personal_agent.adapters.persistence.workflow_models import Workflow, WorkflowVersion
from personal_agent.domain.common import AppError, new_id
from personal_agent.domain.workflow_schema import WorkflowDefinition
from personal_agent.domain.workflow_validation import (
    ValidationReport,
    require_valid,
    validate_definition,
)
from personal_agent.domain.workflows import (
    assert_base_matches,
    assert_editable,
    executable_flag,
    next_version_no,
)
from personal_agent.ports.contracts import Clock


@dataclass(frozen=True, slots=True)
class SaveOutcome:
    version: WorkflowVersion
    created: bool
    promoted: bool


class WorkflowRegistry:
    def __init__(self, database: Database, clock: Clock) -> None:
        self._database = database
        self._clock = clock

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    async def list_summaries(self) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = (
                await session.scalars(select(Workflow).order_by(Workflow.created_at, Workflow.id))
            ).all()
            summaries: list[dict[str, Any]] = []
            for workflow in rows:
                latest = await self._latest_executable(session, workflow)
                payload = await self._summary(session, workflow, latest)
                if latest is not None and workflow.disabled_at is None:
                    summaries.append(payload)
            return summaries

    async def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        async with self._database.session() as session:
            workflow = await self._require_workflow(session, workflow_id)
            latest = await self._latest_executable(session, workflow)
            return await self._summary(session, workflow, latest)

    async def list_versions(self, workflow_id: str) -> list[WorkflowVersion]:
        async with self._database.session() as session:
            await self._require_workflow(session, workflow_id)
            return list(
                (
                    await session.scalars(
                        select(WorkflowVersion)
                        .where(WorkflowVersion.workflow_id == workflow_id)
                        .order_by(WorkflowVersion.version_no.desc())
                    )
                ).all()
            )

    async def get_version(self, workflow_id: str, version_id: str) -> WorkflowVersion:
        async with self._database.session() as session:
            version = await session.scalar(
                select(WorkflowVersion).where(
                    WorkflowVersion.workflow_id == workflow_id,
                    WorkflowVersion.id == version_id,
                )
            )
            if version is None:
                raise AppError(
                    code="VERSION_NOT_FOUND",
                    message="workflow version not found",
                    status=404,
                )
            return version

    async def latest_executable(self, workflow_id: str) -> WorkflowVersion | None:
        async with self._database.session() as session:
            workflow = await self._require_workflow(session, workflow_id)
            return await self._latest_executable(session, workflow)

    async def definition_of(self, version_id: str) -> tuple[WorkflowVersion, WorkflowDefinition]:
        async with self._database.session() as session:
            version = await session.get(WorkflowVersion, version_id)
            if version is None:
                raise AppError(
                    code="VERSION_NOT_FOUND", message="workflow version not found", status=404
                )
            return version, WorkflowDefinition.model_validate(version.definition_json)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    async def create(
        self,
        *,
        definition: dict[str, Any],
        change_summary: str,
        confirm_executable: bool,
    ) -> SaveOutcome:
        parsed = require_valid(definition)
        report = validate_definition(definition)
        async with self._database.session() as session:
            existing = await session.get(Workflow, parsed.workflow_id)
            if existing is not None:
                raise AppError(
                    code="WORKFLOW_EXISTS",
                    message="a workflow with this id already exists",
                    status=409,
                )
            now = self._clock.now()
            workflow = Workflow(
                id=parsed.workflow_id,
                latest_executable_version_id=None,
                disabled_at=None,
                disabled_reason=None,
                created_at=now,
                revision=1,
            )
            session.add(workflow)
            await session.flush()
            payload = dict(definition)
            payload["version"] = 1
            version = WorkflowVersion(
                id=new_id(),
                workflow_id=parsed.workflow_id,
                version_no=1,
                definition_json=payload,
                definition_hash=report.definition_hash,
                schema_version="1",
                saved_at=now,
                change_summary=change_summary,
                executable=executable_flag(validated=True, confirmed=confirm_executable),
                validation_report_json=report.as_dict(),
                revision=1,
            )
            session.add(version)
            await session.flush()
            if version.executable:
                workflow.latest_executable_version_id = version.id
                workflow.revision += 1
            await session.commit()
            return SaveOutcome(version=version, created=True, promoted=version.executable)

    async def save_version(
        self,
        *,
        workflow_id: str,
        base_version_id: str,
        definition: dict[str, Any],
        change_summary: str,
        confirm_executable: bool,
    ) -> SaveOutcome:
        report: ValidationReport = validate_definition(definition)
        require_valid(definition)
        async with self._database.session() as session:
            workflow = await self._require_workflow(session, workflow_id)
            assert_editable(workflow.disabled_at)
            current_latest = await self._current_latest_version(session, workflow)
            if current_latest is not None:
                assert_base_matches(current_latest.id, base_version_id)
            elif base_version_id not in {"", None}:
                raise AppError(
                    code="VERSION_CHANGED",
                    message="the editor baseline no longer exists",
                    status=409,
                )

            duplicate = await session.scalar(
                select(WorkflowVersion).where(
                    WorkflowVersion.workflow_id == workflow_id,
                    WorkflowVersion.definition_hash == report.definition_hash,
                )
            )
            if duplicate is not None:
                promoted = False
                if confirm_executable and not duplicate.executable:
                    duplicate.executable = True
                    duplicate.revision += 1
                    workflow.latest_executable_version_id = duplicate.id
                    workflow.revision += 1
                    promoted = True
                    await session.commit()
                return SaveOutcome(version=duplicate, created=False, promoted=promoted)

            numbers = list(
                (
                    await session.scalars(
                        select(WorkflowVersion.version_no).where(
                            WorkflowVersion.workflow_id == workflow_id
                        )
                    )
                ).all()
            )
            version_no = next_version_no(numbers)
            payload = dict(definition)
            payload["version"] = version_no
            version = WorkflowVersion(
                id=new_id(),
                workflow_id=workflow_id,
                version_no=version_no,
                definition_json=payload,
                definition_hash=report.definition_hash,
                schema_version="1",
                saved_at=self._clock.now(),
                change_summary=change_summary,
                executable=executable_flag(validated=True, confirmed=confirm_executable),
                validation_report_json=report.as_dict(),
                revision=1,
            )
            session.add(version)
            await session.flush()
            if version.executable:
                await cas_update(
                    session,
                    Workflow,
                    identity={"id": workflow_id},
                    expected_revision=workflow.revision,
                    values={"latest_executable_version_id": version.id},
                    stale_code="VERSION_CONFLICT",
                    stale_status=409,
                )
            await session.commit()
            return SaveOutcome(version=version, created=True, promoted=version.executable)

    async def disable(self, workflow_id: str, reason: str) -> Workflow:
        if not reason.strip():
            raise AppError(
                code="DISABLE_REASON_REQUIRED",
                message="disabling a workflow requires a reason",
                status=422,
            )
        async with self._database.session() as session:
            workflow = await self._require_workflow(session, workflow_id)
            if workflow.disabled_at is not None:
                return workflow
            workflow.disabled_at = self._clock.now()
            workflow.disabled_reason = reason.strip()
            workflow.revision += 1
            await session.commit()
            return workflow

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _require_workflow(self, session: AsyncSession, workflow_id: str) -> Workflow:
        workflow = await session.get(Workflow, workflow_id)
        if workflow is None:
            raise AppError(
                code="WORKFLOW_NOT_FOUND", message="workflow not found", status=404
            )
        return workflow

    async def _latest_executable(
        self, session: AsyncSession, workflow: Workflow
    ) -> WorkflowVersion | None:
        if workflow.latest_executable_version_id is None:
            return None
        version = await session.get(WorkflowVersion, workflow.latest_executable_version_id)
        if version is None or version.workflow_id != workflow.id or not version.executable:
            return None
        return version

    async def _current_latest_version(
        self, session: AsyncSession, workflow: Workflow
    ) -> WorkflowVersion | None:
        return await session.scalar(
            select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow.id)
            .order_by(WorkflowVersion.version_no.desc())
            .limit(1)
        )

    async def _summary(
        self,
        session: AsyncSession,
        workflow: Workflow,
        latest: WorkflowVersion | None,
    ) -> dict[str, Any]:
        if latest is None:
            definition: dict[str, Any] = {}
        else:
            definition = dict(latest.definition_json)
        return {
            "id": workflow.id,
            "name": definition.get("name", ""),
            "description": definition.get("description", ""),
            "applicability": definition.get("applicability", ""),
            "disabled": workflow.disabled_at is not None,
            "revision": workflow.revision,
            "latest_version": None
            if latest is None
            else {
                "id": latest.id,
                "version_no": latest.version_no,
                "definition_hash": latest.definition_hash,
                "saved_at": latest.saved_at.isoformat().replace("+00:00", "Z"),
                "executable": latest.executable,
            },
            "directions": [
                dict(item) for item in definition.get("directions", [])
            ],
            "required_inputs": sorted(
                (definition.get("input_schema") or {}).get("required", [])
            ),
            "steps": [
                {"id": step.get("id"), "name": step.get("name"), "type": step.get("type")}
                for step in definition.get("steps", [])
            ],
            "output": {
                "structure": next(
                    (
                        step.get("config", {}).get("structure")
                        for step in definition.get("steps", [])
                        if step.get("type") == "Output"
                    ),
                    None,
                )
            },
            "version_count": await session.scalar(
                select(func.count())
                .select_from(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == workflow.id)
            ),
        }


def serialize_version(version: WorkflowVersion) -> dict[str, Any]:
    return {
        "id": version.id,
        "workflow_id": version.workflow_id,
        "version_no": version.version_no,
        "definition": version.definition_json,
        "definition_hash": version.definition_hash,
        "schema_version": version.schema_version,
        "saved_at": version.saved_at.isoformat().replace("+00:00", "Z"),
        "change_summary": version.change_summary,
        "executable": version.executable,
        "validation_report": version.validation_report_json,
        "revision": version.revision,
    }


def serialize_workflow(workflow: Workflow) -> dict[str, Any]:
    return {
        "id": workflow.id,
        "latest_executable_version_id": workflow.latest_executable_version_id,
        "disabled": workflow.disabled_at is not None,
        "revision": workflow.revision,
    }

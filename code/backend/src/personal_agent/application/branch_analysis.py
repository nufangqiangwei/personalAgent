"""Branch impact analysis: what a changed requirement can reach.

The analysis is deliberately conservative. It follows the declared ``input_reads`` and
``depends_on`` edges of the fixed version, computes the transitive closure of the
changed input paths, and marks every step at or after the chosen start point as
re-running. A start point whose prerequisites are not satisfied, or whose impact cannot
be established, is ``BLOCKED`` with a suggested earlier point; nothing here moves the
start point on its own.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from personal_agent.adapters.persistence.artifact_models import Artifact, Blob
from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.execution_models import StepRun, WorkflowRun
from personal_agent.application.workflow_registry import WorkflowRegistry
from personal_agent.domain.branches import (
    BranchStatus,
    StepImpact,
    compute_impact,
    normalize_path,
)
from personal_agent.domain.common import AppError, iso_z, stable_hash
from personal_agent.ports.contracts import Clock


class BranchAnalyzer:
    def __init__(
        self, database: Database, clock: Clock, registry: WorkflowRegistry
    ) -> None:
        self._database = database
        self._clock = clock
        self._registry = registry

    async def analyze(
        self,
        *,
        run_id: str,
        selected_step_id: str,
        changed_paths: list[str],
    ) -> dict[str, Any]:
        async with self._database.session() as session:
            run = await session.get(WorkflowRun, run_id)
            if run is None:
                raise AppError(code="RUN_NOT_FOUND", message="run not found", status=404)
            step_rows = {
                row.step_id: row
                for row in (
                    await session.scalars(select(StepRun).where(StepRun.run_id == run_id))
                ).all()
            }
            artifacts = list(
                (
                    await session.scalars(
                        select(Artifact).where(Artifact.run_id == run_id)
                    )
                ).all()
            )
            blobs = {}
            for artifact in artifacts:
                blob = await session.get(Blob, artifact.blob_id)
                if blob is not None:
                    blobs[artifact.id] = blob
            attempt_ids = [item.attempt_id for item in artifacts if item.attempt_id]
            step_of_attempt: dict[str, str] = {}
            if attempt_ids:
                from personal_agent.adapters.persistence.execution_models import (
                    StepAttempt,
                )

                rows = (
                    await session.execute(
                        select(StepAttempt.id, StepRun.step_id)
                        .join(StepRun, StepAttempt.step_run_id == StepRun.id)
                        .where(StepAttempt.id.in_(attempt_ids))
                    )
                ).all()
                step_of_attempt = {attempt_id: step_id for attempt_id, step_id in rows}
            version_id = run.workflow_version_id
            run_hash = run.input_hash

        _version, definition = await self._registry.definition_of(version_id)
        declared = [
            StepImpact(
                step_id=step.id,
                depends_on=tuple(step.depends_on),
                input_reads=tuple(step.input_reads) + (
                    ("/preference_order",) if step.type == "AgentTask"
                    else ("/output_spec",) if step.type == "Output" else ()
                ),
            )
            for step in definition.steps
        ]
        known = {step.step_id for step in declared}
        if selected_step_id not in known:
            raise AppError(
                code="START_POINT_INVALID",
                message=f"step {selected_step_id!r} does not exist in the fixed version",
                status=422,
            )
        reachable = _reachable_from(declared, selected_step_id)

        invalid_artifacts = [
            artifact.id
            for artifact in artifacts
            if not self._artifact_usable(artifact, blobs.get(artifact.id))
        ]
        invalid_steps = {
            step_of_attempt[artifact.attempt_id]
            for artifact in artifacts
            if artifact.id in invalid_artifacts
            and artifact.attempt_id is not None
            and artifact.attempt_id in step_of_attempt
        }
        untraceable = [
            artifact.id
            for artifact in artifacts
            if artifact.id in invalid_artifacts and artifact.id not in {
                item.id
                for item in artifacts
                if item.attempt_id is not None and item.attempt_id in step_of_attempt
            }
        ]
        report = compute_impact(
            steps=declared,
            changed_paths=changed_paths,
            start_step_id=selected_step_id,
            reachable_from_start=reachable,
            invalid_artifact_step_ids=invalid_steps,
        )
        declared_inputs = set(definition.input_schema.get("properties", {}))
        unclassified = [path for path in changed_paths if normalize_path(path).split("/")[1]
                        not in declared_inputs | {"preference_order", "output_spec"}]
        if unclassified:
            report.blocked_reasons.append(
                "要求差异无法匹配流程输入声明，影响范围无法确定：" + "、".join(unclassified)
            )
        if untraceable:
            report.blocked_reasons.append(
                "以下失效成果无法追溯到产生它的步骤，需人工确认："
                + "、".join(sorted(untraceable))
            )

        missing = [
            step_id
            for step_id in _prerequisites(declared, selected_step_id)
            if step_rows.get(step_id) is None
            or step_rows[step_id].status != "SUCCEEDED"
        ]
        if missing:
            report.blocked_reasons.append(
                "起点前置步骤尚未成功：" + "、".join(sorted(missing))
            )

        # A prerequisite whose result depends on a changed input is not a valid
        # dependency any more: the start point must move earlier.
        tainted = sorted(
            set(_prerequisites(declared, selected_step_id))
            & set(report.affected_step_ids)
        )
        if tainted:
            report.blocked_reasons.append(
                "起点前置步骤依赖已变化的要求，需选择更早的起点："
                + "、".join(tainted)
            )

        reusable_after_prerequisites = [
            step_id for step_id in report.reusable_step_ids if step_id not in missing
        ]
        rejected = [
            step_id for step_id in report.reusable_step_ids if step_id in missing
        ]
        if rejected:
            report.blocked_reasons.append(
                "以下成果不能作为依赖复用：" + "、".join(sorted(rejected))
            )
            report.reusable_step_ids = reusable_after_prerequisites

        suggested = _suggest_earlier(declared, missing or tainted, selected_step_id)
        payload = report.as_dict()
        payload.update(
            {
                "run_id": run_id,
                "workflow_version_id": version_id,
                "input_hash": run_hash,
                "selected_step_id": selected_step_id,
                "effective_step_id": selected_step_id,
                "suggested_start_step_id": suggested,
                "invalidated_artifact_ids": sorted(invalid_artifacts),
                "invalidated_step_ids": sorted(invalid_steps),
            }
        )
        payload["report_hash"] = stable_hash(payload)
        payload["status"] = (
            BranchStatus.BLOCKED.value
            if report.blocked_reasons
            else BranchStatus.READY.value
        )
        payload["generated_at"] = iso_z(self._clock.now())
        return payload

    # ------------------------------------------------------------------
    @staticmethod
    def changed_paths(
        original: dict[str, Any], updated: dict[str, Any]
    ) -> list[str]:
        """Every field path whose value differs from the frozen confirmed input."""
        original_fields = original.get("fields", original)
        updated_fields = updated.get("fields", updated)
        paths: set[str] = set()
        for key, value in updated_fields.items():
            before = original_fields.get(key)
            before_value = before.get("value") if isinstance(before, dict) else before
            after_value = value.get("value") if isinstance(value, dict) else value
            if before_value != after_value:
                paths.add(normalize_path(str(key)))
        for key in original_fields:
            if key not in updated_fields:
                paths.add(normalize_path(str(key)))
        if "fields" in updated:
            for key, default in (("preference_order", []), ("output_spec", {})):
                if original.get(key, default) != updated.get(key, default):
                    paths.add(normalize_path(key))
        return sorted(paths)

    @staticmethod
    def _artifact_usable(artifact: Artifact, blob: Blob | None) -> bool:
        if artifact.access_state != "AVAILABLE":
            return False
        if blob is None:
            return False
        if blob.deleted_at is not None:
            return False
        return True


def _reachable_from(steps: list[StepImpact], start: str) -> set[str]:
    graph = {step.step_id: list(step.depends_on) for step in steps}
    reachable = {start}
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for candidate, deps in graph.items():
            if current in deps and candidate not in reachable:
                reachable.add(candidate)
                frontier.append(candidate)
    return reachable


def _prerequisites(steps: list[StepImpact], start: str) -> set[str]:
    graph = {step.step_id: list(step.depends_on) for step in steps}
    found: set[str] = set()
    frontier = list(graph.get(start, []))
    while frontier:
        current = frontier.pop()
        if current in found or current == start:
            continue
        found.add(current)
        frontier.extend(graph.get(current, []))
    return found


def _suggest_earlier(
    steps: list[StepImpact], missing: list[str], selected: str
) -> str | None:
    if missing:
        return sorted(missing)[0]
    ordered = [step.step_id for step in steps]
    if selected in ordered and ordered.index(selected) > 0:
        return ordered[ordered.index(selected) - 1]
    return None


__all__ = ["BranchAnalyzer"]

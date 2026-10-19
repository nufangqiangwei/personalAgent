"""Branch domain rules.

A branch reuses only results produced *before* its start point and only when the
changed inputs cannot reach them. Impact that cannot be established is ``BLOCKED``;
nothing here silently moves the start point forward. The decisions are pure so they
can be replayed from persisted records without the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class BranchStatus(StrEnum):
    DRAFT = "DRAFT"
    BLOCKED = "BLOCKED"
    READY = "READY"
    CONFIRMED = "CONFIRMED"


class ReuseStatus(StrEnum):
    REUSABLE = "REUSABLE"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class StepImpact:
    """Which declared reads make each step depend on which input paths."""

    step_id: str
    depends_on: tuple[str, ...]
    input_reads: tuple[str, ...]


@dataclass(slots=True)
class ImpactReport:
    changed_paths: list[str]
    input_reads_closure: list[str]
    affected_step_ids: list[str]
    reusable_step_ids: list[str]
    blocked_reasons: list[str] = field(default_factory=list)

    @property
    def status(self) -> BranchStatus:
        if self.blocked_reasons:
            return BranchStatus.BLOCKED
        return BranchStatus.READY

    def as_dict(self) -> dict[str, Any]:
        return {
            "changed_paths": list(self.changed_paths),
            "input_reads_closure": list(self.input_reads_closure),
            "affected_step_ids": list(self.affected_step_ids),
            "reusable_step_ids": list(self.reusable_step_ids),
            "blocked_reasons": list(self.blocked_reasons),
            "status": self.status.value,
        }


def normalize_path(path: str) -> str:
    """Compare declared reads as absolute JSON pointers without a trailing slash."""
    trimmed = path.strip()
    if not trimmed:
        return ""
    if not trimmed.startswith("/"):
        trimmed = "/" + trimmed
    return trimmed.rstrip("/") or "/"


def overlaps(changed: str, read: str) -> bool:
    """A step must re-run if it reads a file or anything beneath it.

    ``/topic`` is affected by a change to ``/topic``; a change to ``/topic/scope``
    is also visible to a step that reads ``/topic`` (it gets the whole object).
    """
    left = normalize_path(changed)
    right = normalize_path(read)
    if left == right:
        return True
    return right.startswith(left + "/") or left.startswith(right + "/")


def compute_impact(
    *,
    steps: list[StepImpact],
    changed_paths: list[str],
    start_step_id: str,
    reachable_from_start: set[str],
    invalid_artifact_step_ids: set[str] | None = None,
) -> ImpactReport:
    """Compute the reusable prefix and every step the change can reach.

    ``reachable_from_start`` is every step at or after the chosen start point on the
    declared graph. Steps before the start point may be reused, but only when none of
    their declared reads overlaps a changed input and their results are still valid.
    """
    changed = [normalize_path(path) for path in changed_paths if path.strip()]
    invalid = invalid_artifact_step_ids or set()
    report = ImpactReport(
        changed_paths=changed, input_reads_closure=[], affected_step_ids=[], reusable_step_ids=[]
    )

    affected: set[str] = set()
    closure: set[str] = set()
    for step in steps:
        reads = [normalize_path(item) for item in step.input_reads]
        touched = any(overlaps(path, item) for path in changed for item in reads)
        if touched:
            affected.add(step.step_id)
            closure.update(reads)
            for downstream in _descendants(steps, step.step_id):
                affected.add(downstream)

    for step in steps:
        if step.step_id not in reachable_from_start and step.step_id not in affected:
            if step.step_id in invalid:
                affected.add(step.step_id)
                continue
            report.reusable_step_ids.append(step.step_id)

    # Actions after the start point always run again, whatever they read.
    for step in steps:
        if step.step_id in reachable_from_start:
            affected.add(step.step_id)

    report.affected_step_ids = sorted(affected)
    report.input_reads_closure = sorted(closure)

    if start_step_id not in {step.step_id for step in steps}:
        report.blocked_reasons.append(f"起点 {start_step_id} 不在该流程定义中")
    if not changed:
        report.blocked_reasons.append("没有可识别的要求变化，无法确定影响范围")
    for step_id in report.reusable_step_ids:
        if step_id in affected:
            report.blocked_reasons.append(f"步骤 {step_id} 影响不明，需选择更早的起点")
    return report


def _descendants(steps: list[StepImpact], step_id: str) -> set[str]:
    graph = {step.step_id: list(step.depends_on) for step in steps}
    found: set[str] = set()
    frontier = [step_id]
    while frontier:
        current = frontier.pop()
        for candidate, deps in graph.items():
            if current in deps and candidate not in found:
                found.add(candidate)
                frontier.append(candidate)
    return found


__all__ = [
    "BranchStatus",
    "ImpactReport",
    "ReuseStatus",
    "StepImpact",
    "compute_impact",
    "normalize_path",
    "overlaps",
]

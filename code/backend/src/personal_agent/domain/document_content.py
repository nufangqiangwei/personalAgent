"""Document content contract.

The delivered Markdown is rendered only from this structure. Important external
facts must carry sources or be explicitly unverified; only candidates whose hard
constraints are all SATISFIED may be presented as qualified recommendations; the
priority used for ordering is the confirmed one, never an invented weight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from personal_agent.domain.common import AppError, stable_hash

VALID_STRUCTURES = {"report", "checklist", "comparison"}
VALID_COMPLETENESS = {"COMPLETE", "PARTIAL", "NO_QUALIFIED_RESULT"}


@dataclass(frozen=True, slots=True)
class DirectionResult:
    id: str
    title: str
    status: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DocumentContent:
    title: str
    structure: str
    completeness: str
    objective: str
    constraints: list[str]
    directions: list[DirectionResult]
    qualified: list[dict[str, Any]]
    unknown_candidates: list[dict[str, Any]]
    rejected_candidates: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    gaps: list[str]
    unverified: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if self.structure not in VALID_STRUCTURES:
            raise AppError(
                code="DOCUMENT_STRUCTURE_INVALID",
                message=f"unsupported document structure {self.structure!r}",
                status=422,
            )
        if self.completeness not in VALID_COMPLETENESS:
            raise AppError(
                code="DOCUMENT_COMPLETENESS_INVALID",
                message="completeness must be COMPLETE/PARTIAL/NO_QUALIFIED_RESULT",
                status=422,
            )
        if not self.title.strip():
            raise AppError(code="DOCUMENT_TITLE_REQUIRED", message="title is required", status=422)
        for direction in self.directions:
            if direction.status not in {"covered", "missing"}:
                raise AppError(
                    code="DIRECTION_STATUS_INVALID",
                    message="direction status must be covered or missing",
                    status=422,
                )
            if direction.status == "missing" and not direction.gaps:
                raise AppError(
                    code="GAP_MUST_BE_EXPLICIT",
                    message=f"direction {direction.id!r} is missing without a stated gap",
                    status=422,
                )
            for finding in direction.findings:
                if (
                    str(finding.get("classification", "FACT")).upper() == "FACT"
                    and not finding.get("source_ids")
                    and str(finding.get("validity", "")).upper() != "UNVERIFIED"
                ):
                    raise AppError(
                        code="FACT_REQUIRES_SOURCE",
                        message="an important fact needs a source or an explicit unverified mark",
                        status=422,
                    )
        required = {str(item).strip() for item in self.constraints if str(item).strip()}
        for candidate in self.qualified:
            constraints = [
                item for item in candidate.get("constraints") or [] if isinstance(item, dict)
            ]
            names = {
                str(
                    item.get("constraint") or item.get("name") or item.get("id") or ""
                ).strip()
                for item in constraints
            }
            if any(str(item.get("result")) != "SATISFIED" for item in constraints) or (
                required and not required.issubset(names)
            ):
                raise AppError(
                    code="QUALIFIED_CANDIDATE_INVALID",
                    message=(
                        "a qualified recommendation requires every confirmed hard "
                        "constraint to be present and SATISFIED"
                    ),
                    status=422,
                )
        if self.completeness == "COMPLETE" and (
            self.gaps or any(direction.status == "missing" for direction in self.directions)
        ):
            raise AppError(
                code="COMPLETENESS_MISMATCH",
                message="a COMPLETE document cannot contain open gaps",
                status=422,
            )

    @property
    def manifest_hash(self) -> str:
        return stable_hash(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "title": self.title,
            "structure": self.structure,
            "completeness": self.completeness,
            "objective": self.objective,
            "constraints": list(self.constraints),
            "directions": [
                {
                    "id": direction.id,
                    "title": direction.title,
                    "status": direction.status,
                    "findings": list(direction.findings),
                    "gaps": list(direction.gaps),
                    "source_ids": list(direction.source_ids),
                }
                for direction in self.directions
            ],
            "qualified": list(self.qualified),
            "unknown_candidates": list(self.unknown_candidates),
            "rejected_candidates": list(self.rejected_candidates),
            "sources": list(self.sources),
            "gaps": list(self.gaps),
            "unverified": list(self.unverified),
            "notes": list(self.notes),
        }


def build_content(
    *,
    definition_title: str,
    structure: str,
    completeness: str,
    inputs: dict[str, Any],
    evidence: dict[str, Any],
    sources: list[dict[str, Any]],
    direction_titles: dict[str, str] | None = None,
) -> DocumentContent:
    titles = direction_titles or {}
    directions = [
        DirectionResult(
            id=str(item.get("id")),
            title=titles.get(str(item.get("id")), str(item.get("id"))),
            status=str(item.get("status", "missing")),
            findings=[
                finding for finding in item.get("findings") or [] if isinstance(finding, dict)
            ],
            gaps=[str(gap) for gap in item.get("gaps") or []],
            source_ids=[str(source) for source in item.get("source_ids") or []],
        )
        for item in evidence.get("directions") or []
    ]
    constraints: list[str] = []
    raw_constraints = inputs.get("hard_constraints")
    if isinstance(raw_constraints, list):
        constraints = [str(item) for item in raw_constraints]
    content = DocumentContent(
        title=definition_title,
        structure=structure,
        completeness=completeness,
        objective=str(inputs.get("topic") or ""),
        constraints=constraints,
        directions=directions,
        qualified=[item for item in evidence.get("qualified") or [] if isinstance(item, dict)],
        unknown_candidates=[
            item for item in evidence.get("unknown_candidates") or [] if isinstance(item, dict)
        ],
        rejected_candidates=[
            item for item in evidence.get("rejected_candidates") or [] if isinstance(item, dict)
        ],
        sources=[item for item in sources if isinstance(item, dict)],
        gaps=[str(gap) for gap in evidence.get("gaps") or []],
        unverified=[item for item in evidence.get("unverified") or [] if isinstance(item, dict)],
    )
    content.validate()
    return content


def decide_completeness(
    *,
    required_directions: list[str],
    evidence: dict[str, Any],
    allow_partial: bool,
) -> str:
    covered = {
        direction.get("id")
        for direction in evidence.get("directions") or []
        if direction.get("status") == "covered"
    }
    missing = [item for item in required_directions if item not in covered]
    gaps = list(evidence.get("gaps") or [])
    if missing or gaps:
        return "PARTIAL" if allow_partial else "NO_QUALIFIED_RESULT"
    if evidence.get("rejected_candidates") and not evidence.get("qualified"):
        return "NO_QUALIFIED_RESULT"
    return "COMPLETE"

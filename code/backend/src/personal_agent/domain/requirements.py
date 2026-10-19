"""Requirement draft rules: sources, completeness, stale confirmations.

Field values always record where they came from (user / default / suggestion) and
which message or suggestion produced them. Accepting a suggestion is not an
execution confirmation, and any edit invalidates an existing confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from personal_agent.domain.common import AppError


class DraftStatus(StrEnum):
    COLLECTING = "COLLECTING"
    READY = "READY"
    CONFIRMED = "CONFIRMED"
    STALE = "STALE"


class FieldSource(StrEnum):
    USER = "user"
    DEFAULT = "default"
    SUGGESTION = "suggestion"


class SuggestionStatus(StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class ConfirmationAction(StrEnum):
    START = "START"
    BRANCH = "BRANCH"


EDITABLE_STATUSES = frozenset({DraftStatus.COLLECTING, DraftStatus.READY, DraftStatus.STALE})
GRANTABLE_STATUSES = frozenset({DraftStatus.COLLECTING, DraftStatus.READY})


@dataclass(frozen=True, slots=True)
class FieldValue:
    path: str
    value: Any
    source: FieldSource
    message_id: str | None = None
    suggestion_id: str | None = None
    accepted_at: str | None = None
    explicit_none: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "value": self.value,
            "source": str(self.source),
            "message_id": self.message_id,
            "suggestion_id": self.suggestion_id,
            "accepted_at": self.accepted_at,
            "explicit_none": self.explicit_none,
        }


@dataclass(frozen=True, slots=True)
class SummarySection:
    heading: str
    entries: list[str] = field(default_factory=list)
    subsections: list[SummarySection] = field(default_factory=list)


def assert_editable(status: DraftStatus) -> None:
    if status not in EDITABLE_STATUSES:
        raise AppError(
            code="DRAFT_STATE_INVALID",
            message="this draft no longer accepts edits",
            status=409,
            details={"status": str(status)},
        )


def assert_grantable(status: DraftStatus) -> None:
    if status not in GRANTABLE_STATUSES:
        raise AppError(
            code="DRAFT_STATE_INVALID",
            message="only COLLECTING or READY drafts may receive a top-up",
            status=409,
            details={"status": str(status)},
        )


def missing_fields(required: list[str], fields: dict[str, FieldValue]) -> list[str]:
    """A field is missing when absent, or an explicit ``null`` without ``explicit_none``."""
    missing: list[str] = []
    for path in required:
        value = fields.get(path)
        if value is None:
            missing.append(path)
        elif value.value is None and not value.explicit_none and value.source is not FieldSource.USER:
            missing.append(path)
    return missing


def derive_status(
    *,
    required: list[str],
    fields: dict[str, FieldValue],
    conflicts: list[dict[str, Any]],
    confirmed: bool,
) -> DraftStatus:
    if confirmed:
        return DraftStatus.CONFIRMED
    if conflicts:
        return DraftStatus.COLLECTING
    return DraftStatus.READY if not missing_fields(required, fields) else DraftStatus.COLLECTING


def apply_analysis(
    *,
    fields: dict[str, FieldValue],
    analysis_fields: list[dict[str, Any]],
    message_id: str,
    required: list[str],
) -> tuple[dict[str, FieldValue], list[dict[str, Any]]]:
    """Merge model-extracted fields without promoting inference to confirmation.

    Only fields explicitly sourced as ``user`` in the analysis are marked user;
    anything else becomes a pending suggestion and does not enter ``fields``.
    """
    merged = dict(fields)
    suggestions: list[dict[str, Any]] = []
    for item in analysis_fields:
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        source = str(item.get("source", "suggestion"))
        if source == "user":
            merged[path] = FieldValue(
                path=path,
                value=item.get("value"),
                source=FieldSource.USER,
                message_id=message_id,
                explicit_none=bool(item.get("explicit_none", False)),
            )
        else:
            suggestions.append(
                {
                    "field_path": path,
                    "value": item.get("value"),
                    "rationale": str(item.get("rationale", "")),
                }
            )
    return merged, suggestions


def required_paths(input_schema: dict[str, Any]) -> list[str]:
    required = input_schema.get("required") or []
    return [str(item) for item in required]


def invalidate_confirmation(status: DraftStatus) -> DraftStatus:
    """Editing a confirmed draft makes the stored confirmation unusable."""
    if status is DraftStatus.CONFIRMED:
        return DraftStatus.STALE
    return status


def assert_confirmable(
    *,
    status: DraftStatus,
    missing: list[str],
    conflicts: list[dict[str, Any]],
) -> None:
    if missing:
        raise AppError(
            code="INPUT_INCOMPLETE",
            message="required input is still missing",
            status=409,
            details={"missing_fields": missing},
        )
    if conflicts:
        raise AppError(
            code="INPUT_INCONSISTENT",
            message="conflicting values must be resolved before confirmation",
            status=409,
            details={"conflicts": conflicts},
        )
    if status not in {DraftStatus.READY, DraftStatus.COLLECTING}:
        raise AppError(
            code="DRAFT_STATE_INVALID",
            message="the draft is not in a confirmable state",
            status=409,
            details={"status": str(status)},
        )


def assert_suggestion_accepted_before_confirm(
    pending_suggestions: int, accepted_values_required: bool
) -> None:
    if accepted_values_required and pending_suggestions > 0:
        raise AppError(
            code="UNACCEPTED_SUGGESTION",
            message="pending suggestions must be accepted or rejected before confirmation",
            status=409,
            details={"pending": pending_suggestions},
        )

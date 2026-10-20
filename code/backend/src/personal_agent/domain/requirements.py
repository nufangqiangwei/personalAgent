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
    """A field is missing when absent, empty, or null without ``explicit_none``.

    A user-supplied null is not completeness: only an explicit "none" decision
    (``explicit_none``) can satisfy an optional requirement.
    """
    missing: list[str] = []
    for path in required:
        value = fields.get(path)
        if value is None:
            missing.append(path)
        elif value.value is None and not value.explicit_none:
            missing.append(path)
        elif isinstance(value.value, str) and not value.value.strip():
            missing.append(path)
    return missing


def _field_document(fields: dict[str, FieldValue]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for path, value in fields.items():
        tokens = [token for token in str(path).split("/") if token]
        if not tokens:
            continue
        current = document
        for token in tokens[:-1]:
            child = current.get(token)
            if not isinstance(child, dict):
                child = {}
                current[token] = child
            current = child
        current[tokens[-1]] = value.value
    return document


def validate_field_values(
    input_schema: dict[str, Any], fields: dict[str, FieldValue],
) -> list[str]:
    """Return paths whose value is null, empty, the wrong type, or unknown.

    Nested ``required`` and ``additionalProperties: false`` are honoured through
    the actual workflow input schema; nothing is promoted from a suggestion here.
    """
    if not isinstance(input_schema, dict) or not input_schema:
        return []
    from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
    from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]

    properties = input_schema.get("properties") or {}
    invalid: list[str] = []
    for path, value in fields.items():
        tokens = [token for token in str(path).split("/") if token]
        if properties and (not tokens or tokens[0] not in properties):
            invalid.append(str(path))
            continue
        if isinstance(value.value, str) and not value.value.strip():
            invalid.append(str(path))
    document = _field_document(fields)
    try:
        validator = Draft202012Validator(input_schema)
    except SchemaError:
        return sorted(set(invalid))
    for error in validator.iter_errors(document):
        parent = "/".join(str(part) for part in error.absolute_path)
        if error.validator == "required":
            instance = error.instance if isinstance(error.instance, dict) else {}
            for name in error.validator_value or []:
                if name not in instance:
                    invalid.append(f"{parent}/{name}" if parent else str(name))
            continue
        if not parent:
            invalid.append(str(error.message))
        else:
            invalid.append(parent)
    return sorted(set(invalid))


def nested_required_paths(input_schema: dict[str, Any]) -> list[str]:
    paths: list[str] = []

    def walk(schema: dict[str, Any], prefix: str) -> None:
        for name in schema.get("required") or []:
            paths.append(f"{prefix}{name}")
        for name, child in (schema.get("properties") or {}).items():
            if isinstance(child, dict):
                walk(child, f"{prefix}{name}/")

    if isinstance(input_schema, dict):
        walk(input_schema, "")
    return paths


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
    """All required paths, including nested ``required`` arrays."""
    return nested_required_paths(input_schema)


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
    if status not in {DraftStatus.READY, DraftStatus.COLLECTING, DraftStatus.STALE}:
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

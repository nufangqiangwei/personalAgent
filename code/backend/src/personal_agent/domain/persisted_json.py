"""Versioned validation for persisted business JSON.

The database stores JSON columns; shape drift or a future ``schema_version`` must
be rejected at the boundary instead of failing later in the engine. Unknown keys
are rejected so a renamed field cannot silently drop evidence.
"""

from __future__ import annotations

from typing import Any

from personal_agent.domain.common import AppError, ErrorCategory

INPUT_SNAPSHOT_VERSION = 1

_FIELD_ENTRY = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "value": {},
        "source": {"type": "string", "enum": ["user", "default", "suggestion"]},
        "message_id": {"type": ["string", "null"]},
        "suggestion_id": {"type": ["string", "null"]},
        "accepted_at": {"type": ["string", "null"]},
        "explicit_none": {"type": "boolean"},
    },
    "required": ["path", "value", "source"],
    "additionalProperties": False,
}

_INPUT_SNAPSHOT = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": INPUT_SNAPSHOT_VERSION},
        "fields": {
            "type": "object",
            "additionalProperties": _FIELD_ENTRY,
        },
        "preference_order": {"type": "array", "items": {"type": "string"}},
        "output_spec": {"type": "object"},
    },
    "required": ["fields", "preference_order", "output_spec"],
    "additionalProperties": False,
}


def _invalid(message: str, *, details: dict[str, Any] | None = None) -> AppError:
    return AppError(
        code="PERSISTED_JSON_INVALID",
        message=message,
        category=ErrorCategory.INTEGRITY,
        status=500,
        details=details or {},
    )


def validate_input_snapshot(value: Any) -> dict[str, Any]:
    """Validate a confirmed input snapshot (legacy snapshots omit the version)."""
    if not isinstance(value, dict):
        raise _invalid("a persisted input snapshot must be an object")
    version = value.get("schema_version", INPUT_SNAPSHOT_VERSION)
    if version != INPUT_SNAPSHOT_VERSION:
        raise AppError(
            code="PERSISTED_JSON_VERSION_UNSUPPORTED",
            message="the persisted input snapshot uses an unsupported schema version",
            category=ErrorCategory.INTEGRITY,
            status=500,
            details={"schema_version": version},
        )
    from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

    errors = sorted(
        Draft202012Validator(_INPUT_SNAPSHOT).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise _invalid(
            "the persisted input snapshot does not match its schema version",
            details={
                "errors": [
                    {
                        "path": "/".join(str(part) for part in error.absolute_path),
                        "message": error.message,
                    }
                    for error in errors[:5]
                ]
            },
        )
    return dict(value)

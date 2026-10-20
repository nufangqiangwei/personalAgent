"""Requirement field validation: null, empty, type, nested required, unknown (T107)."""

from __future__ import annotations

from typing import Any

from personal_agent.domain.requirements import (
    FieldSource,
    FieldValue,
    missing_fields,
    nested_required_paths,
    validate_field_values,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "minLength": 1},
        "constraints": {
            "type": "object",
            "properties": {"budget": {"type": "integer", "minimum": 1}},
            "required": ["budget"],
            "additionalProperties": False,
        },
    },
    "required": ["topic", "constraints"],
    "additionalProperties": False,
}


def field(path: str, value: Any, *, source: FieldSource = FieldSource.USER,
          explicit_none: bool = False) -> FieldValue:
    return FieldValue(
        path=path, value=value, source=source, explicit_none=explicit_none,
    )


def test_a_user_null_is_not_completeness() -> None:
    fields = {"topic": field("topic", None)}
    assert missing_fields(["topic"], fields) == ["topic"]


def test_an_explicit_none_is_a_real_decision() -> None:
    fields = {"topic": field("topic", None, explicit_none=True)}
    assert missing_fields(["topic"], fields) == []


def test_blank_strings_are_missing() -> None:
    fields = {"topic": field("topic", "   ")}
    assert missing_fields(["topic"], fields) == ["topic"]


def test_nested_required_paths_are_collected() -> None:
    assert nested_required_paths(SCHEMA) == ["topic", "constraints", "constraints/budget"]


def test_null_empty_type_unknown_and_nested_required_are_invalid() -> None:
    invalid = validate_field_values(SCHEMA, {
        "topic": field("topic", "AI"),
        "constraints": field("constraints", {"budget": 0}),
        "unknown": field("unknown", "x"),
    })
    assert "constraints/budget" in invalid
    assert "unknown" in invalid

    nested_missing = validate_field_values(SCHEMA, {
        "topic": field("topic", "AI"),
        "constraints": field("constraints", {}),
    })
    assert "constraints/budget" in nested_missing

    wrong_type = validate_field_values(SCHEMA, {
        "topic": field("topic", "AI"),
        "constraints": field("constraints", {"budget": "many"}),
    })
    assert "constraints/budget" in wrong_type

    blank = validate_field_values(SCHEMA, {"topic": field("topic", " ")})
    assert "topic" in blank


def test_a_valid_document_has_no_invalid_paths() -> None:
    assert validate_field_values(SCHEMA, {
        "topic": field("topic", "AI"),
        "constraints": field("constraints", {"budget": 40}),
    }) == []

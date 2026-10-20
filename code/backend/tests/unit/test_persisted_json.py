"""Versioned persisted JSON validation (T112)."""

from __future__ import annotations

import pytest

from personal_agent.domain.common import AppError
from personal_agent.domain.persisted_json import validate_input_snapshot


def _snapshot(**extra: object) -> dict[str, object]:
    return {"fields": {}, "preference_order": [], "output_spec": {}, **extra}


def test_a_legacy_snapshot_without_a_version_is_accepted() -> None:
    assert validate_input_snapshot(_snapshot()) == _snapshot()


def test_the_current_version_is_accepted() -> None:
    assert validate_input_snapshot(_snapshot(schema_version=1))["schema_version"] == 1


def test_an_unknown_schema_version_is_rejected() -> None:
    with pytest.raises(AppError) as error:
        validate_input_snapshot(_snapshot(schema_version=2))
    assert error.value.code == "PERSISTED_JSON_VERSION_UNSUPPORTED"


def test_a_corrupted_shape_is_rejected() -> None:
    with pytest.raises(AppError) as error:
        validate_input_snapshot({"fields": "not-an-object"})
    assert error.value.code == "PERSISTED_JSON_INVALID"


def test_an_unknown_field_source_is_rejected() -> None:
    corrupted = _snapshot(fields={
        "topic": {"path": "topic", "value": "AI", "source": "model"},
    })
    with pytest.raises(AppError) as error:
        validate_input_snapshot(corrupted)
    assert error.value.code == "PERSISTED_JSON_INVALID"

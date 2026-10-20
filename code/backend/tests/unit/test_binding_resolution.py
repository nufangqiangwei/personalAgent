"""Binding resolution at execution time (T107)."""

from __future__ import annotations

import pytest

from personal_agent.application.engine import WorkflowEngine
from personal_agent.domain.common import AppError
from personal_agent.domain.workflow_schema import Binding


def engine() -> WorkflowEngine:
    # The helpers under test read only their arguments.
    return WorkflowEngine.__new__(WorkflowEngine)


def test_pointer_resolves_inputs_and_step_outputs() -> None:
    resolve = WorkflowEngine._resolve_pointer
    assert resolve("/topic", {"topic": "AI"}, {}) == "AI"
    assert resolve("/collect/items/0", {}, {"collect": {"items": ["first"]}}) == "first"


def test_unresolved_pointer_is_rejected() -> None:
    with pytest.raises(AppError) as error:
        WorkflowEngine._resolve_pointer("/missing", {"topic": "AI"}, {})
    assert error.value.code == "BINDING_UNRESOLVED"


def test_named_binding_must_resolve_to_a_value() -> None:
    step = type("Step", (), {
        "id": "write",
        "bindings": {"topic": Binding(source="input", pointer="/topic")},
    })()
    resolved = WorkflowEngine._resolved_bindings(engine(), step, {"topic": "AI"}, {})
    assert resolved == {"topic": "AI"}
    assert WorkflowEngine._binding_value(
        engine(), step, "topic", {"topic": "AI"}, {}, resolved,
    ) == "AI"
    # A plain string is a literal argument, not a dangling binding.
    assert WorkflowEngine._binding_value(
        engine(), step, "https://example.org", {}, {}, resolved,
    ) == "https://example.org"


def test_a_binding_to_a_missing_step_output_fails_closed() -> None:
    step = type("Step", (), {
        "id": "write",
        "bindings": {"data": Binding(source="step", step_id="collect", pointer="/items")},
    })()
    with pytest.raises(AppError) as error:
        WorkflowEngine._resolved_bindings(engine(), step, {}, {})
    assert error.value.code == "BINDING_UNRESOLVED"


def test_step_input_schema_rejects_unknown_and_wrong_types() -> None:
    step = type("Step", (), {
        "id": "write",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    })()
    with pytest.raises(AppError) as unknown:
        WorkflowEngine._validate_step_input(step, {"query": "x", "extra": 1})
    assert unknown.value.code == "STEP_INPUT_INVALID"
    with pytest.raises(AppError) as wrong:
        WorkflowEngine._validate_step_input(step, {"query": 1})
    assert wrong.value.code == "STEP_INPUT_INVALID"
    WorkflowEngine._validate_step_input(step, {"query": "x"})

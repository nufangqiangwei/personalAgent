"""Engine unit rules (T051).

Covers the bounded expression AST, tri-state hard constraints, priority ordering,
direction normalization and safe Markdown rendering. End-to-end six-node
scheduling, retries, exhausted paths and replay are exercised by
``tests/integration/test_fixed_research.py``.
"""

from __future__ import annotations

import pytest

from personal_agent.adapters.rendering.markdown import render_research_document
from personal_agent.application.research_evidence import (
    evaluate_candidates,
    normalize_directions,
    order_by_priority,
)
from personal_agent.domain.common import AppError
from personal_agent.domain.expressions import MISSING, evaluate, resolve_binding
from personal_agent.domain.workflow_schema import Binding, Expression


def expression(raw: dict) -> Expression:
    return Expression.model_validate(raw)


def test_literal_comparisons_and_boolean_operators() -> None:
    truthy = expression(
        {
            "op": "and",
            "args": [
                {"op": "eq", "left": {"literal": 1}, "right": {"literal": 1}},
                {
                    "op": "not",
                    "arg": {
                        "op": "eq",
                        "left": {"literal": "a"},
                        "right": {"literal": "b"},
                    },
                },
            ],
        }
    )
    assert evaluate(truthy, lambda binding: MISSING) is True

    contains = expression(
        {"op": "contains", "left": {"literal": "hello world"}, "right": {"literal": "world"}}
    )
    assert evaluate(contains, lambda binding: MISSING) is True


def test_missing_operand_is_not_treated_as_true() -> None:
    missing = expression(
        {
            "op": "eq",
            "left": {"ref": {"source": "step", "step_id": "collect", "pointer": "/absent"}},
            "right": {"literal": True},
        }
    )
    with pytest.raises(AppError) as error:
        evaluate(missing, lambda binding: MISSING)
    assert error.value.code == "CONDITION_INVALID"


def test_incompatible_operand_types_are_rejected() -> None:
    mismatch = expression(
        {"op": "eq", "left": {"literal": 1}, "right": {"literal": "1"}}
    )
    with pytest.raises(AppError) as error:
        evaluate(mismatch, lambda binding: MISSING)
    assert error.value.code == "CONDITION_INVALID"


def test_bindings_resolve_json_pointers_and_report_missing() -> None:
    binding = Binding.model_validate(
        {"source": "step", "step_id": "merge", "pointer": "/has_gaps"}
    )
    assert resolve_binding(
        binding, input_values={}, step_outputs={"merge": {"has_gaps": False}}
    ) is False
    missing = Binding.model_validate(
        {"source": "input", "pointer": "/closed_ports"}
    )
    assert resolve_binding(missing, input_values={"closed_ports": [80]}, step_outputs={}) == [80]
    assert resolve_binding(missing, input_values={}, step_outputs={}) is MISSING


def test_candidate_constraints_are_tri_state() -> None:
    result = evaluate_candidates(
        [
            {
                "id": "a",
                "name": "A",
                "constraints": [{"constraint": "部署", "result": "SATISFIED"}],
            },
            {
                "id": "b",
                "name": "B",
                "constraints": [{"constraint": "部署", "result": "UNKNOWN"}],
            },
            {
                "id": "c",
                "name": "C",
                "constraints": [{"constraint": "部署", "result": "VIOLATED"}],
            },
            {"id": "d", "name": "D", "constraints": []},
        ]
    )
    assert [item["id"] for item in result["qualified"]] == ["a"]
    assert {item["id"] for item in result["unknown"]} == {"b", "d"}
    assert [item["id"] for item in result["rejected"]] == ["c"]


def test_priority_ordering_uses_only_the_confirmed_list() -> None:
    candidates = [
        {"id": "a", "attributes": {"cost": 1}},
        {"id": "b", "attributes": {"accuracy": 9}},
    ]
    ordered = order_by_priority(candidates, ["accuracy", "cost"])
    assert [item["id"] for item in ordered] == ["b", "a"]
    assert order_by_priority(candidates, []) == candidates


def test_every_declared_direction_appears_with_a_gap_when_missing() -> None:
    normalized = normalize_directions(
        ["official", "ops"],
        {
            "directions": [
                {
                    "id": "official",
                    "findings": [{"claim": "x", "source_ids": ["s1"]}],
                    "source_ids": ["s1"],
                }
            ]
        },
    )
    assert [item["id"] for item in normalized] == ["official", "ops"]
    assert normalized[0]["status"] == "covered"
    assert normalized[1]["status"] == "missing"
    assert normalized[1]["gaps"]


def test_markdown_never_emits_raw_html_or_unsafe_links() -> None:
    markdown, document = render_research_document(
        title="<script>alert(1)</script>",
        structure="report",
        inputs={"topic": "<img src=x onerror=alert(1)>"},
        evidence={
            "directions": [
                {
                    "id": "background",
                    "status": "covered",
                    "findings": [
                        {"claim": "<b>raw</b>", "source_ids": ["s1"], "validity": "VERIFIED"}
                    ],
                    "gaps": [],
                }
            ],
            "gaps": [],
            "unverified": [],
        },
        sources=[{"url": "javascript:alert(1)", "title": "<i>x</i>", "fetched_at": "t"}],
        completeness="PARTIAL",
        direction_titles={"background": "背景"},
    )
    assert "<script>" not in markdown
    assert "<img" not in markdown
    assert "<b>" not in markdown
    assert "javascript:" not in markdown
    assert "&lt;b&gt;raw&lt;/b&gt;" in markdown
    assert "### 背景" in markdown
    assert document["completeness"] == "PARTIAL"

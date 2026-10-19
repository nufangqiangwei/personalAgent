"""Workflow definition validation (T029).

Every rule here protects determinism: no cycles, no unreachable or dead-end steps,
declared dependencies, bounded expressions, no remote schema references and no
silent permission relaxation.
"""

from __future__ import annotations

import copy
from typing import Any

from personal_agent.domain.workflow_validation import validate_definition


def codes(report: Any) -> set[str]:
    return {issue.code for issue in report.issues}


def test_the_fixture_definition_is_valid(workflow_definition: dict[str, Any]) -> None:
    report = validate_definition(workflow_definition)
    assert report.valid, report.as_dict()
    assert report.definition_hash.startswith("sha256:")


def test_hash_ignores_the_server_assigned_version(workflow_definition: dict[str, Any]) -> None:
    first = validate_definition(workflow_definition)
    bumped = copy.deepcopy(workflow_definition)
    bumped["version"] = 7
    assert validate_definition(bumped).definition_hash == first.definition_hash


def test_empty_or_overlong_name_is_rejected(workflow_definition: dict[str, Any]) -> None:
    empty = copy.deepcopy(workflow_definition)
    empty["name"] = ""
    assert not validate_definition(empty).valid
    overlong = copy.deepcopy(workflow_definition)
    overlong["name"] = "x" * 201
    assert not validate_definition(overlong).valid


def test_duplicate_step_ids_are_rejected(workflow_definition: dict[str, Any]) -> None:
    broken = copy.deepcopy(workflow_definition)
    broken["steps"][1]["id"] = "collect"
    report = validate_definition(broken)
    assert "DUPLICATE_STEP_ID" in codes(report)


def test_unknown_entry_step_is_rejected(workflow_definition: dict[str, Any]) -> None:
    broken = copy.deepcopy(workflow_definition)
    broken["entry_step"] = "missing"
    assert "ENTRY_STEP_MISSING" in codes(validate_definition(broken))


def test_unreachable_step_is_rejected(workflow_definition: dict[str, Any]) -> None:
    broken = copy.deepcopy(workflow_definition)
    broken["steps"].append(
        {
            "id": "orphan",
            "type": "Transform",
            "name": "孤立步骤",
            "input_schema": {},
            "output_schema": {},
            "bindings": {},
            "input_reads": [],
            "depends_on": [],
            "policy": {},
            "config": {"transform_id": "dedupe", "parameters": {}},
        }
    )
    assert "UNREACHABLE_STEP" in codes(validate_definition(broken))


def test_cycles_are_rejected(workflow_definition: dict[str, Any]) -> None:
    broken = copy.deepcopy(workflow_definition)
    broken["transition_rules"].append({"from": "review", "to": "collect", "on": "success"})
    broken["transition_rules"] = [
        rule
        for rule in broken["transition_rules"]
        if not (rule["from"] == "review" and rule["to"] == "write" and rule["on"] == "success")
    ]
    assert "CYCLE_DETECTED" in codes(validate_definition(broken))


def test_unknown_transition_target_is_rejected(workflow_definition: dict[str, Any]) -> None:
    broken = copy.deepcopy(workflow_definition)
    broken["transition_rules"][0]["to"] = "nowhere"
    assert "TRANSITION_TARGET_UNKNOWN" in codes(validate_definition(broken))


def test_declared_dependency_must_be_an_ancestor(workflow_definition: dict[str, Any]) -> None:
    broken = copy.deepcopy(workflow_definition)
    broken["steps"][1]["depends_on"] = ["write"]
    assert "DEPENDENCY_NOT_ANCESTOR" in codes(validate_definition(broken))


def test_condition_needs_both_branches(workflow_definition: dict[str, Any]) -> None:
    broken = copy.deepcopy(workflow_definition)
    broken["transition_rules"] = [
        rule
        for rule in broken["transition_rules"]
        if not (rule["from"] == "check" and rule["on"] == "false")
    ]
    assert "CONDITION_BRANCHES_INVALID" in codes(validate_definition(broken))


def test_output_must_be_terminal(workflow_definition: dict[str, Any]) -> None:
    broken = copy.deepcopy(workflow_definition)
    broken["transition_rules"].append({"from": "write", "to": "check", "on": "success"})
    assert "OUTPUT_HAS_EDGES" in codes(validate_definition(broken))


def test_unknown_tool_and_relaxed_policy_are_rejected(workflow_definition: dict[str, Any]) -> None:
    broken = copy.deepcopy(workflow_definition)
    broken["steps"][0]["policy"]["allowed_tools"] = ["shell.exec"]
    broken["execution_policy"]["max_model_requests"] = 100
    report = validate_definition(broken)
    assert {"TOOL_NOT_REGISTERED", "POLICY_RELAXED"} <= codes(report)


def test_domains_must_be_bare_hostnames(workflow_definition: dict[str, Any]) -> None:
    broken = copy.deepcopy(workflow_definition)
    broken["steps"][0]["policy"]["allowed_domains"] = ["https://example.org/path"]
    assert "DOMAIN_INVALID" in codes(validate_definition(broken))


def test_remote_schema_references_are_rejected(workflow_definition: dict[str, Any]) -> None:
    broken = copy.deepcopy(workflow_definition)
    broken["output_schema"] = {"$ref": "https://schemas.example.org/report.json"}
    assert "REMOTE_REF_FORBIDDEN" in codes(validate_definition(broken))


def test_deeply_nested_schema_is_rejected(workflow_definition: dict[str, Any]) -> None:
    nested: dict[str, Any] = {"type": "string"}
    for _ in range(30):
        nested = {"type": "object", "properties": {"child": nested}}
    broken = copy.deepcopy(workflow_definition)
    broken["output_schema"] = nested
    assert "SCHEMA_TOO_DEEP" in codes(validate_definition(broken))


def test_expression_depth_is_bounded(workflow_definition: dict[str, Any]) -> None:
    expression: dict[str, Any] = {"op": "eq", "left": {"literal": 1}, "right": {"literal": 1}}
    for _ in range(12):
        expression = {"op": "not", "arg": expression}
    broken = copy.deepcopy(workflow_definition)
    broken["steps"][1]["config"] = {"expression": expression}
    assert "EXPRESSION_TOO_DEEP" in codes(validate_definition(broken))


def test_condition_reading_an_unexecuted_step_is_rejected(
    workflow_definition: dict[str, Any],
) -> None:
    broken = copy.deepcopy(workflow_definition)
    broken["steps"][1]["config"] = {
        "expression": {
            "op": "eq",
            "left": {"ref": {"source": "step", "step_id": "write", "pointer": "/ok"}},
            "right": {"literal": True},
        }
    }
    assert "CONDITION_READS_UNEXECUTED" in codes(validate_definition(broken))


def test_completion_policy_must_reference_real_outputs(
    workflow_definition: dict[str, Any],
) -> None:
    broken = copy.deepcopy(workflow_definition)
    broken["completion_policy"]["required_output_step_ids"] = ["missing"]
    broken["completion_policy"]["required_direction_ids"] = ["unknown"]
    report = validate_definition(broken)
    assert {"COMPLETION_STEP_UNKNOWN", "COMPLETION_DIRECTION_UNKNOWN"} <= codes(report)


def test_unused_direction_is_rejected(workflow_definition: dict[str, Any]) -> None:
    broken = copy.deepcopy(workflow_definition)
    broken["directions"].append(
        {"id": "extra", "title": "额外方向", "scope": "没有步骤覆盖"}
    )
    assert "DIRECTION_UNUSED" in codes(validate_definition(broken))


def test_unknown_fields_and_step_config_mismatch_are_rejected(
    workflow_definition: dict[str, Any],
) -> None:
    broken = copy.deepcopy(workflow_definition)
    broken["unexpected"] = True
    broken["steps"][0]["config"] = {"expression": {"op": "not"}}
    report = validate_definition(broken)
    assert "DEFINITION_INVALID" in codes(report)


def test_more_than_one_hundred_steps_is_rejected(workflow_definition: dict[str, Any]) -> None:
    broken = copy.deepcopy(workflow_definition)
    template = broken["steps"][2]
    for index in range(100):
        step = copy.deepcopy(template)
        step["id"] = f"extra_{index}"
        broken["steps"].append(step)
    assert not validate_definition(broken).valid


def test_definition_larger_than_one_mib_is_rejected(workflow_definition: dict[str, Any]) -> None:
    broken = copy.deepcopy(workflow_definition)
    broken["description"] = "x" * (1024 * 1024 + 1)
    assert "DEFINITION_TOO_LARGE" in codes(validate_definition(broken))

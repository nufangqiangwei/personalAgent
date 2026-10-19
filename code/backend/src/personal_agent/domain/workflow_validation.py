"""Semantic validation for workflow definitions.

A JSON shape check is not enough to publish: the definition must be acyclic and
reachable from its entry, every normal path must end in an Output or an explicit
``exhausted`` failure path, dependencies and tool scopes must be declared, the
completion policy must reference real steps, and expressions stay inside the
bounded AST.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from personal_agent.domain.common import canonical_json, stable_hash
from personal_agent.domain.workflow_schema import (
    MAX_EXPRESSION_DEPTH,
    MAX_EXPRESSION_NODES,
    MAX_SCHEMA_DEPTH,
    MAX_STEPS,
    AgentTaskConfig,
    ConditionConfig,
    Expression,
    HumanReviewConfig,
    OutputConfig,
    Step,
    ToolTaskConfig,
    TransformConfig,
    WorkflowDefinition,
    expression_stats,
    schema_depth,
)

ALLOWED_TOOLS = frozenset({"search.web", "http.fetch", "browser.read", "content.extract"})
ALLOWED_TRANSFORMS = frozenset({"dedupe", "sort", "merge"})
MAX_DEFINITION_BYTES = 1024 * 1024
GLOBAL_BUDGET_CEILINGS = {
    "max_active_seconds": 20 * 60,
    "max_model_requests": 30,
    "max_tool_calls": 60,
    "max_search_calls": 20,
    "max_page_reads": 40,
    "max_tokens": 100_000,
    "max_attempts_per_step": 3,
    "model_timeout_seconds": 120,
    "tool_timeout_seconds": 120,
}


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    step_id: str | None = None
    path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "step_id": self.step_id,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class ValidationReport:
    valid: bool
    issues: list[ValidationIssue] = field(default_factory=list)
    definition_hash: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "definition_hash": self.definition_hash,
            "issues": [issue.as_dict() for issue in self.issues],
        }


def content_hash(raw: dict[str, Any]) -> str:
    """Hash the definition content, excluding the server-assigned version number."""
    content = {key: value for key, value in raw.items() if key != "version"}
    return stable_hash(content)


def validate_definition(raw: dict[str, Any]) -> ValidationReport:
    issues: list[ValidationIssue] = []
    encoded = canonical_json(raw).encode("utf-8")
    if len(encoded) > MAX_DEFINITION_BYTES:
        return ValidationReport(
            valid=False,
            issues=[
                ValidationIssue(
                    code="DEFINITION_TOO_LARGE",
                    message="the definition exceeds the 1 MiB limit",
                )
            ],
            definition_hash=content_hash(raw),
        )

    definition_hash = content_hash(raw)
    try:
        definition = WorkflowDefinition.model_validate(raw)
    except ValidationError as error:
        for item in error.errors():
            issues.append(
                ValidationIssue(
                    code="DEFINITION_INVALID",
                    message=str(item.get("msg", "invalid")),
                    path=".".join(str(part) for part in item.get("loc", ())),
                )
            )
        return ValidationReport(valid=False, issues=issues, definition_hash=definition_hash)

    issues.extend(_validate_structure(definition))
    issues.extend(_validate_schemas(definition))
    issues.extend(_validate_expressions(definition))
    issues.extend(_validate_policies(definition))
    issues.extend(_validate_completion(definition))
    return ValidationReport(valid=not issues, issues=issues, definition_hash=definition_hash)


def require_valid(raw: dict[str, Any]) -> WorkflowDefinition:
    report = validate_definition(raw)
    if not report.valid:
        from personal_agent.domain.common import AppError

        raise AppError(
            code="DEFINITION_INVALID",
            message="the workflow definition is not publishable",
            status=422,
            details={"issues": [issue.as_dict() for issue in report.issues]},
        )
    return WorkflowDefinition.model_validate(raw)


def _validate_structure(definition: WorkflowDefinition) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    steps: list[Step] = list(definition.steps)
    if len(steps) > MAX_STEPS:
        issues.append(_issue("TOO_MANY_STEPS", f"a workflow may define at most {MAX_STEPS} steps"))
    by_id: dict[str, Step] = {}
    for step in steps:
        if step.id in by_id:
            issues.append(_issue("DUPLICATE_STEP_ID", "step ids must be unique", step.id))
        by_id[step.id] = step

    if definition.entry_step not in by_id:
        issues.append(_issue("ENTRY_STEP_MISSING", "entry_step must reference a defined step"))
        return issues

    outgoing: dict[str, list[tuple[str, str]]] = {step.id: [] for step in steps}
    incoming: dict[str, list[str]] = {step.id: [] for step in steps}
    for transition in definition.transition_rules:
        source = transition.from_
        target = transition.to
        if source not in by_id:
            issues.append(
                _issue(
                    "TRANSITION_SOURCE_UNKNOWN",
                    "transition source is not a defined step",
                    source,
                )
            )
            continue
        if target not in by_id:
            issues.append(
                _issue(
                    "TRANSITION_TARGET_UNKNOWN",
                    "transition target is not a defined step",
                    source,
                )
            )
            continue
        outgoing[source].append((target, transition.on))
        incoming[target].append(source)

    for step in steps:
        edges = outgoing[step.id]
        success_edges = [edge for edge in edges if edge[1] == "success"]
        true_edges = [edge for edge in edges if edge[1] == "true"]
        false_edges = [edge for edge in edges if edge[1] == "false"]
        if step.type == "Condition":
            if len(true_edges) != 1 or len(false_edges) != 1:
                issues.append(
                    _issue(
                        "CONDITION_BRANCHES_INVALID",
                        "a Condition needs exactly one true and one false edge",
                        step.id,
                    )
                )
            if success_edges:
                issues.append(
                    _issue("CONDITION_BRANCHES_INVALID", "a Condition has no success edge", step.id)
                )
        elif step.type == "Output":
            if edges:
                issues.append(_issue("OUTPUT_HAS_EDGES", "an Output is terminal", step.id))
        else:
            if len(success_edges) != 1:
                issues.append(
                    _issue(
                        "SUCCESS_EDGE_INVALID",
                        "each non-terminal step needs exactly one success edge",
                        step.id,
                    )
                )
            if true_edges or false_edges:
                issues.append(
                    _issue(
                        "BRANCH_EDGE_INVALID",
                        "true/false edges belong to Condition only",
                        step.id,
                    )
                )

    # Cycle detection over all declared edges.
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, path: list[str]) -> bool:
        if node in visiting:
            cycle = " -> ".join([*path, node])
            issues.append(_issue("CYCLE_DETECTED", f"cycle: {cycle}", node))
            return False
        if node in visited:
            return True
        visiting.add(node)
        for target, _on in outgoing.get(node, []):
            if not visit(target, [*path, node]):
                return False
        visiting.discard(node)
        visited.add(node)
        return True

    for step in steps:
        if not visit(step.id, []):
            break

    reachable = _reachable(definition.entry_step, outgoing)
    for step in steps:
        if step.id not in reachable:
            issues.append(
                _issue(
                    "UNREACHABLE_STEP",
                    "step is not reachable from entry_step",
                    step.id,
                )
            )

    # Every normal path must reach an Output; exhausted edges may end at a
    # HumanReview or an Output but must not bypass a still-required step.
    outputs = {step.id for step in steps if step.type == "Output"}
    for step in steps:
        if step.type == "Output" or step.id not in reachable:
            continue
        normal = [target for target, on in outgoing[step.id] if on in {"success", "true", "false"}]
        if not normal:
            issues.append(
                _issue(
                    "DEAD_END_STEP",
                    "the step has no normal outgoing path to an Output",
                    step.id,
                )
            )
    if not _has_output_path(definition.entry_step, outgoing, outputs):
        issues.append(_issue("NO_OUTPUT_PATH", "no normal path from entry_step reaches an Output"))

    for step in steps:
        for dependency in step.depends_on:
            if dependency not in by_id:
                issues.append(
                    _issue(
                        "DEPENDENCY_UNKNOWN",
                        "depends_on is not a defined step",
                        step.id,
                    )
                )
            elif dependency not in _ancestors(step.id, incoming):
                issues.append(
                    _issue(
                        "DEPENDENCY_NOT_ANCESTOR",
                        "depends_on must be an ancestor on every normal path",
                        step.id,
                    )
                )
        if step.type == "Condition":
            config = step.config
            if isinstance(config, ConditionConfig):
                for reference in _expression_step_refs(config.expression):
                    if reference not in _ancestors(step.id, incoming):
                        issues.append(
                            _issue(
                                "CONDITION_READS_UNEXECUTED",
                                f"condition reads step {reference!r} that is not an ancestor",
                                step.id,
                            )
                        )
    return issues


def _validate_schemas(definition: WorkflowDefinition) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    documents = {
        "input_schema": definition.input_schema,
        "output_schema": definition.output_schema,
    }
    for step in definition.steps:
        documents[f"{step.id}.input_schema"] = step.input_schema
        documents[f"{step.id}.output_schema"] = step.output_schema

    for name, schema in documents.items():
        if not isinstance(schema, dict):
            issues.append(_issue("SCHEMA_INVALID", "a JSON Schema must be an object", path=name))
            continue
        if schema_depth(schema) > MAX_SCHEMA_DEPTH:
            issues.append(
                _issue(
                    "SCHEMA_TOO_DEEP",
                    f"schema nesting exceeds {MAX_SCHEMA_DEPTH}",
                    path=name,
                )
            )
        for reference in _collect_refs(schema):
            if not reference.startswith("#"):
                issues.append(
                    _issue(
                        "REMOTE_REF_FORBIDDEN",
                        "only local $ref values are allowed",
                        path=name,
                    )
                )
    return issues


def _validate_expressions(definition: WorkflowDefinition) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for step in definition.steps:
        if not isinstance(step.config, ConditionConfig):
            continue
        depth, nodes = expression_stats(step.config.expression)
        if depth > MAX_EXPRESSION_DEPTH:
            issues.append(
                _issue("EXPRESSION_TOO_DEEP", "expression depth exceeds 10", step.id)
            )
        if nodes > MAX_EXPRESSION_NODES:
            issues.append(
                _issue(
                    "EXPRESSION_TOO_LARGE",
                    "expression has more than 100 subexpressions",
                    step.id,
                )
            )
    return issues


def _validate_policies(definition: WorkflowDefinition) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    policy = definition.execution_policy
    for name, ceiling in GLOBAL_BUDGET_CEILINGS.items():
        value = getattr(policy, name)
        if value > ceiling:
            issues.append(
                _issue(
                    "POLICY_RELAXED",
                    (
                        f"{name} may not exceed the global value {ceiling} "
                        "without explicit authorization"
                    ),
                )
            )

    direction_ids = {direction.id for direction in definition.directions}
    assigned: set[str] = set()
    for step in definition.steps:
        config = step.config
        if isinstance(config, AgentTaskConfig):
            for tool in step.policy.allowed_tools:
                if tool not in ALLOWED_TOOLS:
                    issues.append(
                        _issue("TOOL_NOT_REGISTERED", f"tool {tool!r} is not registered", step.id)
                    )
            for direction in config.direction_ids:
                if direction not in direction_ids:
                    issues.append(
                        _issue(
                            "DIRECTION_UNKNOWN",
                            f"direction {direction!r} is not defined",
                            step.id,
                        )
                    )
                assigned.add(direction)
            if not config.direction_ids:
                issues.append(
                    _issue("DIRECTION_MISSING", "an AgentTask must declare its directions", step.id)
                )
        if isinstance(config, ToolTaskConfig):
            if config.tool_name not in ALLOWED_TOOLS:
                issues.append(
                    _issue(
                        "TOOL_NOT_REGISTERED",
                        f"tool {config.tool_name!r} is not registered",
                        step.id,
                    )
                )
            if config.tool_name not in step.policy.allowed_tools:
                issues.append(
                    _issue(
                        "TOOL_NOT_ALLOWED",
                        "the step tool must also be present in policy.allowed_tools",
                        step.id,
                    )
                )
            if config.direction_id is not None:
                if config.direction_id not in direction_ids:
                    issues.append(
                        _issue("DIRECTION_UNKNOWN", "unknown direction id", step.id)
                    )
                assigned.add(config.direction_id)
        if isinstance(config, TransformConfig) and config.transform_id not in ALLOWED_TRANSFORMS:
            issues.append(
                _issue("TRANSFORM_NOT_REGISTERED", "unknown pure transform", step.id)
            )
        if isinstance(config, HumanReviewConfig) and not config.reason_code:
            issues.append(
                _issue(
                    "REVIEW_REASON_MISSING",
                    "HumanReview needs a reason_code",
                    step.id,
                )
            )

        if step.policy.max_attempts > policy.max_attempts_per_step:
            issues.append(
                _issue("STEP_POLICY_RELAXED", "step attempts exceed the execution policy", step.id)
            )
        if step.policy.timeout_seconds > policy.tool_timeout_seconds:
            issues.append(
                _issue("STEP_TIMEOUT_RELAXED", "step timeout exceeds the execution policy", step.id)
            )
        for domain in step.policy.allowed_domains:
            if not _valid_domain(domain):
                issues.append(
                    _issue(
                        "DOMAIN_INVALID",
                        f"domain {domain!r} must be a bare hostname (no scheme, port or path)",
                        step.id,
                    )
                )

    for direction_item in definition.directions:
        if direction_item.id not in assigned:
            issues.append(
                _issue(
                    "DIRECTION_UNUSED",
                    f"direction {direction_item.id!r} has no step producing results or gaps",
                )
            )
    return issues


def _validate_completion(definition: WorkflowDefinition) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    by_id = {step.id: step for step in definition.steps}
    for step_id in definition.completion_policy.required_output_step_ids:
        step = by_id.get(step_id)
        if step is None:
            issues.append(
                _issue("COMPLETION_STEP_UNKNOWN", "required output step is not defined")
            )
        elif not isinstance(step.config, OutputConfig):
            issues.append(
                _issue("COMPLETION_STEP_NOT_OUTPUT", "required step is not an Output", step_id)
            )
    direction_ids = {direction.id for direction in definition.directions}
    for direction_id in definition.completion_policy.required_direction_ids:
        if direction_id not in direction_ids:
            issues.append(
                _issue("COMPLETION_DIRECTION_UNKNOWN", "required direction is not defined")
            )
    required = set(definition.completion_policy.required_output_step_ids)
    reachable_outputs = {
        step.id for step in definition.steps if step.type == "Output"
    }
    if required and not (required & reachable_outputs):
        issues.append(
            _issue("COMPLETION_UNREACHABLE", "no required Output is reachable in the definition")
        )
    return issues


def _issue(
    code: str,
    message: str,
    step_id: str | None = None,
    path: str | None = None,
) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, step_id=step_id, path=path)


def _reachable(entry: str, outgoing: dict[str, list[tuple[str, str]]]) -> set[str]:
    seen: set[str] = set()
    stack = [entry]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(target for target, _on in outgoing.get(node, []))
    return seen


def _has_output_path(
    entry: str, outgoing: dict[str, list[tuple[str, str]]], outputs: set[str]
) -> bool:
    """Normal (success/true/false) edges only; exhausted is an explicit failure path."""
    seen: set[str] = set()
    stack = [entry]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        if node in outputs:
            return True
        stack.extend(
            target for target, on in outgoing.get(node, []) if on in {"success", "true", "false"}
        )
    return False


def _ancestors(node: str, incoming: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(incoming.get(node, []))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(incoming.get(current, []))
    return seen


def _expression_step_refs(expression: Expression) -> set[str]:
    refs: set[str] = set()
    if expression.op in {"and", "or"}:
        for child in expression.args:
            refs |= _expression_step_refs(child)
    elif expression.op == "not":
        if expression.arg is not None:
            refs |= _expression_step_refs(expression.arg)
    else:
        for operand in (expression.left, expression.right):
            if operand is not None and operand.ref is not None and operand.ref.step_id:
                refs.add(operand.ref.step_id)
    return refs


def _collect_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str):
                refs.append(item)
            else:
                refs.extend(_collect_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_collect_refs(item))
    return refs


def _valid_domain(domain: str) -> bool:
    if not domain or domain != domain.strip():
        return False
    if "://" in domain or "/" in domain or ":" in domain or "*" in domain:
        return False
    if domain.startswith(".") or domain.endswith("."):
        return False
    return all(part and part.replace("-", "").isalnum() for part in domain.split("."))

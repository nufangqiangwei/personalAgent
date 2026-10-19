"""Assistance and user decision enums shared by engine and API.

The available decisions depend on *why* the run is waiting. A review is closed by an
explicit answer that satisfies the step's decision schema; a budget block needs an
explicit top-up **and** an explicit "continue"; a write block can only be cleared by
the server re-reading the target. Silence, viewing or chatting never counts.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class AssistanceKind(StrEnum):
    BUDGET_LIMIT = "BUDGET_LIMIT"
    WRITE_RECONCILIATION = "WRITE_RECONCILIATION"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    RUNTIME_UNCERTAIN = "RUNTIME_UNCERTAIN"


class AssistanceStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


class UserDecisionType(StrEnum):
    DISCUSS = "DISCUSS"
    GRANT_AND_CONTINUE = "GRANT_AND_CONTINUE"
    PAUSE = "PAUSE"
    CANCEL = "CANCEL"
    CHANGE_REQUIREMENTS = "CHANGE_REQUIREMENTS"
    RECONCILE = "RECONCILE"


#: Every kind can be discussed, paused, cancelled or sent back for new requirements.
_COMMON = frozenset(
    {
        UserDecisionType.DISCUSS,
        UserDecisionType.PAUSE,
        UserDecisionType.CANCEL,
        UserDecisionType.CHANGE_REQUIREMENTS,
    }
)

#: A budget block is the only block an explicit top-up can clear.
AVAILABLE_DECISIONS: dict[str, frozenset[UserDecisionType]] = {
    AssistanceKind.BUDGET_LIMIT.value: _COMMON | {UserDecisionType.GRANT_AND_CONTINUE},
    AssistanceKind.HUMAN_REVIEW.value: _COMMON | {UserDecisionType.GRANT_AND_CONTINUE},
    AssistanceKind.WRITE_RECONCILIATION.value: _COMMON | {UserDecisionType.RECONCILE},
    # An unknown model outcome is never unlocked by a decision; only real completion
    # evidence plus an explicit resume can do that.
    AssistanceKind.RUNTIME_UNCERTAIN.value: _COMMON,
}


def available_decisions(kind: str) -> list[str]:
    return sorted(choice.value for choice in AVAILABLE_DECISIONS.get(kind, _COMMON))


def validate_review_result(
    schema: dict[str, Any] | None, result: dict[str, Any] | None
) -> list[str]:
    """Validate a human review answer against the step's declared schema.

    The subset is intentionally small: object shape, required keys, simple types,
    enums and ``additionalProperties: false``. An empty schema still requires an
    explicit answer, because silence is not a review outcome.
    """
    if result is None:
        return ["需要提供明确的审核结果（review_result）"]
    if not isinstance(result, dict):
        return ["审核结果必须是 JSON 对象"]

    errors: list[str] = []
    declared = schema or {}
    if not declared:
        return errors
    schema_type = declared.get("type")
    if schema_type is not None and schema_type != "object":
        return [f"审核结果契约只支持 object，实际为 {schema_type!r}"]

    required = declared.get("required") or []
    for key in required:
        if key not in result:
            errors.append(f"缺少必需字段 {key}")

    properties = declared.get("properties") or {}
    if declared.get("additionalProperties") is False:
        for key in result:
            if key not in properties:
                errors.append(f"不允许的字段 {key}")

    type_map: dict[str, Any] = {
        "string": str,
        "boolean": bool,
        "integer": int,
        "number": (int, float),
        "array": list,
        "object": dict,
    }
    for key, rule in properties.items():
        if key not in result or not isinstance(rule, dict):
            continue
        value = result[key]
        expected = rule.get("type")
        if expected in type_map and not isinstance(value, type_map[expected]):
            errors.append(f"字段 {key} 类型应为 {expected}")
            continue
        if expected == "integer" and isinstance(value, bool):
            errors.append(f"字段 {key} 类型应为 integer")
            continue
        enum = rule.get("enum")
        if enum is not None and value not in enum:
            errors.append(f"字段 {key} 的取值必须属于 {enum}")
    return errors


__all__ = [
    "AVAILABLE_DECISIONS",
    "AssistanceKind",
    "AssistanceStatus",
    "UserDecisionType",
    "available_decisions",
    "validate_review_result",
]

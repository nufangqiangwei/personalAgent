"""Bounded expression evaluation for Condition steps.

Only the declared AST is supported: no scripts, regex execution or dynamic
function names. A missing value or a type mismatch raises ``CONDITION_INVALID``
instead of being treated as truthy.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from personal_agent.domain.common import AppError
from personal_agent.domain.workflow_schema import Binding, Expression, Operand

Resolver = Callable[[Binding], Any]


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - debugging
        return "<missing>"


MISSING = _Missing()


def _invalid(message: str) -> AppError:
    return AppError(
        code="CONDITION_INVALID",
        message=message,
        status=422,
    )


def resolve_binding(
    binding: Binding,
    *,
    input_values: Mapping[str, Any],
    step_outputs: Mapping[str, Any],
) -> Any:
    pointer = binding.pointer
    if not pointer.startswith("/"):
        raise _invalid("a binding pointer must start with '/'")
    if binding.source == "step":
        if not binding.step_id:
            raise _invalid("a step binding requires step_id")
        root: Any = step_outputs.get(binding.step_id)
    else:
        root = input_values
    if root is None:
        return MISSING
    current = root
    for raw in pointer.lstrip("/").split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping) and token in current:
            current = current[token]
        elif isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError):
                return MISSING
        else:
            return MISSING
    return current


def _operand_value(operand: Operand, resolve: Resolver) -> Any:
    if operand.ref is not None:
        return resolve(operand.ref)
    return operand.literal


def evaluate(expression: Expression, resolve: Resolver) -> bool:
    if expression.op == "and":
        return all(evaluate(child, resolve) for child in expression.args)
    if expression.op == "or":
        return any(evaluate(child, resolve) for child in expression.args)
    if expression.op == "not":
        if expression.arg is None:
            raise _invalid("'not' requires an argument")
        return not evaluate(expression.arg, resolve)

    left = _operand_value(expression.left, resolve) if expression.left else MISSING
    right = _operand_value(expression.right, resolve) if expression.right else MISSING
    if left is MISSING or right is MISSING:
        raise _invalid(f"operand is missing for {expression.op}")
    if isinstance(left, bool) and isinstance(right, bool):
        pass
    elif (
        isinstance(left, (int, float))
        and isinstance(right, (int, float))
        and not isinstance(left, bool)
    ):
        pass
    elif isinstance(left, str) and isinstance(right, str):
        if expression.op not in {"eq", "ne", "contains", "lt", "lte", "gt", "gte"}:
            raise _invalid(f"{expression.op} is not valid for strings")
    else:
        raise _invalid(f"incompatible operand types for {expression.op}")

    match expression.op:
        case "eq":
            return left == right
        case "ne":
            return left != right
        case "lt":
            return bool(cast(Any, left) < cast(Any, right))
        case "lte":
            return bool(cast(Any, left) <= cast(Any, right))
        case "gt":
            return bool(cast(Any, left) > cast(Any, right))
        case "gte":
            return bool(cast(Any, left) >= cast(Any, right))
        case "contains":
            if isinstance(left, str) and isinstance(right, str):
                return right in left
            if isinstance(left, (list, tuple, set)):
                return right in left
            raise _invalid("contains requires a string or a collection")
        case _:  # pragma: no cover - schema forbids other operators
            raise _invalid(f"unsupported operator {expression.op}")

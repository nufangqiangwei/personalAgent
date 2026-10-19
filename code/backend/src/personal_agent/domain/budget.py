"""Budget ledger arithmetic.

Two disjoint scopes exist and never share or transfer value:

* a research ledger keyed by ``root_run_id`` (shared by every branch of one root);
* a draft ledger keyed by ``draft_id`` (requirement chat only).

All quantities are non-negative, ``consumed`` is monotonic, and an exhausted
dimension is derived (``limits - consumed - reserved <= 0``) rather than stored
as a boolean that could drift from the entries.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from personal_agent.domain.common import AppError, ErrorCategory

RUN_DIMENSIONS: Final[tuple[str, ...]] = (
    "model_requests",
    "tool_calls",
    "search_calls",
    "page_reads",
    "tokens",
    "active_ms",
)
DRAFT_DIMENSIONS: Final[tuple[str, ...]] = ("model_requests", "tokens")

#: A single explicit top-up may not exceed this multiple of the default policy
#: value; it prevents one click from granting an unbounded budget.
MAX_GRANT_MULTIPLIER: Final = 10


class LedgerScope(StrEnum):
    RUN = "RUN"
    DRAFT = "DRAFT"


class EntryKind(StrEnum):
    RESERVE = "RESERVE"
    SETTLE = "SETTLE"
    RELEASE = "RELEASE"
    GRANT = "GRANT"


@dataclass(frozen=True, slots=True)
class Ledger:
    scope: LedgerScope
    limits: Mapping[str, int]
    consumed: Mapping[str, int]
    reserved: Mapping[str, int]
    active_ms: int = 0
    revision: int = 1

    def __post_init__(self) -> None:
        dimensions = RUN_DIMENSIONS if self.scope is LedgerScope.RUN else DRAFT_DIMENSIONS
        for name in ("limits", "consumed", "reserved"):
            values = getattr(self, name)
            unknown = set(values) - set(dimensions)
            if unknown:
                raise ValueError(f"{name} has unknown dimensions: {sorted(unknown)}")
            for key, value in values.items():
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValueError(f"{name}.{key} must be a non-negative integer")
        for key in dimensions:
            if self.consumed.get(key, 0) > self.limits.get(key, 0) + self.reserved.get(key, 0):
                raise ValueError(f"consumed.{key} exceeds limits + reserved")

    def remaining(self) -> dict[str, int]:
        return {
            key: max(
                0,
                self.limits.get(key, 0)
                - self.consumed.get(key, 0)
                - self.reserved.get(key, 0),
            )
            for key in self.limits
        }

    def exhausted_dimensions(self) -> list[str]:
        return [key for key, left in self.remaining().items() if left <= 0]

    @property
    def exhausted(self) -> bool:
        return bool(self.exhausted_dimensions())

    def as_dict(self) -> dict[str, object]:
        return {
            "scope": str(self.scope),
            "limits": dict(self.limits),
            "consumed": dict(self.consumed),
            "reserved": dict(self.reserved),
            "remaining": self.remaining(),
            "active_ms": self.active_ms,
            "revision": self.revision,
            "exhausted": self.exhausted,
            "exhausted_dimensions": self.exhausted_dimensions(),
        }


def empty_ledger(scope: LedgerScope, limits: Mapping[str, int]) -> Ledger:
    dimensions = RUN_DIMENSIONS if scope is LedgerScope.RUN else DRAFT_DIMENSIONS
    unknown = set(limits) - set(dimensions)
    if unknown:
        raise ValueError(f"limits has unknown dimensions: {sorted(unknown)}")
    zero = {key: 0 for key in limits}
    return Ledger(scope=scope, limits=dict(limits), consumed=zero, reserved=zero)


def _require_positive(scope: LedgerScope, amount: Mapping[str, int]) -> dict[str, int]:
    dimensions = RUN_DIMENSIONS if scope is LedgerScope.RUN else DRAFT_DIMENSIONS
    if not amount:
        raise AppError(
            code="BUDGET_AMOUNT_INVALID",
            message="at least one dimension must be positive",
            category=ErrorCategory.BUDGET,
            status=422,
        )
    cleaned: dict[str, int] = {}
    for key, value in amount.items():
        if key not in dimensions:
            raise AppError(
                code="BUDGET_AMOUNT_INVALID",
                message=f"unknown budget dimension {key!r}",
                status=422,
                details={"dimension": key, "allowed": list(dimensions)},
            )
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AppError(
                code="BUDGET_AMOUNT_INVALID",
                message=f"dimension {key!r} must be a positive integer",
                status=422,
                details={"dimension": key},
            )
        cleaned[key] = value
    return cleaned


def reserve(ledger: Ledger, worst_case: Mapping[str, int]) -> Ledger:
    """Reserve the worst-case usage before an action starts.

    Refuses instead of silently shrinking the reservation: a caller that cannot
    bound its request must not start one.
    """
    amount = _require_positive(ledger.scope, worst_case)
    reserved = dict(ledger.reserved)
    for key, value in amount.items():
        reserved[key] = reserved.get(key, 0) + value
    if any(
        ledger.consumed.get(key, 0) + reserved.get(key, 0) > ledger.limits.get(key, 0)
        for key in ledger.limits
    ):
        raise AppError(
            code="BUDGET_ASSISTANCE_REQUIRED",
            message="the reservation does not fit in the remaining budget",
            category=ErrorCategory.BUDGET,
            status=409,
            details={"requested": dict(amount), "remaining": ledger.remaining()},
        )
    return replace(ledger, reserved=reserved, revision=ledger.revision + 1)


def settle(ledger: Ledger, reserved_amount: Mapping[str, int], actual: Mapping[str, int]) -> Ledger:
    """Consume the reservation and account the actual usage.

    Actual usage above the reservation is still charged; it is never treated as
    zero and never allowed to reduce ``consumed``.
    """
    reserved = dict(ledger.reserved)
    consumed = dict(ledger.consumed)
    for key in set(reserved_amount) | set(actual):
        held = reserved_amount.get(key, 0)
        reserved[key] = max(0, reserved.get(key, 0) - held)
        charge = max(held, actual.get(key, 0))
        consumed[key] = consumed.get(key, 0) + max(0, charge)
    if any(consumed[key] < ledger.consumed.get(key, 0) for key in consumed):
        raise AppError(
            code="BUDGET_LEDGER_INVALID",
            message="consumed must never decrease",
            category=ErrorCategory.INTEGRITY,
            status=409,
        )
    return replace(ledger, reserved=reserved, consumed=consumed, revision=ledger.revision + 1)


def release(ledger: Ledger, reserved_amount: Mapping[str, int]) -> Ledger:
    reserved = dict(ledger.reserved)
    for key, value in reserved_amount.items():
        reserved[key] = max(0, reserved.get(key, 0) - value)
    return replace(ledger, reserved=reserved, revision=ledger.revision + 1)


def grant(
    ledger: Ledger,
    increment: Mapping[str, int],
    *,
    default_limits: Mapping[str, int],
) -> Ledger:
    """Explicit user grant: raises limits only, never touches consumed/reserved."""
    amount = _require_positive(ledger.scope, increment)
    limits = dict(ledger.limits)
    for key, value in amount.items():
        ceiling = default_limits.get(key)
        if ceiling is not None and value > ceiling * MAX_GRANT_MULTIPLIER:
            raise AppError(
                code="DRAFT_BUDGET_GRANT_INVALID",
                message=f"grant for {key!r} exceeds the single-grant maximum",
                status=422,
                details={"dimension": key, "max": ceiling * MAX_GRANT_MULTIPLIER},
            )
        limits[key] = limits.get(key, 0) + value
    return replace(ledger, limits=limits, revision=ledger.revision + 1)


def require_exhausted(ledger: Ledger) -> None:
    """A top-up is only allowed after a dimension actually reached zero."""
    if not ledger.exhausted:
        raise AppError(
            code="DRAFT_BUDGET_NOT_EXHAUSTED",
            message="draft budget is not exhausted; no preventive top-up is allowed",
            status=409,
            details={"remaining": ledger.remaining()},
        )

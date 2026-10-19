from __future__ import annotations

import pytest

from personal_agent.domain import budget as rules
from personal_agent.domain.budget import LedgerScope
from personal_agent.domain.common import AppError

LIMITS = {"model_requests": 2, "tokens": 100}
DEFAULT_LIMITS = {"model_requests": 2, "tokens": 100}


def ledger() -> rules.Ledger:
    return rules.empty_ledger(LedgerScope.DRAFT, LIMITS)


def test_remaining_and_exhaustion_are_derived() -> None:
    state = ledger()
    assert state.remaining() == {"model_requests": 2, "tokens": 100}
    assert not state.exhausted
    reserved = rules.reserve(state, {"model_requests": 2})
    assert reserved.exhausted
    assert reserved.exhausted_dimensions() == ["model_requests"]
    assert reserved.remaining()["model_requests"] == 0


def test_reservation_that_does_not_fit_requires_assistance() -> None:
    state = rules.reserve(ledger(), {"model_requests": 2})
    with pytest.raises(AppError) as error:
        rules.reserve(state, {"model_requests": 1})
    assert error.value.code == "BUDGET_ASSISTANCE_REQUIRED"


def test_settle_charges_the_reservation_even_if_actual_is_lower() -> None:
    state = ledger()
    reserved = rules.reserve(state, {"tokens": 40})
    settled = rules.settle(reserved, {"tokens": 40}, {"tokens": 10})
    assert settled.consumed["tokens"] == 40
    assert settled.reserved["tokens"] == 0
    assert settled.revision == state.revision + 2


def test_actual_usage_above_reservation_is_still_charged() -> None:
    reserved = rules.reserve(ledger(), {"tokens": 20})
    settled = rules.settle(reserved, {"tokens": 20}, {"tokens": 55})
    assert settled.consumed["tokens"] == 55


def test_release_returns_only_unspent_reservation() -> None:
    reserved = rules.reserve(ledger(), {"tokens": 30})
    released = rules.release(reserved, {"tokens": 30})
    assert released.reserved["tokens"] == 0
    assert released.consumed["tokens"] == 0


def test_consumed_never_decreases() -> None:
    state = rules.settle(rules.reserve(ledger(), {"tokens": 30}), {"tokens": 30}, {"tokens": 30})
    settled_again = rules.settle(state, {"tokens": 0}, {"tokens": 0})
    assert settled_again.consumed["tokens"] == state.consumed["tokens"]
    with pytest.raises(ValueError):
        rules.Ledger(
            scope=LedgerScope.DRAFT,
            limits=state.limits,
            consumed={"model_requests": -1, "tokens": 0},
            reserved={},
            revision=state.revision,
        )


def test_grant_raises_limits_only_and_never_resets_consumption() -> None:
    state = rules.settle(rules.reserve(ledger(), {"tokens": 100}), {"tokens": 100}, {"tokens": 100})
    state = rules.Ledger(
        scope=state.scope,
        limits=state.limits,
        consumed=state.consumed,
        reserved={"model_requests": 2, "tokens": 0},
        revision=state.revision,
    )
    assert state.exhausted
    granted = rules.grant(state, {"model_requests": 3, "tokens": 50}, default_limits=DEFAULT_LIMITS)
    assert granted.limits == {"model_requests": 5, "tokens": 150}
    assert granted.consumed == state.consumed
    assert granted.reserved == state.reserved
    assert granted.revision == state.revision + 1


def test_grant_requires_an_exhausted_ledger() -> None:
    with pytest.raises(AppError) as error:
        rules.require_exhausted(ledger())
    assert error.value.code == "DRAFT_BUDGET_NOT_EXHAUSTED"


def test_grant_rejects_zero_negative_unknown_and_oversized_amounts() -> None:
    exhausted = rules.Ledger(
        scope=LedgerScope.DRAFT,
        limits={"model_requests": 0, "tokens": 0},
        consumed={"model_requests": 0, "tokens": 0},
        reserved={"model_requests": 0, "tokens": 0},
    )
    for bad in ({"model_requests": 0}, {"model_requests": -1}, {"unknown": 1}):
        with pytest.raises(AppError):
            rules.grant(exhausted, bad, default_limits=DEFAULT_LIMITS)
    with pytest.raises(AppError) as error:
        rules.grant(
            exhausted,
            {"model_requests": DEFAULT_LIMITS["model_requests"] * rules.MAX_GRANT_MULTIPLIER + 1},
            default_limits=DEFAULT_LIMITS,
        )
    assert error.value.code == "DRAFT_BUDGET_GRANT_INVALID"


def test_run_ledger_rejects_draft_only_dimensions() -> None:
    with pytest.raises(ValueError):
        rules.empty_ledger(LedgerScope.RUN, {"draft_tokens": 1})


def test_snapshot_exposes_contract_fields() -> None:
    snapshot = ledger().as_dict()
    assert snapshot["scope"] == "DRAFT"
    assert snapshot["exhausted"] is False
    assert snapshot["remaining"] == {"model_requests": 2, "tokens": 100}

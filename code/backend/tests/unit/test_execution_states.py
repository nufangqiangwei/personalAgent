from __future__ import annotations

import pytest

from personal_agent.domain.common import AppError
from personal_agent.domain.execution import (
    IN_FLIGHT_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    RunStatus,
    assert_transition,
    can_transition,
    cancel_requested,
    is_terminal,
    release_slot_allowed,
)


def test_run_state_machine_matches_the_contract_table() -> None:
    assert can_transition(RunStatus.PENDING, RunStatus.RUNNING)
    assert can_transition(RunStatus.RUNNING, RunStatus.WAITING_USER)
    assert can_transition(RunStatus.WAITING_USER, RunStatus.RUNNING)
    assert can_transition(RunStatus.PAUSING, RunStatus.PAUSED)
    assert can_transition(RunStatus.PAUSING, RunStatus.INTERRUPTED)
    assert can_transition(RunStatus.CANCELLING, RunStatus.CANCELLED)
    assert can_transition(RunStatus.CANCELLING, RunStatus.INTERRUPTED)
    assert can_transition(RunStatus.PAUSED, RunStatus.RUNNING)
    assert can_transition(RunStatus.INTERRUPTED, RunStatus.RUNNING)


def test_terminal_states_never_continue() -> None:
    for terminal in TERMINAL_RUN_STATUSES:
        for target in RunStatus:
            assert not can_transition(terminal, target), f"{terminal} -> {target} must be illegal"
        assert is_terminal(terminal)


def test_cancel_is_not_resumable_even_when_a_late_result_arrives() -> None:
    assert cancel_requested(RunStatus.CANCELLED)
    assert cancel_requested(RunStatus.CANCELLING)
    assert not can_transition(RunStatus.CANCELLED, RunStatus.RUNNING)
    assert not can_transition(RunStatus.CANCELLED, RunStatus.SUCCEEDED)


def test_illegal_transition_raises_a_structured_conflict() -> None:
    with pytest.raises(AppError) as error:
        assert_transition(RunStatus.SUCCEEDED, RunStatus.RUNNING)
    assert error.value.code == "RUN_STATE_INVALID"
    assert error.value.status == 409


def test_slot_release_requires_convergence_and_no_unknown_write() -> None:
    assert release_slot_allowed(converged=True, unknown_writes=0, draining=False)
    assert not release_slot_allowed(converged=False, unknown_writes=0, draining=False)
    assert not release_slot_allowed(converged=True, unknown_writes=1, draining=False)
    assert not release_slot_allowed(converged=True, unknown_writes=0, draining=True)


def test_in_flight_statuses_drive_draining_ui() -> None:
    assert RunStatus.RUNNING in IN_FLIGHT_RUN_STATUSES
    assert RunStatus.PAUSING in IN_FLIGHT_RUN_STATUSES
    assert RunStatus.CANCELLING in IN_FLIGHT_RUN_STATUSES
    assert RunStatus.PAUSED not in IN_FLIGHT_RUN_STATUSES

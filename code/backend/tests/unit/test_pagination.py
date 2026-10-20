"""Keyset cursor contract (T137)."""

from __future__ import annotations

import pytest

from personal_agent.domain.common import AppError
from personal_agent.domain.pagination import decode_cursor, encode_cursor


def test_cursor_round_trips_and_is_filter_bound() -> None:
    cursor = encode_cursor(
        sort_key="2026-10-19T12:00:00Z", item_id="run-1", filter_key="runs:"
    )
    assert decode_cursor(cursor, filter_key="runs:") == (
        "2026-10-19T12:00:00Z",
        "run-1",
    )
    with pytest.raises(AppError) as mismatch:
        decode_cursor(cursor, filter_key="runs:parent-1")
    assert mismatch.value.code == "CURSOR_FILTER_MISMATCH"
    with pytest.raises(AppError) as invalid:
        decode_cursor("not-a-cursor", filter_key="runs:")
    assert invalid.value.code == "INVALID_CURSOR"

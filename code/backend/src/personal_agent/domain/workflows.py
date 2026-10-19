"""Workflow aggregate helpers.

A workflow owns immutable versions. ``latest_executable_version_id`` only ever
points at a version that passed validation and was explicitly confirmed as
executable; it is never inferred from the highest version number.
"""

from __future__ import annotations

from personal_agent.domain.common import AppError, ErrorCategory


def next_version_no(existing: list[int]) -> int:
    return (max(existing) + 1) if existing else 1


def is_content_unchanged(existing_hash: str, candidate_hash: str) -> bool:
    return existing_hash == candidate_hash


def assert_editable(disabled_at: object | None) -> None:
    if disabled_at is not None:
        raise AppError(
            code="WORKFLOW_DISABLED",
            message="a disabled workflow no longer accepts new versions",
            status=409,
        )


def assert_base_matches(current_latest_version_id: str | None, base_version_id: str) -> None:
    """Optimistic editing baseline: a stale editor must not save over a new version."""
    if current_latest_version_id != base_version_id:
        raise AppError(
            code="VERSION_CHANGED",
            message="the workflow changed since this editor was opened",
            status=409,
            details={
                "current_version_id": current_latest_version_id,
                "base_version_id": base_version_id,
            },
        )


def executable_flag(*, validated: bool, confirmed: bool) -> bool:
    return validated and confirmed


def ensure_latest_belongs(latest_version_id: str | None, workflow_version_ids: set[str]) -> None:
    if latest_version_id is None:
        return
    if latest_version_id not in workflow_version_ids:
        raise AppError(
            code="LATEST_POINTER_INVALID",
            message="latest_executable_version_id must belong to this workflow",
            category=ErrorCategory.INTEGRITY,
            status=409,
        )

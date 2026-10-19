"""Artifact and side-effect rules.

A managed write records an ``INTENT`` before touching the filesystem, publishes an
immutable file, and only then commits the artifact row and success state in the
same transaction. A crash may leave an orphan file; it must never leave a
database row that claims a product which was not fully written.
"""

from __future__ import annotations

from enum import StrEnum

from personal_agent.domain.common import AppError, ErrorCategory


class SideEffectStatus(StrEnum):
    INTENT = "INTENT"
    NOT_APPLIED = "NOT_APPLIED"
    APPLIED = "APPLIED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"


#: States that still need a human decision; they block quiescence.
UNRESOLVED_SIDE_EFFECTS = frozenset(
    {
        SideEffectStatus.INTENT,
        SideEffectStatus.PARTIAL,
        SideEffectStatus.UNKNOWN,
        SideEffectStatus.CONFLICT,
    }
)

RESOLVED_SIDE_EFFECTS = frozenset(
    {SideEffectStatus.APPLIED, SideEffectStatus.NOT_APPLIED}
)


class ArtifactFormat(StrEnum):
    MARKDOWN = "markdown"


class ArtifactState(StrEnum):
    AVAILABLE = "AVAILABLE"
    EXPIRED = "EXPIRED"
    DELETED = "DELETED"
    INTEGRITY_ERROR = "INTEGRITY_ERROR"


class ArtifactStructure(StrEnum):
    REPORT = "report"
    CHECKLIST = "checklist"
    COMPARISON = "comparison"


class ArtifactKind(StrEnum):
    DOCUMENT = "DOCUMENT"


def classify_observation(
    *,
    intended_after_hash: str,
    observed_hash: str | None,
    previous_hash: str | None,
) -> SideEffectStatus:
    """Classify a managed write by comparing the target with the intent.

    * target equals the intended content -> APPLIED (reusable);
    * target absent and never changed -> NOT_APPLIED (retry may be allowed);
    * target is the previous content -> NOT_APPLIED (our write did not land);
    * target changed to something else -> CONFLICT;
    * hash unreadable -> UNKNOWN.

    A missing file alone is not proof it was never written, so callers must pass
    the operation/target history rather than just ``exists()``.
    """
    if observed_hash is None:
        return SideEffectStatus.UNKNOWN
    if observed_hash == intended_after_hash:
        return SideEffectStatus.APPLIED
    if previous_hash is not None and observed_hash == previous_hash:
        return SideEffectStatus.NOT_APPLIED
    return SideEffectStatus.CONFLICT


def require_quiescent_side_effects(statuses: list[SideEffectStatus]) -> None:
    unresolved = [status.value for status in statuses if status in UNRESOLVED_SIDE_EFFECTS]
    if unresolved:
        raise AppError(
            code="SIDE_EFFECT_UNRESOLVED",
            message="unresolved managed writes block this operation",
            category=ErrorCategory.INTEGRITY,
            status=409,
            details={"statuses": unresolved},
        )

"""Assistance and user decision enums shared by engine and API."""

from __future__ import annotations

from enum import StrEnum


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

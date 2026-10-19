"""Metadata registry.

Importing this module registers every mapped class on ``Base.metadata``. Alembic
autogeneration and the migration consistency check both use it, so a model that
is missing here is a bug rather than an invisible table.
"""

from __future__ import annotations

from personal_agent.adapters.persistence.artifact_models import (
    Artifact,
    Blob,
    SideEffectOperation,
)
from personal_agent.adapters.persistence.assistance_models import (
    AssistanceRequest,
    UserDecision,
)
from personal_agent.adapters.persistence.base import Base
from personal_agent.adapters.persistence.branch_models import BranchProposal, ReusedResult
from personal_agent.adapters.persistence.budget_models import (
    BudgetEntry,
    BudgetLedger,
    ModelRequest,
)
from personal_agent.adapters.persistence.execution_models import (
    ExecutionEvent,
    ExecutionSlot,
    StepAttempt,
    StepRun,
    WorkflowRun,
)
from personal_agent.adapters.persistence.operation_models import AnalysisOperation
from personal_agent.adapters.persistence.request_models import RequestRecord
from personal_agent.adapters.persistence.requirement_models import (
    Confirmation,
    ConversationMessage,
    RequirementDraft,
    Suggestion,
)
from personal_agent.adapters.persistence.research_models import (
    Evidence,
    ResearchAction,
    Source,
)
from personal_agent.adapters.persistence.session_models import SessionLink
from personal_agent.adapters.persistence.workflow_models import Workflow, WorkflowVersion

__all__ = [
    "AnalysisOperation",
    "Artifact",
    "AssistanceRequest",
    "Base",
    "Blob",
    "BranchProposal",
    "BudgetEntry",
    "BudgetLedger",
    "Confirmation",
    "ConversationMessage",
    "Evidence",
    "ExecutionEvent",
    "ExecutionSlot",
    "ModelRequest",
    "RequestRecord",
    "RequirementDraft",
    "ResearchAction",
    "ReusedResult",
    "SessionLink",
    "SideEffectOperation",
    "Source",
    "StepAttempt",
    "StepRun",
    "Suggestion",
    "UserDecision",
    "Workflow",
    "WorkflowRun",
    "WorkflowVersion",
]

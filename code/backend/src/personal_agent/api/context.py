"""Application context shared by routers and middleware."""

from __future__ import annotations

from dataclasses import dataclass

from personal_agent.adapters.harness.model_gateway import ModelGateway
from personal_agent.adapters.persistence.database import Database
from personal_agent.api.security import OriginPolicy, SessionManager
from personal_agent.application.artifact_service import ArtifactService
from personal_agent.application.assistance_service import AssistanceService
from personal_agent.application.branch_service import BranchService
from personal_agent.application.budget_service import BudgetService
from personal_agent.application.coordinator import ExecutionCoordinator
from personal_agent.application.engine import WorkflowEngine
from personal_agent.application.managed_writes import ManagedWriter
from personal_agent.application.recovery import RecoveryService
from personal_agent.application.research_evidence import EvidenceService
from personal_agent.application.stop_control import StopControl
from personal_agent.bootstrap.config import AppConfig
from personal_agent.bootstrap.executor import RunExecutor
from personal_agent.ports.contracts import Clock, RuntimePort, ToolPort


@dataclass
class AppContext:
    config: AppConfig
    database: Database
    clock: Clock
    runtime: RuntimePort
    tools: ToolPort
    gateway: ModelGateway
    budget: BudgetService
    writer: ManagedWriter
    artifacts: ArtifactService
    evidence: EvidenceService
    engine: WorkflowEngine
    coordinator: ExecutionCoordinator
    executor: RunExecutor
    control: StopControl
    recovery: RecoveryService
    assistance: AssistanceService
    branches: BranchService
    sessions: SessionManager
    origin_policy: OriginPolicy
    execution_available: bool
    migration_current: str | None
    migration_head: str | None
    runtime_capabilities: dict[str, object]
    unknown_model_request_id: str | None = None

    @property
    def migrations_up_to_date(self) -> bool:
        return (
            self.migration_head is not None
            and self.migration_current == self.migration_head
        )

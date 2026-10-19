"""Application context shared by routers and middleware."""

from __future__ import annotations

from dataclasses import dataclass

from personal_agent.adapters.harness.model_gateway import ModelGateway
from personal_agent.adapters.persistence.database import Database
from personal_agent.api.security import OriginPolicy, SessionManager
from personal_agent.application.budget_service import BudgetService
from personal_agent.application.managed_writes import ManagedWriter
from personal_agent.bootstrap.config import AppConfig
from personal_agent.ports.contracts import Clock, RuntimePort


@dataclass
class AppContext:
    config: AppConfig
    database: Database
    clock: Clock
    runtime: RuntimePort
    gateway: ModelGateway
    budget: BudgetService
    writer: ManagedWriter
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

"""Application lifespan: build adapters and run startup diagnostics.

Startup never starts research. It diagnoses the last known state, marks abandoned
in-flight runs as ``INTERRUPTED`` and leaves the decision to the user.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text

from personal_agent.adapters.harness.mock_runtime import MockRuntime
from personal_agent.adapters.harness.model_gateway import ModelGateway, ModelTransport
from personal_agent.adapters.harness.model_provider import ProviderTransport
from personal_agent.adapters.persistence.blob_store import BlobStore
from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.execution_models import (
    ExecutionEvent,
    ExecutionSlot,
    WorkflowRun,
)
from personal_agent.adapters.persistence.lifecycle_models import MaintenanceState
from personal_agent.adapters.tools.mock_service import (
    MockToolService,
    UnavailableToolService,
)
from personal_agent.adapters.tools.scope import ToolScopeRegistry
from personal_agent.api.context import AppContext
from personal_agent.api.security import OriginPolicy, SessionManager
from personal_agent.application.artifact_service import ArtifactService
from personal_agent.application.assistance_service import AssistanceService
from personal_agent.application.branch_analysis import BranchAnalyzer
from personal_agent.application.branch_service import BranchService
from personal_agent.application.budget_service import BudgetService
from personal_agent.application.coordinator import ExecutionCoordinator
from personal_agent.application.data_lifecycle import DataLifecycleService
from personal_agent.application.engine import EngineDeps, WorkflowEngine
from personal_agent.application.managed_writes import ManagedWriter
from personal_agent.application.recovery import RecoveryService
from personal_agent.application.research_evidence import EvidenceService
from personal_agent.application.stop_control import StopControl
from personal_agent.application.workflow_registry import WorkflowRegistry
from personal_agent.bootstrap import config as config_module
from personal_agent.bootstrap.clock import SystemClock
from personal_agent.bootstrap.executor import RunExecutor
from personal_agent.domain.common import AppError, ErrorCategory, new_id, stable_hash
from personal_agent.domain.execution import EventKind, RunStatus
from personal_agent.ports.contracts import (
    Clock,
    ModelRequest,
    ModelResponse,
    RuntimePort,
    ToolPort,
)

BACKEND_ROOT = Path(__file__).resolve().parents[3]


def migration_head() -> str | None:
    ini = BACKEND_ROOT / "alembic.ini"
    if not ini.exists():
        return None
    config = AlembicConfig(str(ini))
    config.attributes["configure_logger"] = False
    try:
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:  # pragma: no cover - broken install
        return None


async def migration_current(database: Database) -> str | None:
    try:
        async with database.session() as session:
            value = await session.scalar(text("SELECT version_num FROM alembic_version"))
            return str(value) if value else None
    except Exception:
        return None


def _observations() -> dict:
    import json

    fixture = BACKEND_ROOT / "tests" / "fixtures" / "observations.json"
    if not fixture.exists():  # pragma: no cover - packaged install
        return {"model": {"scenarios": {}}, "clock": {}}
    with fixture.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _execution_available(
    config: config_module.AppConfig, *, capabilities: dict | None = None,
    migration_version: str | None = None, expected_migration: str | None = None,
) -> bool:
    if config.mode == "mock":
        return True
    # The real runtime stays disabled until the adoption gate has recorded
    # passing evidence. A configured credential is not evidence.
    from personal_agent.adapters.harness.runtime import adoption_evidence

    return (
        adoption_evidence(profile_path=config.profile_path) is not None
        and bool(capabilities and capabilities.get("tool_policy_enforced") is True
                 and capabilities.get("model_gateway_enforced") is True)
        and migration_version is not None and migration_version == expected_migration
    )


async def ensure_execution_slot(database: Database, clock: Clock) -> None:
    async with database.session() as session:
        slot = await session.get(ExecutionSlot, 1)
        if slot is None:
            session.add(
                ExecutionSlot(
                    singleton_id=1,
                    run_id=None,
                    epoch=0,
                    process_instance_id=None,
                    draining=False,
                    revision=1,
                )
            )
            await session.commit()


async def mark_interrupted_runs(database: Database, clock: Clock) -> int:
    """Legacy RUNNING/PAUSING/CANCELLING runs become INTERRUPTED, never resumed."""
    legacy = [
        status.value
        for status in (RunStatus.RUNNING, RunStatus.PAUSING, RunStatus.CANCELLING)
    ]
    interrupted = 0
    async with database.session() as session:
        runs = (
            await session.scalars(select(WorkflowRun).where(WorkflowRun.status.in_(legacy)))
        ).all()
        for run in runs:
            previous = run.status
            run.status = RunStatus.INTERRUPTED.value
            run.reason_code = "RESTART"
            run.revision += 1
            last_sequence = await session.scalar(
                select(func.max(ExecutionEvent.sequence)).where(ExecutionEvent.run_id == run.id)
            )
            session.add(
                ExecutionEvent(
                    id=new_id(),
                    run_id=run.id,
                    draft_id=None,
                    sequence=int(last_sequence or 0) + 1,
                    step_run_id=None,
                    attempt_id=None,
                    workflow_version_id=run.workflow_version_id,
                    occurred_at=clock.now(),
                    kind=EventKind.RUN_STATE_CHANGED.value,
                    status=run.status,
                    reason_code="RESTART",
                    payload_json={"previous_status": previous, "auto_resumed": False},
                    payload_ref=None,
                    schema_version=1,
                    provider_event_key=None,
                )
            )
            interrupted += 1
        await session.commit()
    return interrupted


async def build_context(
    config: config_module.AppConfig,
    *,
    sessions: SessionManager | None = None,
    origin_policy: OriginPolicy | None = None,
    database: Database | None = None,
    clock: Clock | None = None,
    runtime: RuntimePort | None = None,
) -> AppContext:
    config.ensure_directories()
    clock = clock or SystemClock()
    database = database or Database(config.db_path)
    await ensure_execution_slot(database, clock)
    observations = _observations()
    tool_scopes = ToolScopeRegistry()

    # The supplier client is the only holder of the provider credential. Its
    # absence keeps live mode diagnosable but never falls back to Mock output.
    live_provider = _live_provider() if config.is_live else None
    if config.is_live:
        transport: ModelTransport = (
            ProviderTransport(live_provider) if live_provider is not None
            else UnavailableModelTransport()
        )
    else:
        from personal_agent.adapters.harness.mock_runtime import MockModelTransport

        transport = MockModelTransport(observations)
    gateway = ModelGateway(
        database,
        clock,
        transport,
        runtime_id=config.mode,
        allowed_model_ids={
            os.environ.get("PERSONAL_AGENT_MODEL", "deepseek-v4-flash")
            if config.is_live else "mock-deepseek-phase1"
        },
        enforce_owner=True,
    )
    if config.is_live:
        tools: Any = (
            _live_tools(config, clock, tool_scopes) if live_provider is not None
            else UnavailableToolService()
        )
    else:
        tools = MockToolService(observations, clock)

    live_proxy = None
    live_adapter = None
    if config.is_live and live_provider is not None and runtime is None:
        live_proxy, live_adapter = await _mount_cli_bridge(
            config, clock, gateway, live_provider, tools, tool_scopes,
        )
    if runtime is None:
        if config.is_live:
            from personal_agent.adapters.harness.runtime import LiveRuntime

            runtime = LiveRuntime(
                config, adapter=live_adapter,
                transport=transport if live_provider is not None else None,
                proxy=live_proxy,
            )
        else:
            runtime = MockRuntime(observations, clock)
            runtime.bind_model_gateway(gateway)

    store = BlobStore(config.artifacts_dir)
    writer = ManagedWriter(database, store, clock)
    artifacts = ArtifactService(database, writer, store, clock)
    budget = BudgetService(clock)
    evidence = EvidenceService(clock)
    capabilities = await runtime.capabilities()
    model_id = (
        "mock-deepseek-phase1"
        if not config.is_live
        else os.environ.get("PERSONAL_AGENT_MODEL", "deepseek-v4-flash")
    )
    capability_token = (
        "mock-capability-token" if not config.is_live else "unverified-live-token"
    )
    control = StopControl(database, clock)
    engine = WorkflowEngine(
        EngineDeps(
            database=database,
            clock=clock,
            runtime=runtime,
            tools=tools,
            artifacts=artifacts,
            budget=budget,
            evidence=evidence,
            policy=config.policy,
            model_id=model_id,
            capability_token=capability_token,
            scopes=tool_scopes,
            control=control,
            profile_hash=_profile_hash(config),
        )
    )
    runtime_fingerprint = stable_hash(capabilities.as_dict())
    config_fingerprint = stable_hash(config.policy.as_document())
    coordinator = ExecutionCoordinator(
        database,
        clock,
        gateway,
        budget,
        policy_hash=config.policy.policy_hash,
        policy_version=config.policy.version,
        budget_limits=config.policy.budget.as_dict(),
        runtime_fingerprint=runtime_fingerprint,
        config_fingerprint=config_fingerprint,
    )
    executor = RunExecutor(engine)
    recovery = RecoveryService(
        database,
        clock,
        gateway,
        store,
        runtime_fingerprint=runtime_fingerprint,
        config_fingerprint=config_fingerprint,
        policy_hash=config.policy.policy_hash,
        submit=executor.submit,
    )
    assistance = AssistanceService(database, clock, budget, control, recovery, writer)
    registry = WorkflowRegistry(database, clock)
    analyzer = BranchAnalyzer(database, clock, registry)
    branches = BranchService(
        database,
        clock,
        analyzer,
        registry,
        budget,
        gateway,
        config_fingerprint=config_fingerprint,
        runtime_fingerprint=runtime_fingerprint,
        budget_limits=config.policy.budget.as_dict(),
        draft_limits=config.policy.draft.as_dict(),
        submit=executor.submit,
    )
    async with database.session() as session:
        if await session.get(MaintenanceState, 1) is None:
            session.add(
                MaintenanceState(
                    singleton_id=1,
                    mode="NORMAL",
                    reason=None,
                    requested_at=None,
                    entered_at=None,
                    revision=1,
                )
            )
            await session.commit()
    # Retention is a startup policy: expired business content stops being readable
    # and old sessions/cache/logs are destroyed without waiting for a user action.
    await DataLifecycleService(
        database,
        clock,
        store,
        retention=config.policy.retention,
        data_dir=config.data_dir,
    ).apply_retention()
    unknown = await gateway.non_terminal_request()
    current_migration = await migration_current(database)
    expected_migration = migration_head()
    return AppContext(
        config=config,
        database=database,
        clock=clock,
        runtime=runtime,
        tools=tools,
        gateway=gateway,
        budget=budget,
        writer=writer,
        artifacts=artifacts,
        evidence=evidence,
        engine=engine,
        coordinator=coordinator,
        executor=executor,
        control=control,
        recovery=recovery,
        assistance=assistance,
        branches=branches,
        sessions=sessions or SessionManager(),
        origin_policy=origin_policy
        or OriginPolicy(config.host, config.port, config.dev_origin),
        execution_available=_execution_available(
            config, capabilities=capabilities.as_dict(),
            migration_version=current_migration, expected_migration=expected_migration,
        ),
        migration_current=current_migration,
        migration_head=expected_migration,
        runtime_capabilities=capabilities.as_dict(),
        unknown_model_request_id=unknown.id if unknown else None,
    )


class UnavailableModelTransport:
    """Permits startup diagnostics, never sends a request or supplies Mock output."""

    async def complete(self, spec: ModelRequest) -> ModelResponse:
        raise AppError(
            code="RUNTIME_INCOMPATIBLE", message="the live model bridge is not mounted",
            category=ErrorCategory.DEPENDENCY, status=503,
        )


def _profile_hash(config: config_module.AppConfig) -> str:
    if not config.is_live or config.profile_path is None:
        return "profile"
    return hashlib.sha256(config.profile_path.read_bytes()).hexdigest()


def _live_provider() -> Any:
    """The supplier client, or None when no provider credential is configured."""
    from personal_agent.adapters.harness.model_provider import AnthropicMessagesProvider

    try:
        return AnthropicMessagesProvider.from_environment()
    except AppError:
        return None


def _live_tools(
    config: config_module.AppConfig, clock: Clock, scopes: ToolScopeRegistry,
) -> ToolPort:
    """Real restricted handlers: Brave, httpx, Playwright, pure extraction."""
    import httpx

    from personal_agent.adapters.tools.browser_read import BrowserReader, PlaywrightBrowserDriver
    from personal_agent.adapters.tools.extract import ContentExtractor
    from personal_agent.adapters.tools.http_fetch import HttpFetcher
    from personal_agent.adapters.tools.mcp_service import ToolHandlers, ToolService
    from personal_agent.adapters.tools.search import BraveSearchTool
    from personal_agent.bootstrap.credentials import CredentialService

    credentials = CredentialService(clock)
    client = httpx.AsyncClient(timeout=config.policy.transport.http_read_seconds)
    return ToolService(
        ToolHandlers(
            search=BraveSearchTool(client, lambda: credentials.secret("search")),
            fetch=HttpFetcher(client),
            browser=BrowserReader(
                PlaywrightBrowserDriver(
                    action_timeout_seconds=config.policy.transport.browser_action_seconds,
                ),
                should_stop=lambda: False,
            ),
            extract=ContentExtractor(),
        ),
        scopes,
        clock,
    )


async def _mount_cli_bridge(
    config: config_module.AppConfig,
    clock: Clock,
    gateway: ModelGateway,
    provider: Any,
    tools: ToolPort,
    scopes: ToolScopeRegistry,
) -> tuple[Any, Any]:
    """Start the loopback proxy and the pinned CLI adapter for this process.

    Returns ``(None, None)`` when the installation, profile or built policy
    plugin is unavailable: live mode then stays disabled rather than falling
    back to a direct provider or Mock output.
    """
    from personal_agent.adapters.harness.adoption import configured_installation
    from personal_agent.adapters.harness.cli import HeadlessCli
    from personal_agent.adapters.harness.cli_runtime import CliRuntime
    from personal_agent.adapters.harness.model_proxy import ModelProxy, ProxyTokenRegistry
    from personal_agent.adapters.harness.patch import write_cli_patch

    installation = configured_installation()
    profile = config.profile_path
    plugin = BACKEND_ROOT.parent / "runtime-policy" / "dist" / "index.js"
    if installation is None or profile is None or not plugin.exists():
        return None, None
    tokens = ProxyTokenRegistry()
    proxy = ModelProxy(gateway, tokens, provider, clock, tools=tools)
    try:
        await proxy.start()
    except (OSError, RuntimeError):
        return None, None
    patch = write_cli_patch(
        config.runtime_dir / "restricted.patch.yml",
        policy_plugin=plugin,
        max_output_tokens=config.policy.model.max_output_tokens,
        tool_command=[sys.executable, "-m", "personal_agent.adapters.tools.mcp_stdio"],
    )
    adapter = CliRuntime(
        HeadlessCli(installation, patch, config.runtime_dir / "attempts"),
        clock,
        profile_hash=_profile_hash(config),
        tokens=tokens,
        gateway_base_url=proxy.base_url,
        profile_path=str(profile),
        scopes=scopes,
    )
    return proxy, adapter


def _transport_for(
    config: config_module.AppConfig, runtime: RuntimePort
) -> ModelTransport:
    if config.is_live:
        try:
            transport = getattr(runtime, "model_transport", None)
        except AppError as error:
            if error.code != "RUNTIME_INCOMPATIBLE":
                raise
            return UnavailableModelTransport()
        if transport is None:
            raise AppError(
                code="RUNTIME_INCOMPATIBLE",
                message="live mode requires a verified provider transport",
                category=ErrorCategory.DEPENDENCY, status=503,
            )
        return transport
    from personal_agent.adapters.harness.mock_runtime import MockModelTransport

    return MockModelTransport(_observations())


@asynccontextmanager
async def lifespan_for(config: config_module.AppConfig) -> AsyncIterator[AppContext]:
    context = await build_context(config)
    try:
        await mark_interrupted_runs(context.database, context.clock)
        yield context
    finally:
        close = getattr(context.runtime, "close", None)
        if close is not None:
            await close()
        await context.database.dispose()

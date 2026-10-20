"""Phase-1 delivery acceptance through the real runtime and service chain (T100).

This is the only module where the phase-1 product path may reach real providers.
It is marked ``live``; ``tests/conftest.py`` skips it unless both ``--live`` and
``PERSONAL_AGENT_LIVE=1`` are given. Real provider credentials, a recorded runtime
adoption gate (T021) and an explicit user cost scope are prerequisites. A missing
prerequisite fails the test instead of silently skipping or falling back to Mock:
Mock evidence is never a substitute for this gate.

When enabled, the module drives the application's own HTTP surface (the endpoints
the UI uses) for both seeded workflows and checks four delivery facts:

* the official Harness loop executed the agent steps (``LiveRuntime`` sessions and
  submissions backed by the pinned SDK, with adoption evidence recorded), not a
  Python re-implementation of the loop;
* every model request was admitted by the single ``ModelGateway`` (persistent
  request rows, verified completion evidence, no unpaired provider transport call,
  at most one transport call in flight);
* real public sources were fetched and recorded with their fetch time;
* a Markdown artifact exists, is served by the artifact endpoints and matches the
  managed blob on disk.

The outcome belongs in ``docs/live-acceptance.md``. A skipped or failed run keeps
that file at NOT RUN / FAILED; it never turns Mock output into live evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import ipaddress
import os
import socket
import tomllib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import func, select

from personal_agent.adapters.harness.model_gateway import ModelGateway
from personal_agent.adapters.harness.runtime import (
    ADOPTION_RECORD,
    LiveRuntime,
    adoption_evidence,
)
from personal_agent.adapters.persistence.artifact_models import Artifact, Blob
from personal_agent.adapters.persistence.budget_models import ModelRequest
from personal_agent.adapters.persistence.requirement_models import RequirementDraft
from personal_agent.adapters.persistence.research_models import Source
from personal_agent.adapters.persistence.workflow_models import WorkflowVersion
from personal_agent.api.app import create_app
from personal_agent.api.context import AppContext
from personal_agent.application.requirement_confirmation import RequirementService
from personal_agent.bootstrap.config import AppConfig, load_config
from personal_agent.bootstrap.lifespan import build_context
from personal_agent.bootstrap.seed import seed
from personal_agent.domain.common import AppError, new_id
from personal_agent.domain.workflow_schema import WorkflowDefinition
from personal_agent.ports.contracts import ModelRequest as ModelRequestSpec
from personal_agent.ports.contracts import (
    ModelResponse,
    RuntimeCapabilities,
    RuntimeTask,
    SessionContext,
    SessionHandle,
    Submission,
)

pytestmark = pytest.mark.live

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PINNED_SDK_PACKAGE = "deepseek-harness-sdk"

# Placeholder hosts from fixtures/examples must never be accepted as live evidence.
PLACEHOLDER_HOSTS = frozenset(
    {
        "example.org",
        "example.net",
        "example.com",
        "vendor.example",
        "localhost",
        "127.0.0.1",
        "::1",
    }
)
PLACEHOLDER_SUFFIXES = (".example", ".test", ".invalid", ".localhost")

# The real socket implementations, captured before the offline guard replaces them.
_REAL_SOCKET_CONNECT = socket.socket.connect
_REAL_GETADDRINFO = socket.getaddrinfo


@dataclass(frozen=True)
class LiveFlow:
    """One seeded workflow with concrete, publicly researchable input."""

    name: str
    inputs: dict[str, Any]
    output_step: str


PUBLIC_TOPIC_FLOW = LiveFlow(
    name="公开主题资料研究",
    inputs={
        "topic": "Rust 2024 edition 的语言变化",
        "recency": "2025 年以来的公开资料",
    },
    output_step="write_report",
)

TECHNOLOGY_FLOW = LiveFlow(
    name="技术选型对比",
    inputs={
        "topic": "FastAPI 与 Litestar 两个 Python ASGI 框架",
        "hard_constraints": ["必须支持 Python 3.12", "必须有官方维护的文档"],
    },
    output_step="write_comparison",
)


@dataclass
class LiveDelivery:
    """What a completed flow produced, used by the delivery assertions."""

    flow: LiveFlow
    run_id: str
    status: str
    started_at: datetime
    agent_steps: tuple[str, ...]


class RecordingRuntime:
    """Delegating proxy around the official adapter; it records, it never implements.

    Replacing the adapter with a local loop (or Mock) would make ``inner`` fail the
    ``isinstance`` check in ``_assert_official_loop`` and the session count check.
    """

    def __init__(self, inner: LiveRuntime) -> None:
        self.inner = inner
        self.sessions: list[tuple[SessionContext, SessionHandle]] = []
        self.submissions: list[tuple[RuntimeTask, Submission]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def capabilities(self) -> RuntimeCapabilities:
        return await self.inner.capabilities()

    async def create_session(self, context: SessionContext) -> SessionHandle:
        handle = await self.inner.create_session(context)
        self.sessions.append((context, handle))
        return handle

    async def submit(self, task: RuntimeTask) -> Submission:
        submission = await self.inner.submit(task)
        self.submissions.append((task, submission))
        return submission


class TransportSpy:
    """Wraps the gateway's provider transport and records every call."""

    def __init__(self, inner: Any, spy: GatewaySpy) -> None:
        self._inner = inner
        self._spy = spy

    async def complete(self, spec: ModelRequestSpec) -> ModelResponse:
        self._spy.transport_started(spec.request_id)
        try:
            return await self._inner.complete(spec)
        finally:
            self._spy.transport_finished(spec.request_id)


class GatewaySpy:
    """Records the gateway's admissions without changing its behaviour."""

    def __init__(self, gateway: ModelGateway) -> None:
        self._gateway = gateway
        self.requests: list[str] = []
        self.request_statuses: list[str] = []
        self.reservations: list[str] = []
        self.completions: list[str] = []
        self.transport_calls: list[str] = []
        self.unpaired_transport_calls: list[str] = []
        self._in_flight: set[str] = set()
        self._transport_in_flight: set[str] = set()
        self.in_flight_at_end: set[str] = set()
        self.max_transport_concurrency = 0

    def install(self) -> None:
        gateway = self._gateway
        original_request = gateway.request
        original_reserve = gateway.reserve
        original_complete = gateway.complete
        gateway._transport = TransportSpy(gateway._transport, self)

        async def reserve(spec: ModelRequestSpec, *, reserved_tokens: int) -> Any:
            reservation = await original_reserve(spec, reserved_tokens=reserved_tokens)
            self.reservations.append(reservation.request_id)
            return reservation

        async def request(
            spec: ModelRequestSpec,
            *,
            reserved_tokens: int,
            timeout_seconds: float,
        ) -> ModelResponse:
            self.requests.append(spec.request_id)
            self._in_flight.add(spec.request_id)
            try:
                response = await original_request(
                    spec,
                    reserved_tokens=reserved_tokens,
                    timeout_seconds=timeout_seconds,
                )
            finally:
                self._in_flight.discard(spec.request_id)
            self.request_statuses.append(response.status)
            return response

        async def complete(request_id: str, *, usage: Any, evidence_ref: str | None) -> None:
            self.completions.append(request_id)
            await original_complete(request_id, usage=usage, evidence_ref=evidence_ref)

        gateway.reserve = reserve  # type: ignore[method-assign]
        gateway.request = request  # type: ignore[method-assign]
        gateway.complete = complete  # type: ignore[method-assign]

    def transport_started(self, request_id: str) -> None:
        self.transport_calls.append(request_id)
        if request_id not in self.reservations:
            self.unpaired_transport_calls.append(request_id)
        self._transport_in_flight.add(request_id)
        self.max_transport_concurrency = max(
            self.max_transport_concurrency, len(self._transport_in_flight)
        )

    def transport_finished(self, request_id: str) -> None:
        self._transport_in_flight.discard(request_id)

    def finish(self) -> None:
        self.in_flight_at_end = set(self._in_flight)


@dataclass
class LiveChain:
    config: AppConfig
    context: AppContext
    client: httpx.AsyncClient
    runtime: RecordingRuntime
    gateway: GatewaySpy
    sdk_version: str


# ---------------------------------------------------------------------------
# Live gates
# ---------------------------------------------------------------------------


def _require_live_authorization(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--live"):
        pytest.fail(
            "live delivery acceptance requires the --live flag and explicit user authorization",
            pytrace=False,
        )
    if os.environ.get("PERSONAL_AGENT_LIVE") != "1":
        pytest.fail(
            "live delivery acceptance requires PERSONAL_AGENT_LIVE=1",
            pytrace=False,
        )
    if os.environ.get("PERSONAL_AGENT_RUNTIME_ADOPTED") != "1":
        pytest.fail(
            "bootstrap.run refuses live mode unless PERSONAL_AGENT_RUNTIME_ADOPTED=1; "
            "record the T021 adoption evidence first (docs/runtime-adoption.md)",
            pytrace=False,
        )


def _require_adoption_evidence() -> dict[str, Any]:
    evidence = adoption_evidence()
    if evidence is None:
        pytest.fail(
            "no adopted runtime evidence: docs/runtime-adoption.json must record "
            "adopted=true with every adoption check true. "
            f"adoption_evidence() reads {ADOPTION_RECORD}",
            pytrace=False,
        )
    return evidence


def _declared_live_sdk() -> str:
    pyproject = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declarations = pyproject.get("project", {}).get("optional-dependencies", {}).get("live", [])
    for item in declarations:
        if item.startswith(f"{PINNED_SDK_PACKAGE}=="):
            return str(item).split("==", 1)[1].strip()
    pytest.fail(
        f"pyproject.toml must pin {PINNED_SDK_PACKAGE} in the live extra",
        pytrace=False,
    )


def _require_pinned_sdk() -> str:
    declared = _declared_live_sdk()
    try:
        installed = version(PINNED_SDK_PACKAGE)
    except PackageNotFoundError:
        pytest.fail(
            f"{PINNED_SDK_PACKAGE}=={declared} is not installed; "
            "run: uv sync --project code/backend --python 3.12 --extra live",
            pytrace=False,
        )
    if installed != declared:
        pytest.fail(
            f"installed {PINNED_SDK_PACKAGE}=={installed} does not match the pinned {declared}",
            pytrace=False,
        )
    if importlib.util.find_spec("deepseek_harness") is None:
        pytest.fail(
            "the installed SDK does not expose the deepseek_harness package",
            pytrace=False,
        )
    return installed


@pytest.fixture(autouse=True)
def live_network_allowed(
    monkeypatch: pytest.MonkeyPatch, no_real_network: None
) -> None:
    """Lift the offline socket guard only for this explicitly authorized module.

    ``tests/conftest.py`` blocks non-loopback sockets for every default test. A live
    acceptance run is the documented exception, so restore the real implementations
    after the guard installed its replacements. The module is only executed when
    ``--live`` and ``PERSONAL_AGENT_LIVE=1`` are both present.
    """
    monkeypatch.setattr(socket.socket, "connect", _REAL_SOCKET_CONNECT)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)


# ---------------------------------------------------------------------------
# Live application
# ---------------------------------------------------------------------------


def _live_timeout_seconds() -> float:
    return float(os.environ.get("PERSONAL_AGENT_LIVE_TIMEOUT_SECONDS") or "900")


def _alembic_config(database_url: str) -> AlembicConfig:
    config = AlembicConfig(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture
async def live_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> AsyncIterator[LiveChain]:
    _require_live_authorization(request)
    _require_adoption_evidence()
    sdk_version = _require_pinned_sdk()

    config = load_config(mode="live", data_dir=tmp_path / "Personal-Agent")
    config.ensure_directories()
    database_url = f"sqlite+aiosqlite:///{config.db_path.as_posix()}"
    monkeypatch.setenv("PERSONAL_AGENT_DB_URL", database_url)
    await asyncio.to_thread(command.upgrade, _alembic_config(database_url), "head")

    runtime = RecordingRuntime(LiveRuntime(config))
    try:
        context = await build_context(config, runtime=runtime)
    except AppError as error:
        pytest.fail(
            f"the live service chain cannot start: {error.code}: {error.message}",
            pytrace=False,
        )

    assert context.migrations_up_to_date, (
        context.migration_current,
        context.migration_head,
    )
    assert context.execution_available, "the live chain reports execution_available=false"

    from personal_agent.adapters.harness.mock_runtime import MockModelTransport
    from personal_agent.adapters.tools.mock_service import UnavailableToolService

    if isinstance(context.gateway._transport, MockModelTransport):
        pytest.fail(
            "live mode still uses the Mock model transport; Mock is not live evidence",
            pytrace=False,
        )
    if isinstance(context.tools, UnavailableToolService):
        pytest.fail(
            "live mode still uses UnavailableToolService; real tool fetch is unavailable "
            "(see docs/live-acceptance.md §2)",
            pytrace=False,
        )

    gateway_spy = GatewaySpy(context.gateway)
    gateway_spy.install()
    await seed(context.database)

    app = create_app(config, context=context)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=f"http://127.0.0.1:{config.port}",
        ) as client:
            health = await client.get("/health")
            assert health.status_code == 200, health.text
            csrf = client.cookies.get("pa_csrf")
            assert csrf, "the local session bootstrap must issue a CSRF cookie"
            client.headers["x-csrf-token"] = csrf
            yield LiveChain(
                config=config,
                context=context,
                client=client,
                runtime=runtime,
                gateway=gateway_spy,
                sdk_version=sdk_version,
            )
    finally:
        gateway_spy.finish()
        await context.executor.shutdown()
        await context.database.dispose()


# ---------------------------------------------------------------------------
# Running a seeded workflow through the real endpoints
# ---------------------------------------------------------------------------


def _payload(response: httpx.Response, expected: int = 200) -> dict[str, Any]:
    assert response.status_code == expected, (
        f"{response.request.method} {response.request.url} returned "
        f"{response.status_code}: {response.text}"
    )
    body = response.json()
    assert isinstance(body, dict)
    return body


def _requirement_service(chain: LiveChain) -> RequirementService:
    policy = chain.config.policy
    return RequirementService(
        chain.context.database,
        chain.context.clock,
        chain.context.budget,
        policy_hash=policy.policy_hash,
        policy_version=policy.version,
        draft_limits=policy.draft.as_dict(),
    )


async def _record_user_fields(
    chain: LiveChain, draft_id: str, inputs: dict[str, Any]
) -> None:
    """Record the user's explicit topic/requirements on the draft.

    This stands in for the chat-extraction step (covered by T025/T044): the run-start,
    execution and delivery chain under test here is real, and the field snapshot is
    produced by the same ``RequirementService`` the UI uses, so every subsequent hash
    and confirmation is the real one.
    """
    service = _requirement_service(chain)
    accepted_at = chain.context.clock.now().isoformat().replace("+00:00", "Z")
    async with chain.context.database.session() as session:
        draft = await session.get(RequirementDraft, draft_id)
        assert draft is not None
        draft.field_values_json = {
            path: {
                "path": path,
                "value": value,
                "source": "user",
                "message_id": None,
                "suggestion_id": None,
                "accepted_at": accepted_at,
                "explicit_none": False,
            }
            for path, value in inputs.items()
        }
        draft.revision += 1
        workflow_version = await session.get(WorkflowVersion, draft.selected_version_id)
        assert workflow_version is not None
        await service.recompute(
            session,
            draft,
            WorkflowDefinition.model_validate(workflow_version.definition_json),
        )
        await session.commit()


async def _run_flow(chain: LiveChain, flow: LiveFlow) -> LiveDelivery:
    workflows = _payload(await chain.client.get("/api/v1/workflows"))["items"]
    summary = next((item for item in workflows if item.get("name") == flow.name), None)
    assert summary is not None, f"seeded workflow {flow.name!r} is missing"
    version_id = summary["latest_version"]["id"]
    version_payload = _payload(
        await chain.client.get(f"/api/v1/workflows/{summary['id']}/versions/{version_id}")
    )
    agent_steps = tuple(
        str(step["id"])
        for step in version_payload["definition"]["steps"]
        if step.get("type") == "AgentTask"
    )
    assert agent_steps, f"{flow.name!r} has no AgentTask step to exercise the loop"

    draft = _payload(
        await chain.client.post(
            "/api/v1/requirements",
            json={"workflow_version_id": version_id},
            headers={"Idempotency-Key": new_id()},
        ),
        expected=201,
    )
    await _record_user_fields(chain, draft["id"], flow.inputs)

    snapshot = _payload(await chain.client.get(f"/api/v1/requirements/{draft['id']}"))
    assert snapshot["missing_fields"] == [], snapshot["missing_fields"]
    assert snapshot["status"] == "READY", snapshot["status"]

    confirmation = _payload(
        await chain.client.post(
            f"/api/v1/requirements/{draft['id']}/confirm",
            json={
                "draft_revision": snapshot["revision"],
                "summary_hash": snapshot["summary_hash"],
                "workflow_version_id": version_id,
                "budget_policy_hash": chain.config.policy.policy_hash,
                "data_policy_version": chain.config.policy.version,
                "action": "START",
                "explicit_start_confirmation": True,
            },
            headers={
                "Idempotency-Key": new_id(),
                "If-Match": f'"{snapshot["revision"]}"',
            },
        ),
        expected=201,
    )

    started_at = chain.context.clock.now()
    run = _payload(
        await chain.client.post(
            "/api/v1/runs",
            json={"confirmation_id": confirmation["confirmation_id"]},
            headers={"Idempotency-Key": new_id()},
        ),
        expected=201,
    )
    run_id = str(run["id"])
    status = await chain.context.executor.wait(run_id, max_wait=_live_timeout_seconds())
    return LiveDelivery(
        flow=flow,
        run_id=run_id,
        status=str(status),
        started_at=started_at,
        agent_steps=agent_steps,
    )


# ---------------------------------------------------------------------------
# Delivery assertions
# ---------------------------------------------------------------------------


def _is_public_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host in PLACEHOLDER_HOSTS:
        return False
    if any(host.endswith(suffix) for suffix in PLACEHOLDER_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." in host
    return address.is_global


async def _assert_official_loop(chain: LiveChain, delivery: LiveDelivery) -> None:
    from personal_agent.adapters.harness.mock_runtime import MockRuntime

    inner = chain.runtime.inner
    assert isinstance(inner, LiveRuntime), (
        "the run was not executed by the live Harness adapter"
    )
    assert not isinstance(inner, MockRuntime), (
        "Mock must never stand in for the live acceptance run"
    )
    capabilities = await inner.capabilities()
    assert capabilities.adapter_version.startswith("harness"), capabilities.adapter_version
    assert capabilities.sdk_version == chain.sdk_version, capabilities.sdk_version
    assert capabilities.model_gateway_enforced is True
    assert capabilities.tool_policy_enforced is True
    assert capabilities.persistence_mode != "unknown"
    assert chain.context.config.is_live
    assert chain.context.runtime_capabilities["sdk_version"] == chain.sdk_version

    assert len(chain.runtime.sessions) >= len(delivery.agent_steps), (
        "fewer Harness sessions than AgentTask steps; a Python re-implementation "
        "of the loop may have run"
    )
    assert chain.runtime.submissions, "the Harness loop accepted no task"
    for _context, handle in chain.runtime.sessions:
        assert handle.adapter_session_id, "a Harness session needs an adapter id"
        assert handle.runtime_id, "a Harness session needs a runtime id"
    assert all(submission.status == "accepted" for _task, submission in chain.runtime.submissions)

    steps = _payload(await chain.client.get(f"/api/v1/runs/{delivery.run_id}/steps"))["items"]
    by_id = {str(step["step_id"]): step for step in steps}
    for step_id in delivery.agent_steps:
        assert step_id in by_id, f"AgentTask step {step_id!r} was never recorded"
        assert by_id[step_id]["status"] == "SUCCEEDED", (
            step_id,
            by_id[step_id]["status"],
        )
    assert by_id[delivery.flow.output_step]["status"] == "SUCCEEDED"


async def _assert_model_requests_managed(chain: LiveChain, run_id: str) -> None:
    async with chain.context.database.session() as session:
        rows = list(
            (
                await session.scalars(
                    select(ModelRequest).where(
                        ModelRequest.owner_type == "RUN",
                        ModelRequest.owner_id == run_id,
                    )
                )
            ).all()
        )
        non_terminal = int(
            await session.scalar(
                select(func.count())
                .select_from(ModelRequest)
                .where(ModelRequest.status.in_(("RESERVED", "IN_FLIGHT", "UNKNOWN")))
            )
            or 0
        )

    assert rows, (
        "no ModelGateway request was recorded for this run; the model path bypassed "
        "the single gateway"
    )
    request_ids = {row.id for row in rows}
    for row in rows:
        assert row.runtime_id == "live", (row.id, row.runtime_id)
        assert row.status == "COMPLETED", (row.id, row.status)
        assert row.completion_evidence_ref, (
            f"model request {row.id} completed without verifiable evidence"
        )
        assert row.ended_at is not None
        assert row.actual_tokens is not None

    spy = chain.gateway
    assert spy.requests, "the gateway never admitted a model request"
    assert len(spy.reservations) == len(spy.requests), (
        "a gateway admission did not persist a reservation",
        len(spy.requests),
        len(spy.reservations),
    )
    assert set(spy.reservations) == request_ids, (
        "gateway reservations and persisted requests disagree",
        sorted(set(spy.reservations) ^ request_ids),
    )
    assert set(spy.completions) == request_ids, "a completed row has no verified completion"
    assert set(spy.transport_calls) == request_ids, (
        "a provider call happened outside a gateway request",
        sorted(set(spy.transport_calls) ^ request_ids),
    )
    assert spy.unpaired_transport_calls == [], spy.unpaired_transport_calls
    assert spy.in_flight_at_end == set(), spy.in_flight_at_end
    assert spy.max_transport_concurrency <= 1, spy.max_transport_concurrency
    assert all(status == "completed" for status in spy.request_statuses)
    assert non_terminal == 0, "a model request stayed non-terminal after the run"


async def _assert_real_sources(chain: LiveChain, delivery: LiveDelivery) -> set[str]:
    async with chain.context.database.session() as session:
        sources = list(
            (
                await session.scalars(
                    select(Source).where(Source.run_id == delivery.run_id)
                )
            ).all()
        )
    assert sources, f"{delivery.flow.name} recorded no source; nothing was fetched"
    now = chain.context.clock.now()
    recorded_hosts: set[str] = set()
    for source in sources:
        assert _is_public_url(source.normalized_url), (
            f"placeholder or non-public source URL recorded: {source.normalized_url}"
        )
        assert source.title.strip(), f"source {source.normalized_url} has no title"
        assert source.fetched_at is not None
        assert delivery.started_at - timedelta(seconds=5) <= source.fetched_at <= now + timedelta(
            seconds=5
        ), (source.normalized_url, source.fetched_at)
        assert source.availability == "AVAILABLE", (
            source.normalized_url,
            source.availability,
        )
        assert source.content_hash, (
            f"source {source.normalized_url} has no content hash; it cannot be proven fetched"
        )
        recorded_hosts.add(str(urlparse(source.normalized_url).hostname or "").lower())
    return recorded_hosts


async def _assert_markdown_artifact(
    chain: LiveChain, delivery: LiveDelivery, recorded_hosts: set[str]
) -> None:
    snapshot = _payload(await chain.client.get(f"/api/v1/runs/{delivery.run_id}"))
    assert snapshot["status"] == "SUCCEEDED"
    assert snapshot["completeness"] in {"COMPLETE", "PARTIAL"}, snapshot["completeness"]

    listing = _payload(await chain.client.get(f"/api/v1/runs/{delivery.run_id}/artifacts"))
    markdown = [item for item in listing["items"] if item.get("format") == "markdown"]
    assert markdown, "the run delivered no Markdown artifact"
    artifact = markdown[0]
    assert artifact["access_state"] == "AVAILABLE", artifact["access_state"]
    assert artifact["size_bytes"] > 0
    assert artifact["id"] in snapshot["artifact_ids"]

    metadata = _payload(await chain.client.get(f"/api/v1/artifacts/{artifact['id']}"))
    assert metadata["access_state"] == "AVAILABLE"
    document = metadata["document"]
    assert document["title"].strip()
    assert document["directions"], "a delivered document must list every research direction"
    assert document["sources"], "a delivered document must reference its real sources"
    document_hosts: set[str] = set()
    for source in document["sources"]:
        url = str(source.get("normalized_url") or source.get("url") or "")
        assert _is_public_url(url), f"document source is not a public URL: {url}"
        document_hosts.add(str(urlparse(url).hostname or "").lower())
    assert document_hosts & recorded_hosts, (
        "the document references no host that was recorded as a fetched source",
        sorted(document_hosts),
        sorted(recorded_hosts),
    )

    response = await chain.client.get(f"/api/v1/artifacts/{artifact['id']}/content")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/markdown")
    raw = response.content
    assert len(raw) >= 200, len(raw)
    assert hashlib.sha256(raw).hexdigest() == artifact["sha256"]
    body = raw.decode("utf-8")
    assert body.startswith("# ")
    assert "完成程度：" in body

    async with chain.context.database.session() as session:
        row = await session.get(Artifact, artifact["id"])
        assert row is not None
        blob = await session.get(Blob, row.blob_id)
        assert blob is not None
        managed_path = chain.config.artifacts_dir / blob.managed_relative_path
    assert managed_path.is_file(), managed_path
    assert hashlib.sha256(managed_path.read_bytes()).hexdigest() == artifact["sha256"]


async def _assert_delivery(chain: LiveChain, delivery: LiveDelivery) -> None:
    assert delivery.status == "SUCCEEDED", (
        f"{delivery.flow.name} ended with {delivery.status} instead of SUCCEEDED"
    )
    snapshot = _payload(await chain.client.get(f"/api/v1/runs/{delivery.run_id}"))
    assert snapshot["status"] == "SUCCEEDED"
    await _assert_official_loop(chain, delivery)
    await _assert_model_requests_managed(chain, delivery.run_id)
    recorded_hosts = await _assert_real_sources(chain, delivery)
    await _assert_markdown_artifact(chain, delivery, recorded_hosts)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_live_delivery_prerequisites_are_met(request: pytest.FixtureRequest) -> None:
    """Fail fast with a precise reason; never skip a live gate that was authorized."""
    _require_live_authorization(request)
    evidence = _require_adoption_evidence()
    assert evidence.get("checks", {}).get("gateway_no_bypass") is True
    _require_pinned_sdk()


async def test_public_topic_workflow_delivers_real_sources_and_markdown(
    live_chain: LiveChain,
) -> None:
    delivery = await _run_flow(live_chain, PUBLIC_TOPIC_FLOW)
    await _assert_delivery(live_chain, delivery)


async def test_technology_comparison_workflow_delivers_real_sources_and_markdown(
    live_chain: LiveChain,
) -> None:
    delivery = await _run_flow(live_chain, TECHNOLOGY_FLOW)
    await _assert_delivery(live_chain, delivery)

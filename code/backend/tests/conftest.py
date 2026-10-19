"""Offline-by-default test environment.

Every default test replays fixed observations from ``tests/fixtures/observations.json``
and a frozen clock, and cannot open a real network connection. Real provider or
network calls belong to ``tests/live`` and require both ``--live`` and
``PERSONAL_AGENT_LIVE=1`` plus real credentials; nothing here may silently reach a
provider.
"""

from __future__ import annotations

import json
import os
import random
import socket
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from personal_agent.adapters.harness.mock_runtime import MockModelTransport, MockRuntime
    from personal_agent.adapters.harness.model_gateway import ModelGateway
    from personal_agent.adapters.persistence.database import Database

FIXTURE_DIR = Path(__file__).parent / "fixtures"
OBSERVATIONS_PATH = FIXTURE_DIR / "observations.json"

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="enable tests marked live; requires PERSONAL_AGENT_LIVE=1 and real credentials",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    live_enabled = bool(config.getoption("--live")) and os.environ.get("PERSONAL_AGENT_LIVE") == "1"
    if live_enabled:
        return
    skip = pytest.mark.skip(
        reason=(
            "live test: run with --live and PERSONAL_AGENT_LIVE=1 "
            "after explicit user authorization"
        )
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject any socket connection or DNS lookup outside loopback."""

    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        host = ""
        if isinstance(address, tuple) and address:
            host = str(address[0])
        elif isinstance(address, str):
            host = address
        if host not in LOOPBACK_HOSTS:
            raise ConnectionRefusedError(f"network access to {host!r} is disabled in default tests")
        return real_connect(self, address)

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if str(host) not in LOOPBACK_HOSTS:
            raise socket.gaierror(f"DNS lookup for {host!r} is disabled in default tests")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


def load_observations() -> dict[str, Any]:
    with OBSERVATIONS_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


@dataclass
class FrozenClock:
    """Deterministic clock; ``advance`` is the only way time moves."""

    current: datetime
    elapsed_ms: int = 0

    def now(self) -> datetime:
        return self.current

    def now_iso(self) -> str:
        return self.current.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def monotonic_ms(self) -> int:
        return self.elapsed_ms

    def advance(self, *, seconds: float = 0.0, milliseconds: int = 0) -> datetime:
        delta = timedelta(seconds=seconds, milliseconds=milliseconds)
        self.current = self.current + delta
        self.elapsed_ms += int(delta.total_seconds() * 1000)
        return self.current

    def reset(self, moment: datetime | str) -> None:
        self.current = _parse_utc(moment)
        self.elapsed_ms = 0


def _parse_utc(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = value.replace("Z", "+00:00")
    return datetime.fromisoformat(text).astimezone(UTC)


@dataclass
class ScriptedModel:
    """Replays a named model observation instead of calling a provider."""

    data: dict[str, Any]
    calls: list[str] = field(default_factory=list)

    def scenario(self, name: str) -> dict[str, Any]:
        try:
            value = self.data["model"]["scenarios"][name]
        except KeyError as exc:  # pragma: no cover - deterministic fixture bug
            raise KeyError(f"missing model observation {name!r}") from exc
        self.calls.append(name)
        return json.loads(json.dumps(value))


@pytest.fixture(scope="session")
def observations() -> dict[str, Any]:
    return load_observations()


@pytest.fixture
def clock(observations: dict[str, Any]) -> FrozenClock:
    return FrozenClock(_parse_utc(observations["clock"]["now"]))


@pytest.fixture
def rng(observations: dict[str, Any]) -> random.Random:
    return random.Random(observations["clock"]["random_seed"])


@pytest.fixture
def scripted_model(observations: dict[str, Any]) -> ScriptedModel:
    return ScriptedModel(observations)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A disposable workspace with the same layout as the real data directory."""
    root = tmp_path / "Personal-Agent"
    for name in ("db", "artifacts", "cache", "runtime", "logs", "backups"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def workspace(data_dir: Path) -> Path:
    """Alias for the temporary data directory used by persistence adapters."""
    return data_dir


@pytest.fixture
def ids() -> Iterator[uuid.UUID]:
    """Deterministic UUID stream so snapshots stay comparable across runs."""
    seeded = random.Random(424242)
    counter = 0
    while True:
        counter += 1
        yield uuid.UUID(int=(seeded.getrandbits(128) ^ counter))


@pytest.fixture
async def database(data_dir: Path) -> AsyncIterator[Database]:
    """A disposable database created from the mapped metadata.

    Unit and contract tests create the schema directly from metadata; the
    migration test separately proves that the Alembic chain reaches the same
    shape.
    """
    from personal_agent.adapters.persistence import models  # noqa: F401  (metadata registry)
    from personal_agent.adapters.persistence.base import Base
    from personal_agent.adapters.persistence.database import Database

    db = Database(data_dir / "db" / "test.db")
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
def mock_runtime(observations: dict[str, Any], clock: FrozenClock) -> MockRuntime:
    from personal_agent.adapters.harness.mock_runtime import MockRuntime

    return MockRuntime(observations, clock)


@pytest.fixture
def mock_transport(observations: dict[str, Any]) -> MockModelTransport:
    from personal_agent.adapters.harness.mock_runtime import MockModelTransport

    return MockModelTransport(observations)


@pytest.fixture
def gateway(
    database: Database, clock: FrozenClock, mock_transport: MockModelTransport
) -> ModelGateway:
    from personal_agent.adapters.harness.model_gateway import ModelGateway

    return ModelGateway(database, clock, mock_transport)


@pytest.fixture
async def api_context(data_dir: Path, database: Any, clock: Any) -> Any:
    """A fully built application context on the disposable database."""
    from personal_agent.api.context import AppContext
    from personal_agent.bootstrap.config import load_config
    from personal_agent.bootstrap.lifespan import build_context

    config = load_config(mode="mock", data_dir=data_dir)
    context: AppContext = await build_context(config, database=database, clock=clock)
    return config, context


@pytest.fixture
async def client(api_context: Any) -> AsyncIterator[Any]:
    """HTTP client with a bootstrapped local session and CSRF header."""
    import httpx

    from personal_agent.api.app import create_app

    config, context = api_context
    app = create_app(config, context=context)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=f"http://127.0.0.1:{config.port}"
    ) as http_client:
        response = await http_client.get("/health")
        assert response.status_code == 200, response.text
        csrf = http_client.cookies.get("pa_csrf")
        assert csrf, "the local session bootstrap must issue a CSRF cookie"
        http_client.headers["x-csrf-token"] = csrf
        yield http_client


WORKFLOW_UUID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def workflow_definition() -> dict[str, Any]:
    """A minimal definition that passes every semantic rule."""
    import copy

    definition = {
        "schema_version": "1",
        "workflow_id": WORKFLOW_UUID,
        "version": 1,
        "name": "技术选型对比",
        "description": "比较具体软件产品的官方能力与运维限制。",
        "applicability": "需要比较两个以上具体软件产品或服务时使用。",
        "input_schema": {
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
        },
        "output_schema": {"type": "object"},
        "entry_step": "collect",
        "directions": [
            {"id": "official", "title": "官方能力", "scope": "官方文档与发布说明"},
            {"id": "ops", "title": "运维限制", "scope": "部署与运维成本"},
        ],
        "steps": [
            {
                "id": "collect",
                "type": "AgentTask",
                "name": "收集证据",
                "input_schema": {},
                "output_schema": {},
                "bindings": {},
                "input_reads": ["/topic"],
                "depends_on": [],
                "policy": {
                    "allowed_tools": ["search.web", "http.fetch"],
                    "allowed_domains": ["example.org", "vendor.example"],
                    "max_attempts": 2,
                    "timeout_seconds": 60,
                    "side_effect_mode": "none",
                },
                "config": {
                    "goal_template": "研究 {topic} 的官方能力与运维限制",
                    "context_bindings": [],
                    "direction_ids": ["official", "ops"],
                },
            },
            {
                "id": "check",
                "type": "Condition",
                "name": "检查缺口",
                "input_schema": {},
                "output_schema": {},
                "bindings": {},
                "input_reads": [],
                "depends_on": ["collect"],
                "policy": {},
                "config": {
                    "expression": {
                        "op": "eq",
                        "left": {"literal": "ready"},
                        "right": {"literal": "ready"},
                    }
                },
            },
            {
                "id": "review",
                "type": "HumanReview",
                "name": "缺关键条件时求助",
                "input_schema": {},
                "output_schema": {},
                "bindings": {},
                "input_reads": [],
                "depends_on": ["collect"],
                "policy": {},
                "config": {
                    "prompt": "缺少关键条件，是否继续？",
                    "decision_schema": {},
                    "reason_code": "MISSING_KEY_CONDITION",
                },
            },
            {
                "id": "write",
                "type": "Output",
                "name": "生成对比报告",
                "input_schema": {},
                "output_schema": {},
                "bindings": {},
                "input_reads": [],
                "depends_on": ["check"],
                "policy": {},
                "config": {
                    "renderer": "markdown",
                    "structure": "comparison",
                    "content_bindings": [],
                },
            },
        ],
        "transition_rules": [
            {"from": "collect", "to": "check", "on": "success"},
            {"from": "collect", "to": "review", "on": "exhausted"},
            {"from": "check", "to": "write", "on": "true"},
            {"from": "check", "to": "review", "on": "false"},
            {"from": "review", "to": "write", "on": "success"},
        ],
        "execution_policy": {
            "max_active_seconds": 1200,
            "max_model_requests": 30,
            "max_tool_calls": 60,
            "max_search_calls": 20,
            "max_page_reads": 40,
            "max_tokens": 100000,
            "max_attempts_per_step": 3,
            "model_timeout_seconds": 120,
            "tool_timeout_seconds": 120,
        },
        "completion_policy": {
            "required_output_step_ids": ["write"],
            "required_direction_ids": ["official", "ops"],
            "allow_partial_document": True,
        },
    }
    return copy.deepcopy(definition)

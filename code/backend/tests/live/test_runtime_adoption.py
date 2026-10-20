"""Executable pinned-package adoption checks, independent of an existing pass.

These tests use the real installed CLI/Loop/Cordis with fixed local model
observations. They send NO paid provider request. The production bridge check
must fail while its wiring is absent; fixed observations cannot establish
supplier adoption. No adopted=true record is written by this partial gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from personal_agent.adapters.harness.adoption import configured_installation
from personal_agent.adapters.harness.cli import (
    CLI_VERSION,
    CliInstallation,
    HeadlessCli,
    launch_environment,
)
from personal_agent.adapters.harness.cli_runtime import CliRuntime
from personal_agent.adapters.harness.model_gateway import ModelGateway
from personal_agent.adapters.harness.runtime import LiveRuntime
from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.tools.mcp_service import ToolService
from personal_agent.bootstrap.config import load_config
from personal_agent.bootstrap.lifespan import build_context
from personal_agent.domain.common import AppError, new_id
from personal_agent.ports.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    Owner,
    RuntimeTask,
    SessionContext,
)

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[4]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def adoption_report(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    report: dict[str, Any] = {
        "schema_version": 2, "carrier": "headless-cli", "adopted": False,
        "verification_kind": "installed-runtime-with-fixed-model-observations",
        "provider_requests": 0, "new_paid_cost_cny": 0,
        "prior_budget_occupied_cny": 2.013242, "authorized_total_cny": 25,
        "checks": {}, "raw_output_saved": False,
    }
    yield report
    # Evidence is a reviewable output; tests never mutate production adoption
    # state or overwrite repository documents. Promote the allowlisted report
    # to docs/audits separately after reviewing this run's result.
    target = tmp_path_factory.mktemp("cli-adoption") / "candidate.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"adoption_report": str(target), "adopted": False}))


@pytest.fixture
def installation(adoption_report: dict[str, Any]) -> CliInstallation:
    installed = configured_installation()
    assert installed is not None, (
        "set PERSONAL_AGENT_DSH_CLI_PATH to the installed 0.2.1-rc.2 CLI; "
        "no npx download or SDK fallback"
    )
    assert os.name == "nt", "this adoption gate targets Windows"
    adoption_report["cli_fingerprint"] = installed.fingerprint
    adoption_report["profile_sha256"] = hashlib.sha256(
        (ROOT / "code/runtime-policy/profile.json").read_bytes()
    ).hexdigest()
    adoption_report["versions"] = {
        "sdk_version": "not-used", "runtime_version": CLI_VERSION, "platform": "windows-x64",
    }
    return installed


def runner(installation: CliInstallation, root: Path, *, delay_ms: int = 0) -> HeadlessCli:
    root.mkdir(parents=True, exist_ok=True)
    patch = root / "offline.patch.yml"
    original = (FIXTURES / "headless-smoke.patch.yml").read_text(encoding="utf-8")
    module_path = (FIXTURES / "loop-observation.mjs").resolve().as_posix()
    # Last patch row wins. Managed credentials are explicitly disabled; the
    # subprocess environment is allowlisted and its home is empty.
    patch.write_text(
        original + "\n- id: credentials\n  disabled: true\n- insert:\n"
        "    - id: offline-loop-observation\n      name: " + json.dumps(module_path) +
        "\n      config:\n        delayMs: " + str(delay_ms) + "\n",
        encoding="utf-8",
    )
    return HeadlessCli(installation, patch, root / "managed")


async def test_real_windows_cli_and_official_loop_protocol(
    tmp_path: Path, installation: CliInstallation, clock: Any,
    adoption_report: dict[str, Any],
) -> None:
    checks = adoption_report["checks"]
    cli = runner(installation, tmp_path)
    adapter = CliRuntime(cli, clock, profile_hash="candidate-profile")
    session = await adapter.create_session(SessionContext(
        new_id(), Owner(run_id=new_id(), attempt_id=new_id()), 1,
        "deepseek-flash", "candidate-profile", "local-fixture-scope", "policy",
    ))
    submission = await adapter.submit(RuntimeTask(
        new_id(), session.adapter_session_id, 1, "fixed harmless local observation", (), {},
        {"type": "object", "required": ["answer"],
         "properties": {"answer": {"const": "CLI_OFFLINE_OK"}}, "additionalProperties": False},
        clock.now() + timedelta(seconds=30), (), "fixture-reservation",
    ))
    result = await adapter.wait_result(submission.submission_id)
    checks["windows_start"] = result.completion_evidence_ref is not None
    checks["cli_protocol"] = (
        result.status == "completed" and result.structured_output == {"answer": "CLI_OFFLINE_OK"}
        and result.usage.tokens == 15 and result.usage.model_requests == 1
    )
    normalized = [event async for event in adapter.events(session.adapter_session_id)]
    checks["events_redacted"] = "PRIVATE_ADOPTION_THOUGHT" not in repr(normalized)
    try:
        await adapter.restore_session(session, {})
    except AppError as error:
        checks["restore_missing_session_fails"] = error.code == "SESSION_MISSING"
    else:
        checks["restore_missing_session_fails"] = False
    # Run the process primitive too, so its real exit code and pre-cleanup
    # persistence inspection are checked directly instead of inferred.
    observed = await cli.run("fixed harmless local observation", timeout_seconds=30)
    checks["cli_exit_code"] = observed.exit_code == 0 and observed.status == "completed"
    checks["no_reasoning_persisted"] = observed.durable_session_logs == 0
    assert all(checks[name] for name in (
        "windows_start", "cli_protocol", "cli_exit_code", "events_redacted",
        "no_reasoning_persisted", "restore_missing_session_fails",
    )), {"status": result.status, "error_code": result.error.code if result.error else None}


def test_real_scope_late_registration_is_denied(
    installation: CliInstallation, adoption_report: dict[str, Any],
) -> None:
    process = subprocess.run(
        [str(installation.node), str(FIXTURES / "guard-adoption.mjs"), str(installation.entry),
         str(ROOT / "code/runtime-policy/dist/index.js"),
         str(ROOT / "code/runtime-policy/profile.json")],
        capture_output=True, timeout=15, check=False, env=launch_environment(),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    # Arbitrary stderr is never printed or saved.
    try:
        summary = json.loads(process.stdout)
    except ValueError:
        summary = {}
    passed = (
        process.returncode == 0 and summary.get("tool_scope_denied") is True
        and summary.get("allowed_tool_executed") is True
        and summary.get("same_scope_registered_after_guard") is True
    )
    adoption_report["checks"]["tool_scope_denied"] = passed
    assert passed, "installed Cordis/tool service rejected the scoped policy probe"


async def test_real_cli_gateway_completion_and_unknown_survive_restart(
    tmp_path: Path, installation: CliInstallation, database: Any, clock: Any,
    adoption_report: dict[str, Any],
) -> None:
    class FixtureCliTransport:
        def __init__(self, cli: HeadlessCli) -> None:
            self.cli = cli

        async def complete(self, spec: ModelRequest) -> ModelResponse:
            observed = await self.cli.run("fixed local gateway observation", timeout_seconds=30)
            return ModelResponse(
                observed.status, observed.text, observed.usage,
                completion_evidence_ref=(
                    f"{spec.request_id}:{observed.completion_evidence_ref}"
                    if observed.completion_evidence_ref else None
                ),
            )

    transport = FixtureCliTransport(runner(installation, tmp_path / "completed"))
    gateway = ModelGateway(database, clock, transport)
    spec = ModelRequest(
        new_id(), Owner(draft_id=new_id()), 1, "deepseek-flash", [ModelMessage("user", "fixed")],
        32, clock.now() + timedelta(seconds=30), "fixture-reservation",
    )
    result = await gateway.request(spec, reserved_tokens=128, timeout_seconds=30)
    adoption_report["checks"]["trusted_completion_unlocks"] = (
        result.status == "completed" and result.completion_evidence_ref is not None
        and await gateway.non_terminal_request() is None
    )
    # Let the pinned CLI reach its local fixed-observation stream, then time it out.
    slow = FixtureCliTransport(runner(installation, tmp_path / "unknown", delay_ms=10000))
    gateway = ModelGateway(database, clock, slow)
    from dataclasses import replace

    unknown_spec = replace(spec, request_id=new_id())
    unknown = await gateway.request(unknown_spec, reserved_tokens=128, timeout_seconds=0.1)
    restarted_db = Database(database.path)
    try:
        restarted = ModelGateway(restarted_db, clock, transport)
        blocked = False
        try:
            await restarted.request(replace(spec, request_id=new_id()),
                                    reserved_tokens=128, timeout_seconds=30)
        except AppError as error:
            blocked = error.code == "MODEL_OUTCOME_UNKNOWN"
        state = await restarted.reconcile(unknown_spec.request_id, {"completed": True})
        adoption_report["checks"]["unknown_blocks"] = (
            unknown.status == "unknown" and blocked and state.value == "UNKNOWN"
        )
    finally:
        await restarted_db.dispose()
    assert adoption_report["checks"]["trusted_completion_unlocks"]
    assert adoption_report["checks"]["unknown_blocks"]


async def test_production_gateway_and_managed_tool_bindings(
    tmp_path: Path, installation: CliInstallation, database: Any, clock: Any,
    adoption_report: dict[str, Any],
) -> None:
    config = load_config(mode="live", data_dir=tmp_path)
    app = await build_context(config, database=database, clock=clock)
    capabilities = await app.runtime.capabilities()
    checks = adoption_report["checks"]
    checks["gateway_binding_mounted"] = (
        isinstance(app.runtime, LiveRuntime) and capabilities.model_gateway_enforced
    )
    checks["managed_tools_mounted"] = isinstance(app.tools, ToolService)
    # Claiming no bypass requires the actual production boundary first. A fixed
    # observation sent through a test transport is not that production boundary.
    checks["gateway_no_bypass"] = None  # NOT RUN: mounted production boundary is a prerequisite.
    adoption_report["blockers"] = [
        "production CLI llm/stream provider-to-ModelGateway bridge is not mounted",
        "production managed tool/MCP handlers and retrievable bodies are not mounted",
        "all-model no-bypass acceptance must run against those mounted boundaries",
    ]
    assert checks["gateway_binding_mounted"] and checks["managed_tools_mounted"], (
        "T121 adoption blocked: production provider/ModelGateway and managed tool/MCP "
        "bindings are absent; installed CLI, fixed observations, "
        "or an existing record cannot adopt them"
    )

"""Stale SDK evidence and environment flags cannot open the CLI execution gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from personal_agent.adapters.harness import runtime as module
from personal_agent.bootstrap.config import load_config
from personal_agent.bootstrap.lifespan import _execution_available, build_context
from personal_agent.domain.common import AppError


def record(profile: Path) -> dict[str, Any]:
    return {
        "schema_version": 2, "carrier": "headless-cli", "adopted": True,
        "versions": {"sdk_version": "not-used", "runtime_version": "0.2.1-rc.2",
                     "platform": "windows-x64"},
        "profile_sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
        "cli_fingerprint": "installation-fingerprint",
        "checks": dict.fromkeys(module.REQUIRED_CLI_CHECKS, True),
    }


def test_record_is_at_repository_docs() -> None:
    expected = Path(__file__).resolve().parents[4] / "docs/runtime-adoption.json"
    assert module.ADOPTION_RECORD == expected


@pytest.mark.parametrize("change", [
    "legacy", "version", "sdk", "profile", "installation", "missing_check", "scalar",
])
def test_only_current_cli_and_profile_evidence_is_accepted(
    tmp_path: Path, change: str,
) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text('{}')
    path = tmp_path / "evidence.json"
    data: Any = record(profile)
    path.write_text(json.dumps(data))
    assert module.adoption_evidence(
        record_path=path, profile_path=profile, cli_fingerprint="installation-fingerprint",
    )
    if change == "legacy":
        data.pop("schema_version")
        data["checks"]["wheel_interfaces"] = True
    elif change == "version":
        data["versions"]["runtime_version"] = "0.2.1-rc.3"
    elif change == "sdk":
        data["versions"]["sdk_version"] = "0.2.1rc2"
    elif change == "profile":
        profile.write_text('{"changed":true}')
    elif change == "installation":
        data["cli_fingerprint"] = "other"
    elif change == "missing_check":
        data["checks"].pop("gateway_no_bypass")
    elif change == "scalar":
        data = True
    path.write_text(json.dumps(data))
    assert module.adoption_evidence(
        record_path=path, profile_path=profile, cli_fingerprint="installation-fingerprint",
    ) is None


def test_environment_flag_alone_never_enables_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSONAL_AGENT_RUNTIME_ADOPTED", "1")
    monkeypatch.setattr(module, "ADOPTION_RECORD", tmp_path / "absent.json")
    assert _execution_available(load_config(mode="live", data_dir=tmp_path)) is False


async def test_unadopted_live_health_builds_without_mock_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: Any, clock: Any,
) -> None:
    monkeypatch.setenv("PERSONAL_AGENT_RUNTIME_ADOPTED", "1")
    monkeypatch.setattr(module, "ADOPTION_RECORD", tmp_path / "absent.json")
    config = load_config(mode="live", data_dir=tmp_path)
    app = await build_context(config, database=database, clock=clock)
    assert app.execution_available is False
    assert app.runtime_capabilities["sdk_version"] == "not-used"
    assert app.runtime_capabilities["model_gateway_enforced"] is False
    with pytest.raises(AppError):
        await app.runtime.create_session(None)  # type: ignore[arg-type]
    assert await app.gateway.non_terminal_request() is None


def test_evidence_alone_does_not_replace_mounted_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "adoption_evidence", lambda **_: {"adopted": True})
    assert _execution_available(load_config(mode="live", data_dir=tmp_path)) is False

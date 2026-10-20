"""Versioned, deployment-bound CLI evidence; no legacy wheel auto-migration."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from personal_agent.adapters.harness.cli import CLI_VERSION, CliInstallation
from personal_agent.domain.common import AppError

REPOSITORY_ROOT = Path(__file__).resolve().parents[6]
ADOPTION_RECORD = REPOSITORY_ROOT / "docs" / "runtime-adoption.json"
PROFILE_PATH = REPOSITORY_ROOT / "code" / "runtime-policy" / "profile.json"
REQUIRED_CLI_CHECKS = frozenset({
    "windows_start", "cli_protocol", "cli_exit_code", "tool_scope_denied",
    "gateway_no_bypass", "events_redacted", "no_reasoning_persisted",
    "unknown_blocks", "trusted_completion_unlocks", "restore_missing_session_fails",
    "gateway_binding_mounted", "managed_tools_mounted",
})


def configured_installation() -> CliInstallation | None:
    entry = os.environ.get("PERSONAL_AGENT_DSH_CLI_PATH")
    node = os.environ.get("PERSONAL_AGENT_NODE_PATH") or shutil.which("node")
    if not entry or not node:
        return None
    try:
        return CliInstallation.verify(Path(node), Path(entry))
    except AppError:
        return None


def read_adoption_evidence(
    *, record_path: Path, profile_path: Path, cli_fingerprint: str | None,
) -> dict[str, Any] | None:
    if cli_fingerprint is None:
        return None
    try:
        data = json.loads(record_path.read_text(encoding="utf-8"))
        profile_hash = hashlib.sha256(profile_path.read_bytes()).hexdigest()
        if not isinstance(data, dict):
            return None
        if (data.get("adopted") is not True or data.get("schema_version") != 2
                or data.get("carrier") != "headless-cli"):
            return None
        versions = data.get("versions")
        if not isinstance(versions, dict) or versions != {
            "sdk_version": "not-used", "runtime_version": CLI_VERSION, "platform": "windows-x64",
        }:
            return None
        if (data.get("profile_sha256") != profile_hash
                or data.get("cli_fingerprint") != cli_fingerprint):
            return None
        checks = data.get("checks")
        if not isinstance(checks, dict) or not all(
            checks.get(name) is True for name in REQUIRED_CLI_CHECKS
        ):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None

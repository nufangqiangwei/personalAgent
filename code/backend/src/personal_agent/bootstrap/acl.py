"""Windows single-user ACL protection and self-check.

The data directory (database, artifacts, logs, sessions, backups) must be
readable only by the current user, SYSTEM and Administrators. The check reads the
effective ACL, not a writability probe: inherited ACEs from a parent directory or
an extra principal are reported as a diagnosable failure and block startup.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from personal_agent.domain.common import AppError, ErrorCategory

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
# Windows adds an OWNER RIGHTS ACE when inheritance is removed; the owner is the
# single user this boundary allows.
OWNER_RIGHTS_SID = "S-1-3-4"

Runner = Callable[[str], str]


def _powershell(script: str) -> str:
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise AppError(
            code="ACL_CHECK_FAILED",
            message="the Windows ACL could not be inspected",
            category=ErrorCategory.INTEGRITY,
            status=500,
            details={"stderr": completed.stderr.strip()[:400]},
        )
    return completed.stdout


def _quote(path: Path) -> str:
    return str(path).replace("'", "''")


def current_user_sid(runner: Runner = _powershell) -> str:
    script = "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"
    return runner(script).strip()


def inspect_directory(path: Path, runner: Runner = _powershell) -> dict[str, Any]:
    """Return the effective ACL as ``protected`` plus allow/deny entries by SID."""
    script = (
        f"$acl = Get-Acl -LiteralPath '{_quote(path)}'; "
        "'PROTECTED=' + $acl.AreAccessRulesProtected; "
        "$acl.Access | ForEach-Object { "
        "'ENTRY|' + $_.IdentityReference.Translate("
        "[System.Security.Principal.SecurityIdentifier]).Value + '|' + "
        "$_.AccessControlType + '|' + $_.FileSystemRights }"
    )
    output = runner(script)
    protected = False
    entries: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("PROTECTED="):
            protected = line.split("=", 1)[1].lower() == "true"
        elif line.startswith("ENTRY|"):
            _, sid, access_type, rights = line.split("|", 3)
            entries.append(
                {"sid": sid, "type": access_type, "rights": rights}
            )
    return {"protected": protected, "entries": entries}


def verify_directory(
    path: Path, *, runner: Runner = _powershell, user_sid: str | None = None
) -> None:
    if os.name != "nt":
        return
    sid = user_sid or current_user_sid(runner)
    allowed = {sid, SYSTEM_SID, ADMINISTRATORS_SID, OWNER_RIGHTS_SID}
    info = inspect_directory(path, runner)
    violations: list[str] = []
    if not info["protected"]:
        violations.append("the directory still inherits permissions from its parent")
    for entry in info["entries"]:
        if entry["type"].lower() != "allow":
            continue
        if entry["sid"] not in allowed:
            violations.append(f"{entry['sid']} has {entry['rights']}")
    if violations:
        raise AppError(
            code="ACL_INSUFFICIENT",
            message="the data directory is accessible beyond the single-user boundary",
            category=ErrorCategory.INTEGRITY,
            status=500,
            details={
                "path": str(path),
                "violations": violations,
                "allowed_principals": sorted(allowed),
                "remediation": (
                    "remove inherited or additional principals and re-run startup; "
                    "the app will not store data outside its private ACL"
                ),
            },
        )


def secure_directory(path: Path, *, runner: Runner = _powershell) -> None:
    """Create the directory and restrict it to the current user, SYSTEM, Admins."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
        return
    sid = current_user_sid(runner)
    script = (
        f"icacls '{_quote(path)}' /inheritance:r /grant:r "
        f'"*{sid}:(OI)(CI)F" "*{SYSTEM_SID}:(OI)(CI)F" '
        f'"*{ADMINISTRATORS_SID}:(OI)(CI)F"; exit $LASTEXITCODE'
    )
    runner(script)
    verify_directory(path, runner=runner, user_sid=sid)


def secure_paths(paths: Iterable[Path], *, runner: Runner = _powershell) -> None:
    for path in paths:
        secure_directory(path, runner=runner)


def secure_data_directories(config: Any, *, runner: Runner = _powershell) -> None:
    """Protect every directory the data boundary owns, then re-verify each one."""
    secure_paths(
        (
            config.data_dir,
            config.db_path.parent,
            config.artifacts_dir,
            config.backups_dir,
            config.logs_dir,
            config.cache_dir,
            config.runtime_dir,
        ),
        runner=runner,
    )

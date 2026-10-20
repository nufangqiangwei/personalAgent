"""Windows single-user ACL self-check (T116)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from personal_agent.bootstrap.acl import (
    ADMINISTRATORS_SID,
    OWNER_RIGHTS_SID,
    SYSTEM_SID,
    current_user_sid,
    inspect_directory,
    secure_directory,
    verify_directory,
)
from personal_agent.domain.common import AppError

USER_SID = "S-1-5-21-111111111-222222222-333333333-1001"
EVERYONE_SID = "S-1-1-0"


def _runner(
    *,
    entries: list[tuple[str, str]] | None = None,
    protected: bool = True,
    sid: str = USER_SID,
):
    calls: list[str] = []

    def run(script: str) -> str:
        calls.append(script)
        if "WindowsIdentity" in script:
            return sid
        if "icacls" in script:
            return ""
        if "Get-Acl" in script:
            assert entries is not None
            lines = [f"PROTECTED={protected}"]
            lines.extend(
                f"ENTRY|{entry_sid}|Allow|{rights}" for entry_sid, rights in entries
            )
            return "\n".join(lines) + "\n"
        raise AssertionError(f"unexpected script: {script}")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_a_private_directory_passes_verification() -> None:
    runner = _runner(entries=[
        (USER_SID, "FullControl"),
        (SYSTEM_SID, "FullControl"),
        (ADMINISTRATORS_SID, "FullControl"),
    ])
    verify_directory(Path("C:/data"), runner=runner, user_sid=USER_SID)


def test_an_extra_principal_is_reported_and_blocks() -> None:
    runner = _runner(entries=[
        (USER_SID, "FullControl"),
        (EVERYONE_SID, "ReadAndExecute"),
    ])
    with pytest.raises(AppError) as error:
        verify_directory(Path("C:/data"), runner=runner, user_sid=USER_SID)
    assert error.value.code == "ACL_INSUFFICIENT"
    assert EVERYONE_SID in " ".join(error.value.details["violations"])
    assert error.value.details["remediation"]


def test_inherited_permissions_fail_the_check() -> None:
    runner = _runner(entries=[(USER_SID, "FullControl")], protected=False)
    with pytest.raises(AppError) as error:
        verify_directory(Path("C:/data"), runner=runner, user_sid=USER_SID)
    assert "inherits" in " ".join(error.value.details["violations"])


class _Config:
    def __init__(self, root: Path) -> None:
        self.data_dir = root
        self.db_path = root / "db" / "personal-agent.db"
        self.artifacts_dir = root / "artifacts"
        self.backups_dir = root / "backups"
        self.logs_dir = root / "logs"
        self.cache_dir = root / "cache"
        self.runtime_dir = root / "runtime"


def test_secure_data_directories_secures_every_owned_directory(tmp_path: Path) -> None:
    from personal_agent.bootstrap.acl import secure_data_directories

    runner = _runner(entries=[(USER_SID, "FullControl")])
    config = _Config(tmp_path / "data")
    secure_data_directories(config, runner=runner)
    for directory in (
        config.data_dir, config.db_path.parent, config.artifacts_dir,
        config.backups_dir, config.logs_dir, config.cache_dir, config.runtime_dir,
    ):
        assert directory.is_dir()
    grants = [script for script in runner.calls if "icacls" in script]
    assert len(grants) == 7  # type: ignore[attr-defined]


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL")
def test_real_icacls_creates_a_private_directory(tmp_path: Path) -> None:
    target = tmp_path / "protected"
    secure_directory(target)
    sid = current_user_sid()
    info = inspect_directory(target)
    assert info["protected"] is True
    allowed = {sid, SYSTEM_SID, ADMINISTRATORS_SID, OWNER_RIGHTS_SID}
    for entry in info["entries"]:
        if entry["type"].lower() == "allow":
            assert entry["sid"] in allowed, entry

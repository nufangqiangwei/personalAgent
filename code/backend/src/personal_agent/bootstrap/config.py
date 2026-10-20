"""Versioned configuration and default policy.

The policy numbers are displayed before execution and must be explicitly
acknowledged; changing any value changes ``policy_hash`` and invalidates earlier
acknowledgements. Nothing here reads credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from personal_agent.domain.common import stable_hash

POLICY_VERSION: Final = "1"
APP_VERSION: Final = "0.1.0"
SCHEMA_VERSION: Final = 1

# Hard limits from the plan; each is either a budget dimension or an input guard.
MAX_DEFINITION_BYTES: Final = 1024 * 1024  # 1 MiB
MAX_EVENT_PAYLOAD_BYTES: Final = 64 * 1024  # 64 KiB
MAX_ARTIFACT_BYTES: Final = 20 * 1024 * 1024  # 20 MiB
MAX_STEPS: Final = 100
MAX_WORKFLOW_VERSION_NO: Final = 1_000_000


def default_data_dir() -> Path:
    override = os.environ.get("PERSONAL_AGENT_DATA_DIR")
    if override:
        return Path(override).expanduser()
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "Personal-Agent"
    return Path.home() / ".personal-agent"


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    max_active_seconds: int = 20 * 60
    max_model_requests: int = 30
    max_tool_calls: int = 60
    max_search_calls: int = 20
    max_page_reads: int = 40
    max_tokens: int = 100_000

    def as_dict(self) -> dict[str, int]:
        # Keys are ledger dimensions, not policy field names.
        return {
            "active_ms": self.max_active_seconds * 1000,
            "model_requests": self.max_model_requests,
            "tool_calls": self.max_tool_calls,
            "search_calls": self.max_search_calls,
            "page_reads": self.max_page_reads,
            "tokens": self.max_tokens,
        }


@dataclass(frozen=True, slots=True)
class DraftBudgetLimits:
    max_model_requests: int = 20
    max_tokens: int = 30_000

    def as_dict(self) -> dict[str, int]:
        # Keys are budget dimensions, matching the ledger, not policy field names.
        return {
            "model_requests": self.max_model_requests,
            "tokens": self.max_tokens,
        }


@dataclass(frozen=True, slots=True)
class ModelPolicy:
    timeout_seconds: int = 120
    max_output_tokens: int = 8192
    max_attempts_per_step: int = 3
    retry_backoff_seconds: tuple[int, ...] = (1, 4)


@dataclass(frozen=True, slots=True)
class TransportPolicy:
    http_connect_seconds: int = 10
    http_read_seconds: int = 30
    browser_action_seconds: int = 30
    stop_convergence_seconds: int = 15


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    cache_and_logs_days: int = 7
    session_data_max_days: int = 7
    business_content_days: int = 90
    backup_days: int = 7


@dataclass(frozen=True, slots=True)
class Policy:
    version: str = POLICY_VERSION
    budget: BudgetLimits = field(default_factory=BudgetLimits)
    draft: DraftBudgetLimits = field(default_factory=DraftBudgetLimits)
    model: ModelPolicy = field(default_factory=ModelPolicy)
    transport: TransportPolicy = field(default_factory=TransportPolicy)
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    storage_scope: str = "local-only"

    def as_document(self) -> dict[str, Any]:
        """The exact document shown to the user and hashed for acknowledgement."""
        return {
            "version": self.version,
            "budget_defaults": self.budget.as_dict(),
            "draft_budget_defaults": self.draft.as_dict(),
            "model": {
                "timeout_seconds": self.model.timeout_seconds,
                "max_output_tokens": self.model.max_output_tokens,
                "max_attempts_per_step": self.model.max_attempts_per_step,
                "retry_backoff_seconds": list(self.model.retry_backoff_seconds),
            },
            "transport": {
                "http_connect_seconds": self.transport.http_connect_seconds,
                "http_read_seconds": self.transport.http_read_seconds,
                "browser_action_seconds": self.transport.browser_action_seconds,
                "stop_convergence_seconds": self.transport.stop_convergence_seconds,
            },
            "retention": {
                "cache_and_logs_days": self.retention.cache_and_logs_days,
                "session_data_max_days": self.retention.session_data_max_days,
                "business_content_days": self.retention.business_content_days,
                "backup_days": self.retention.backup_days,
            },
            "storage_scope": self.storage_scope,
        }

    @property
    def policy_hash(self) -> str:
        return stable_hash(self.as_document())


DEFAULT_POLICY: Final = Policy()


@dataclass(frozen=True, slots=True)
class AppConfig:
    mode: str
    data_dir: Path
    host: str
    port: int
    dev_origin: str | None
    policy: Policy
    profile_path: Path | None = None
    frontend_dist: Path | None = None

    @property
    def policy_hash(self) -> str:
        return self.policy.policy_hash

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "personal-agent.db"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def runtime_dir(self) -> Path:
        return self.data_dir / "runtime"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    def ensure_directories(self) -> None:
        for directory in (
            self.data_dir,
            self.db_path.parent,
            self.artifacts_dir,
            self.backups_dir,
            self.logs_dir,
            self.cache_dir,
            self.runtime_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


def load_config(
    *,
    mode: str | None = None,
    data_dir: Path | str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> AppConfig:
    resolved_mode = (mode or os.environ.get("PERSONAL_AGENT_MODE") or "mock").strip().lower()
    if resolved_mode not in {"mock", "live"}:
        raise ValueError(f"unsupported mode {resolved_mode!r}; expected mock or live")

    resolved_dir = Path(data_dir) if data_dir else default_data_dir()
    resolved_host = host or os.environ.get("PERSONAL_AGENT_HOST") or "127.0.0.1"
    resolved_port = port or int(os.environ.get("PERSONAL_AGENT_PORT") or 8000)
    dev_origin = os.environ.get("PERSONAL_AGENT_DEV_ORIGIN")

    repo_root = Path(__file__).resolve().parents[4]
    profile_override = os.environ.get("PERSONAL_AGENT_PROFILE_PATH")
    frontend_override = os.environ.get("PERSONAL_AGENT_FRONTEND_DIST")
    profile = (
        Path(profile_override)
        if profile_override
        else repo_root / "runtime-policy" / "profile.json"
    )
    frontend_dist = (
        Path(frontend_override)
        if frontend_override
        else repo_root / "frontend" / "dist"
    )

    return AppConfig(
        mode=resolved_mode,
        data_dir=resolved_dir,
        host=resolved_host,
        port=resolved_port,
        dev_origin=dev_origin,
        policy=DEFAULT_POLICY,
        profile_path=profile if profile.exists() else None,
        frontend_dist=frontend_dist if frontend_dist.exists() else None,
    )

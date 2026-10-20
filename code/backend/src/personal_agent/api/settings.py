"""First-run setup health, policy acknowledgement and credential health.

``completed`` is derived by the server from migrations, the data directory, runtime
availability and a current policy acknowledgement. The client can record an
acknowledgement, but cannot set ``completed`` itself.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from personal_agent.adapters.persistence.setup_models import PolicyAcknowledgement
from personal_agent.api.context import AppContext
from personal_agent.api.deps import context
from personal_agent.bootstrap.credentials import CredentialService
from personal_agent.domain.common import AppError, iso_z

router = APIRouter(prefix="/api/v1", tags=["settings"])


class AcknowledgementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_version: str = Field(min_length=1, max_length=32)
    policy_hash: str = Field(min_length=1, max_length=80)
    data_policy_acknowledged: bool = False
    budget_policy_acknowledged: bool = False
    explicit_confirm: bool = False


class CredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(pattern="^(ENV|OS_CREDENTIAL_STORE)$")
    secret: str | None = None
    explicit_save: bool = False


class CredentialRemovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(pattern="^(ENV|OS_CREDENTIAL_STORE)$")
    explicit_remove: bool = False


def _data_dir_writable(app_context: AppContext) -> bool:
    try:
        probe = app_context.config.data_dir / "runtime" / ".write-probe"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


async def setup_status(app_context: AppContext) -> dict[str, Any]:
    policy = app_context.config.policy
    async with app_context.database.session() as session:
        acknowledgement = await session.get(PolicyAcknowledgement, policy.policy_hash)
        acknowledged = acknowledgement is not None
    writable = _data_dir_writable(app_context)
    migrations_current = app_context.migrations_up_to_date
    runtime_compatible = bool(
        app_context.runtime_capabilities.get("tool_policy_enforced")
    )
    completed = (
        migrations_current
        and writable
        and app_context.execution_available
        and acknowledged
    )
    missing: list[str] = []
    if not migrations_current:
        missing.append("数据库迁移未到达当前版本")
    if not writable:
        missing.append("数据目录不可写")
    if not app_context.execution_available:
        missing.append("执行运行时不可用")
    if not acknowledged:
        missing.append("尚未确认当前政策")
    return {
        "completed": completed,
        "required": not completed,
        "missing": missing,
        "migrations": {
            "current": app_context.migration_current,
            "head": app_context.migration_head,
            "up_to_date": migrations_current,
        },
        "data_dir": {"writable": writable, "path": str(app_context.config.data_dir)},
        "runtime": {
            "available": bool(app_context.runtime_capabilities),
            "compatible": runtime_compatible,
            "execution_available": app_context.execution_available,
        },
        "policy": {
            "version": policy.version,
            "hash": policy.policy_hash,
            "acknowledged": acknowledged,
            "acknowledged_at": iso_z(acknowledgement.confirmed_at)
            if acknowledgement
            else None,
        },
        "credentials": CredentialService(app_context.clock).health(),
    }


@router.get("/setup/status")
async def get_setup_status(request: Request) -> dict[str, Any]:
    return await setup_status(context(request))


@router.post("/setup/acknowledgements")
async def acknowledge(request: Request, payload: AcknowledgementRequest) -> dict[str, Any]:
    app_context = context(request)
    policy = app_context.config.policy
    if (
        payload.policy_hash != policy.policy_hash
        or payload.policy_version != policy.version
    ):
        raise AppError(
            code="POLICY_CHANGED",
            message="the policy changed; reload it before acknowledging",
            status=409,
        )
    if not payload.explicit_confirm:
        raise AppError(
            code="POLICY_ACKNOWLEDGEMENT_REQUIRED",
            message="acknowledging the policy needs an explicit confirmation",
            status=422,
        )
    async with app_context.database.session() as session:
        row = await session.get(PolicyAcknowledgement, policy.policy_hash)
        if row is None:
            row = PolicyAcknowledgement(
                policy_hash=policy.policy_hash,
                policy_version=policy.version,
                data_policy_acknowledged=payload.data_policy_acknowledged,
                budget_policy_acknowledged=payload.budget_policy_acknowledged,
                confirmed_at=app_context.clock.now(),
            )
            session.add(row)
        else:
            row.data_policy_acknowledged = payload.data_policy_acknowledged
            row.budget_policy_acknowledged = payload.budget_policy_acknowledged
        await session.commit()
    return await setup_status(app_context)


@router.get("/settings/credentials")
async def list_credentials(request: Request) -> dict[str, Any]:
    return CredentialService(context(request).clock).health()


@router.put("/settings/credentials/{provider}")
async def put_credential(
    provider: str, request: Request, payload: CredentialRequest
) -> dict[str, Any]:
    return CredentialService(context(request).clock).put(
        provider,
        source=payload.source,
        secret=payload.secret,
        explicit_save=payload.explicit_save,
    )


@router.delete("/settings/credentials/{provider}")
async def delete_credential(
    provider: str, request: Request, payload: CredentialRemovalRequest
) -> dict[str, Any]:
    return CredentialService(context(request).clock).remove(
        provider, source=payload.source, explicit_remove=payload.explicit_remove
    )


__all__ = ["router", "setup_status"]

"""Maintenance endpoints: status, transitions, backups, restores and migrations.

Every write is explicit: entering/exiting, creating a backup, verifying it, migrating
and restoring all need an explicit confirmation, and backups/restores are refused while
anything is still in flight. Restoring keeps the mode in MAINTENANCE and applies the
current lifecycle policy before and after; it never auto-resumes research.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from personal_agent.api.context import AppContext
from personal_agent.api.data_items import lifecycle_of
from personal_agent.api.deps import context, require_if_match
from personal_agent.application.maintenance import MaintenanceService

router = APIRouter(prefix="/api/v1", tags=["maintenance"])


class EnterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)
    explicit_confirm: bool = False


class ExitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    explicit_confirm: bool = False


class BackupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=2000)
    explicit_backup: bool = False


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest_hash: str = Field(min_length=1, max_length=80)


class MigrateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_revision: str = "head"
    backup_id: str = Field(min_length=1, max_length=36)
    explicit_migrate: bool = False


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backup_id: str = Field(min_length=1, max_length=36)
    manifest_hash: str = Field(min_length=1, max_length=80)
    explicit_restore: bool = False
    acknowledge_interruption: bool = False
    acknowledge_lifecycle: bool = False


def service_of(app_context: AppContext) -> MaintenanceService:
    return MaintenanceService(
        app_context.database, app_context.clock, app_context.config, app_context.gateway
    )


@router.get("/maintenance/status")
async def status(request: Request) -> dict[str, Any]:
    return await service_of(context(request)).status()


@router.post("/maintenance/enter", status_code=202)
async def enter(request: Request, payload: EnterRequest) -> dict[str, Any]:
    app_context = context(request)
    revision: int | None = None
    header = request.headers.get("if-match")
    if header:
        try:
            revision = int(header.strip('"'))
        except ValueError:
            revision = None
    result = await service_of(app_context).enter(
        reason=payload.reason,
        explicit_confirm=payload.explicit_confirm,
        expected_revision=revision,
    )
    return result


@router.post("/maintenance/exit")
async def exit_maintenance(request: Request, payload: ExitRequest) -> dict[str, Any]:
    app_context = context(request)
    require_if_match(request, payload.expected_revision, code="MAINTENANCE_REVISION_STALE")
    return await service_of(app_context).exit(
        expected_revision=payload.expected_revision,
        explicit_confirm=payload.explicit_confirm,
    )


@router.post("/maintenance/migrations", status_code=202)
async def migrate(request: Request, payload: MigrateRequest) -> dict[str, Any]:
    if not payload.explicit_migrate:
        from personal_agent.domain.common import AppError

        raise AppError(
            code="EXPLICIT_CONFIRMATION_REQUIRED",
            message="migrations need an explicit confirmation",
            status=422,
        )
    return await service_of(context(request)).migrate(
        backup_id=payload.backup_id, target_revision=payload.target_revision
    )


@router.get("/maintenance/backups")
async def list_backups(request: Request) -> dict[str, Any]:
    app_context = context(request)
    return {"items": await service_of(app_context).backups.list_backups()}


@router.post("/maintenance/backups", status_code=202)
async def create_backup(request: Request, payload: BackupRequest) -> dict[str, Any]:
    return await service_of(context(request)).create_backup(
        reason=payload.reason, explicit_backup=payload.explicit_backup
    )


@router.get("/maintenance/backups/{backup_id}")
async def get_backup(backup_id: str, request: Request) -> dict[str, Any]:
    return await service_of(context(request)).backups.get(backup_id)


@router.post("/maintenance/backups/{backup_id}/verify")
async def verify_backup(
    backup_id: str, request: Request, payload: VerifyRequest
) -> dict[str, Any]:
    return await service_of(context(request)).verify_backup(
        backup_id, manifest_hash=payload.manifest_hash
    )


@router.post("/maintenance/restores", status_code=202)
async def create_restore(request: Request, payload: RestoreRequest) -> dict[str, Any]:
    app_context = context(request)
    service = service_of(app_context)
    lifecycle = lifecycle_of(app_context)
    # Apply the current retention policy before replacing data, then again afterwards
    # so the restored snapshot cannot inherit an outdated retention window.
    await lifecycle.apply_retention()
    result = await service.restore(
        backup_id=payload.backup_id,
        manifest_hash=payload.manifest_hash,
        explicit_restore=payload.explicit_restore,
        acknowledge_interruption=payload.acknowledge_interruption,
        acknowledge_lifecycle=payload.acknowledge_lifecycle,
    )
    await lifecycle.apply_retention()
    return result


@router.get("/maintenance/restores/{restore_id}")
async def get_restore(restore_id: str, request: Request) -> dict[str, Any]:
    return await service_of(context(request)).restores.get(restore_id)


__all__ = ["router"]

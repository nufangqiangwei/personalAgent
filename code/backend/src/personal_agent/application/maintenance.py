"""Maintenance mode: stop new actions, prove quiescence, then back up or migrate.

The mode is a single-row state machine. Entering never claims "stopped" while an
action is still in flight; a backup or migration is refused until nothing is pending.
A restore leaves the mode in MAINTENANCE and keeps restored runs INTERRUPTED — exiting
maintenance never auto-resumes research.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import func, select

from personal_agent.adapters.persistence.artifact_models import SideEffectOperation
from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.execution_models import (
    ExecutionSlot,
    StepAttempt,
    StepRun,
)
from personal_agent.adapters.persistence.lifecycle_models import (
    BackupManifest,
    MaintenanceState,
    RestoreRecord,
)
from personal_agent.adapters.persistence.uow import cas_update
from personal_agent.bootstrap.backup import BackupService
from personal_agent.bootstrap.config import AppConfig
from personal_agent.bootstrap.lifespan import migration_current, migration_head
from personal_agent.bootstrap.restore import RestoreService
from personal_agent.domain.common import AppError, iso_z
from personal_agent.ports.contracts import Clock

UNRESOLVED_SIDE_EFFECTS = {"INTENT", "UNKNOWN", "PARTIAL", "CONFLICT"}


class MaintenanceService:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        config: AppConfig,
        gateway: Any,
    ) -> None:
        self._database = database
        self._clock = clock
        self._config = config
        self._gateway = gateway
        self.backups = BackupService(
            database, clock, data_dir=config.data_dir, policy=config.policy
        )
        self.restores = RestoreService(
            database, clock, data_dir=config.data_dir, policy=config.policy
        )

    # ------------------------------------------------------------------
    async def _state(self, session: Any) -> MaintenanceState:
        state = await session.get(MaintenanceState, 1)
        if state is None:
            state = MaintenanceState(
                singleton_id=1,
                mode="NORMAL",
                reason=None,
                requested_at=None,
                entered_at=None,
                revision=1,
            )
            session.add(state)
            await session.flush()
        return state

    async def _pending(self) -> dict[str, Any]:
        async with self._database.session() as session:
            in_flight = list(
                (
                    await session.scalars(
                        select(StepAttempt)
                        .join(StepRun, StepAttempt.step_run_id == StepRun.id)
                        .where(StepAttempt.status == "RUNNING")
                    )
                ).all()
            )
            operations = list(
                (
                    await session.scalars(
                        select(SideEffectOperation).where(
                            SideEffectOperation.status.in_(UNRESOLVED_SIDE_EFFECTS)
                        )
                    )
                ).all()
            )
            slot = await session.get(ExecutionSlot, 1)
            backup_in_progress = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BackupManifest)
                    .where(BackupManifest.status.in_(["CREATING", "VERIFYING"]))
                )
                or 0
            )
            restore_in_progress = int(
                await session.scalar(
                    select(func.count())
                    .select_from(RestoreRecord)
                    .where(
                        RestoreRecord.status.in_(
                            ["VALIDATING", "APPLYING_TOMBSTONES", "RESTORING", "VERIFYING"]
                        )
                    )
                )
                or 0
            )
            unknown = await self._gateway.non_terminal_request()
            return {
                "in_flight_actions": [
                    {
                        "id": item.id,
                        "type": "model_request",
                        "status": item.status,
                        "step_run_id": item.step_run_id,
                    }
                    for item in in_flight
                ],
                "pending_writes": [
                    {"id": item.id, "status": item.status, "target_ref": item.target_ref}
                    for item in operations
                ],
                "slot_run_id": slot.run_id if slot else None,
                "backup_in_progress": backup_in_progress > 0,
                "restore_in_progress": restore_in_progress > 0,
                "unknown_model_request": (
                    {"id": unknown.id, "status": unknown.status} if unknown else None
                ),
            }

    async def status(self) -> dict[str, Any]:
        pending = await self._pending()
        quiescent = (
            not pending["in_flight_actions"]
            and not pending["pending_writes"]
            and pending["slot_run_id"] is None
            and not pending["backup_in_progress"]
            and not pending["restore_in_progress"]
        )
        async with self._database.session() as session:
            state = await self._state(session)
            # ENTERING only becomes MAINTENANCE once nothing is in flight.
            if state.mode == "ENTERING" and quiescent:
                now = self._clock.now()
                await cas_update(
                    session, MaintenanceState, identity={"singleton_id": 1},
                    expected_revision=state.revision,
                    values={"mode": "MAINTENANCE", "entered_at": now},
                    stale_code="MAINTENANCE_REVISION_STALE", stale_status=412,
                )
                state.mode = "MAINTENANCE"
                state.entered_at = now
                state.revision += 1
            if state.mode == "EXITING" and quiescent:
                await cas_update(
                    session, MaintenanceState, identity={"singleton_id": 1},
                    expected_revision=state.revision,
                    values={"mode": "NORMAL"},
                    stale_code="MAINTENANCE_REVISION_STALE", stale_status=412,
                )
                state.mode = "NORMAL"
                state.revision += 1
            await session.commit()
            mode = state.mode
            revision = state.revision
            reason = state.reason
            requested_at = state.requested_at
            entered_at = state.entered_at
        current = await migration_current(self._database)
        head = migration_head()
        return {
            "mode": mode,
            "revision": revision,
            "reason": reason,
            "requested_at": iso_z(requested_at) if requested_at else None,
            "entered_at": iso_z(entered_at) if entered_at else None,
            "draining": mode in {"ENTERING", "EXITING"} and not quiescent,
            "in_flight_actions": pending["in_flight_actions"],
            "pending_writes": pending["pending_writes"],
            "unknown_model_request": pending["unknown_model_request"],
            "quiescent": quiescent,
            "migration_version": current,
            "migration_head": head,
            "backup_in_progress": pending["backup_in_progress"],
            "restore_in_progress": pending["restore_in_progress"],
            "allowed_actions": self._allowed_actions(mode, quiescent, current, head),
        }

    @staticmethod
    def _allowed_actions(
        mode: str, quiescent: bool, current: str | None, head: str | None
    ) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []

        def add(action: str, enabled: bool, reason: str | None = None) -> None:
            actions.append(
                {
                    "action": action,
                    "enabled": enabled,
                    "disabled_reason": None if enabled else reason,
                }
            )

        if mode == "NORMAL":
            add("ENTER_MAINTENANCE", True)
        elif mode == "MAINTENANCE":
            add("CREATE_BACKUP", quiescent, "仍有在途动作或未核验写入")
            add("VERIFY_BACKUP", True)
            add(
                "EXIT_MAINTENANCE",
                quiescent and current == head,
                "需先收敛动作并迁移到当前版本",
            )
        else:
            add("ENTER_MAINTENANCE", False, "维护模式正在切换")
        return actions

    async def enter(
        self, *, reason: str, explicit_confirm: bool, expected_revision: int | None
    ) -> dict[str, Any]:
        if not explicit_confirm:
            raise AppError(
                code="EXPLICIT_CONFIRMATION_REQUIRED",
                message="entering maintenance needs an explicit confirmation",
                status=422,
            )
        async with self._database.session() as session:
            state = await self._state(session)
            if expected_revision is not None and expected_revision != state.revision:
                raise AppError(
                    code="MAINTENANCE_REVISION_STALE",
                    message="the maintenance state changed",
                    status=412,
                )
            if state.mode == "MAINTENANCE":
                allowed = True
            elif state.mode == "NORMAL":
                now = self._clock.now()
                await cas_update(
                    session, MaintenanceState, identity={"singleton_id": 1},
                    expected_revision=state.revision,
                    values={"mode": "ENTERING", "reason": reason, "requested_at": now},
                    stale_code="MAINTENANCE_REVISION_STALE", stale_status=412,
                )
                state.mode = "ENTERING"
                state.reason = reason
                state.requested_at = now
                state.revision += 1
                allowed = True
            else:
                allowed = False
            await session.commit()
            mode = state.mode
            revision = state.revision
        if not allowed:
            raise AppError(
                code="MAINTENANCE_BUSY",
                message="a maintenance transition is already in progress",
                status=409,
            )
        return {
            "mode": mode,
            "revision": revision,
            "status_url": "/api/v1/maintenance/status",
        }

    async def exit(self, *, expected_revision: int, explicit_confirm: bool) -> dict[str, Any]:
        if not explicit_confirm:
            raise AppError(
                code="EXPLICIT_CONFIRMATION_REQUIRED",
                message="exiting maintenance needs an explicit confirmation",
                status=422,
            )
        pending = await self._pending()
        current = await migration_current(self._database)
        head = migration_head()
        blockers: list[str] = []
        if pending["in_flight_actions"]:
            blockers.append("仍有在途动作")
        if pending["pending_writes"]:
            blockers.append("仍有未核验写入")
        if pending["restore_in_progress"]:
            blockers.append("恢复仍在进行")
        if current != head:
            blockers.append("迁移尚未到达当前版本")
        try:
            probe = self._config.data_dir / "runtime" / ".write-probe"
            probe.parent.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError:
            blockers.append("数据目录不可写")
        async with self._database.session() as session:
            state = await self._state(session)
            if state.revision != expected_revision:
                raise AppError(
                    code="MAINTENANCE_REVISION_STALE",
                    message="the maintenance state changed",
                    status=412,
                )
            if state.mode != "MAINTENANCE" or blockers:
                await session.commit()
                raise AppError(
                    code="MAINTENANCE_TRANSITION_BLOCKED",
                    message="maintenance cannot be exited yet",
                    status=409,
                    details={"blockers": blockers or ["当前不是维护模式"]},
                )
            await cas_update(
                session, MaintenanceState, identity={"singleton_id": 1},
                expected_revision=state.revision,
                values={"mode": "EXITING", "reason": None},
                stale_code="MAINTENANCE_REVISION_STALE", stale_status=412,
            )
            state.mode = "EXITING"
            state.reason = None
            state.revision += 1
            await session.commit()
        return await self.status()

    # ------------------------------------------------------------------
    async def create_backup(self, *, reason: str | None, explicit_backup: bool) -> dict[str, Any]:
        await self._require_maintenance()
        if not explicit_backup:
            raise AppError(
                code="EXPLICIT_CONFIRMATION_REQUIRED",
                message="creating a backup needs an explicit confirmation",
                status=422,
            )
        pending = await self._pending()
        if pending["in_flight_actions"] or pending["pending_writes"]:
            raise AppError(
                code="MAINTENANCE_BUSY",
                message="the application is not quiescent",
                status=409,
                details=pending,
            )
        manifest = await self.backups.create(reason=reason)
        return {
            "operation_id": manifest["id"],
            "backup_id": manifest["id"],
            "status": "REQUESTED",
            "status_url": f"/api/v1/maintenance/backups/{manifest['id']}",
        }

    async def verify_backup(self, backup_id: str, *, manifest_hash: str) -> dict[str, Any]:
        return await self.backups.verify(backup_id, manifest_hash=manifest_hash)

    async def restore(
        self,
        *,
        backup_id: str,
        manifest_hash: str,
        explicit_restore: bool,
        acknowledge_interruption: bool,
        acknowledge_lifecycle: bool,
    ) -> dict[str, Any]:
        await self._require_maintenance()
        if not (explicit_restore and acknowledge_interruption):
            raise AppError(
                code="RESTORE_BLOCKED",
                message="a restore needs explicit acknowledgements",
                status=422,
            )
        pending = await self._pending()
        if pending["in_flight_actions"] or pending["pending_writes"]:
            raise AppError(
                code="MAINTENANCE_BUSY",
                message="the application is not quiescent",
                status=409,
                details=pending,
            )
        return await self.restores.restore(
            backup_id=backup_id,
            manifest_hash=manifest_hash,
            acknowledge_lifecycle=acknowledge_lifecycle,
        )

    async def migrate(self, *, backup_id: str, target_revision: str) -> dict[str, Any]:
        await self._require_maintenance()
        if target_revision != "head":
            raise AppError(
                code="MAINTENANCE_TRANSITION_BLOCKED",
                message="only migrating to head is supported",
                status=422,
            )
        pending = await self._pending()
        if pending["in_flight_actions"] or pending["pending_writes"]:
            raise AppError(
                code="MAINTENANCE_BUSY",
                message="the application is not quiescent",
                status=409,
                details=pending,
            )
        async with self._database.session() as session:
            backup = await session.get(BackupManifest, backup_id)
            if backup is None or backup.status != "VERIFIED" or not backup.usable:
                raise AppError(
                    code="BACKUP_VERIFICATION_FAILED",
                    message="migration needs a verified backup",
                    status=422,
                )
        current = await migration_current(self._database)
        head = migration_head()
        if current == head:
            return {
                "operation_id": backup_id,
                "status": "COMPLETED",
                "app_restart_required": False,
                "status_url": f"/api/v1/maintenance/backups/{backup_id}",
            }
        try:
            await asyncio.to_thread(_run_upgrade)
            return {
                "operation_id": backup_id,
                "status": "COMPLETED",
                "app_restart_required": True,
                "status_url": f"/api/v1/maintenance/backups/{backup_id}",
            }
        except Exception as error:  # migration failure must be recoverable
            restored = await self.restores.restore(
                backup_id=backup_id,
                manifest_hash=backup.manifest_hash,
                acknowledge_lifecycle=True,
            )
            raise AppError(
                code="MAINTENANCE_TRANSITION_BLOCKED",
                message="the migration failed; the verified backup was restored",
                status=500,
                details={"error": str(error), "restore": restored["status"]},
            ) from error

    async def _require_maintenance(self) -> None:
        async with self._database.session() as session:
            state = await self._state(session)
            await session.commit()
            if state.mode != "MAINTENANCE":
                raise AppError(
                    code="MAINTENANCE_REQUIRED",
                    message="this operation is only available in maintenance mode",
                    status=409,
                    details={"mode": state.mode},
                )

    async def apply_lifecycle_after_restore(self, lifecycle: Any) -> dict[str, int]:
        return await lifecycle.apply_retention()


def _run_upgrade() -> None:
    from alembic import command
    from alembic.config import Config as AlembicConfig

    from personal_agent.bootstrap.lifespan import BACKEND_ROOT

    config = AlembicConfig(str(BACKEND_ROOT / "alembic.ini"))
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")


__all__ = ["MaintenanceService"]

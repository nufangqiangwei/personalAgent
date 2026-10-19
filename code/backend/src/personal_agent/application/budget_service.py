"""Budget service: reservations, settlements, and explicit draft top-ups.

The service is the only place that may write ``BudgetEntry`` rows. Callers pass
an idempotency ``operation_id``; a repeated request with the same triple
``(ledger, operation, kind)`` returns the recorded entry instead of charging
twice. Ledger updates use revision CAS, so concurrent analysis, settlement and
top-up cannot produce a partial grant.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.adapters.persistence.budget_models import BudgetEntry, BudgetLedger
from personal_agent.adapters.persistence.uow import cas_update
from personal_agent.domain import budget as rules
from personal_agent.domain.common import (
    DRAFT_BUDGET_CONFLICT,
    DRAFT_BUDGET_GRANT_INVALID,
    DRAFT_STATE_INVALID,
    AppError,
    iso_z,
    new_id,
)
from personal_agent.ports.contracts import Clock


class BudgetService:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    # ------------------------------------------------------------------
    # Ledger creation / reading
    # ------------------------------------------------------------------
    async def ensure_run_ledger(
        self, session: AsyncSession, *, root_run_id: str, limits: Mapping[str, int]
    ) -> BudgetLedger:
        existing = await session.scalar(
            select(BudgetLedger).where(BudgetLedger.root_run_id == root_run_id)
        )
        if existing is not None:
            return existing
        ledger = BudgetLedger(
            id=new_id(),
            root_run_id=root_run_id,
            draft_id=None,
            limits_json=dict(limits),
            consumed_json=dict.fromkeys(limits, 0),
            reserved_json=dict.fromkeys(limits, 0),
            active_ms=0,
            revision=1,
        )
        session.add(ledger)
        await session.flush()
        return ledger

    async def ensure_draft_ledger(
        self, session: AsyncSession, *, draft_id: str, limits: Mapping[str, int]
    ) -> BudgetLedger:
        existing = await session.scalar(
            select(BudgetLedger).where(BudgetLedger.draft_id == draft_id)
        )
        if existing is not None:
            return existing
        ledger = BudgetLedger(
            id=new_id(),
            root_run_id=None,
            draft_id=draft_id,
            limits_json=dict(limits),
            consumed_json=dict.fromkeys(limits, 0),
            reserved_json=dict.fromkeys(limits, 0),
            active_ms=0,
            revision=1,
        )
        session.add(ledger)
        await session.flush()
        return ledger

    async def load_ledger(self, session: AsyncSession, ledger_id: str) -> BudgetLedger:
        ledger = await session.get(BudgetLedger, ledger_id)
        if ledger is None:
            raise AppError(code="BUDGET_LEDGER_NOT_FOUND", message="ledger not found", status=404)
        return ledger

    async def snapshot(self, session: AsyncSession, ledger: BudgetLedger) -> dict[str, Any]:
        entries = await session.scalars(
            select(BudgetEntry)
            .where(BudgetEntry.ledger_id == ledger.id)
            .order_by(BudgetEntry.created_at)
        )
        domain_ledger = self.to_domain(ledger)
        snapshot: dict[str, Any] = {
            "ledger_id": ledger.id,
            "ledger_scope": "DRAFT" if ledger.draft_id else "RUN",
            "revision": ledger.revision,
            "limits": dict(ledger.limits_json),
            "consumed": dict(ledger.consumed_json),
            "reserved": dict(ledger.reserved_json),
            "remaining": domain_ledger.remaining(),
            "active_ms": ledger.active_ms,
            "exhausted": domain_ledger.exhausted,
            "exhausted_dimensions": domain_ledger.exhausted_dimensions(),
            "updated_at": iso_z(self._clock.now()),
            "grants": [
                {
                    "grant_id": entry.id,
                    "increment": dict(entry.amount_json),
                    "created_at": iso_z(entry.created_at),
                }
                for entry in entries
                if entry.kind == rules.EntryKind.GRANT.value
            ],
        }
        return snapshot

    @staticmethod
    def to_domain(ledger: BudgetLedger) -> rules.Ledger:
        scope = rules.LedgerScope.DRAFT if ledger.draft_id else rules.LedgerScope.RUN
        return rules.Ledger(
            scope=scope,
            limits=dict(ledger.limits_json),
            consumed=dict(ledger.consumed_json),
            reserved=dict(ledger.reserved_json),
            active_ms=ledger.active_ms,
            revision=ledger.revision,
        )

    # ------------------------------------------------------------------
    # Entry bookkeeping
    # ------------------------------------------------------------------
    async def _existing_entry(
        self, session: AsyncSession, ledger_id: str, operation_id: str, kind: str
    ) -> BudgetEntry | None:
        return await session.scalar(
            select(BudgetEntry).where(
                BudgetEntry.ledger_id == ledger_id,
                BudgetEntry.operation_id == operation_id,
                BudgetEntry.kind == kind,
            )
        )

    async def _write_entry(
        self,
        session: AsyncSession,
        *,
        ledger_id: str,
        operation_id: str,
        kind: str,
        amount: Mapping[str, int],
        decision_id: str | None = None,
    ) -> BudgetEntry:
        existing = await self._existing_entry(session, ledger_id, operation_id, kind)
        if existing is not None:
            return existing
        entry = BudgetEntry(
            id=new_id(),
            ledger_id=ledger_id,
            operation_id=operation_id,
            kind=kind,
            amount_json=dict(amount),
            created_at=self._clock.now(),
            decision_id=decision_id,
        )
        session.add(entry)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            stored = await self._existing_entry(session, ledger_id, operation_id, kind)
            if stored is None:
                raise
            return stored
        return entry

    async def reserve(
        self,
        session: AsyncSession,
        *,
        ledger: BudgetLedger,
        worst_case: Mapping[str, int],
        operation_id: str,
    ) -> str:
        """Persist the worst-case reservation and return its entry id."""
        existing = await self._existing_entry(
            session, ledger.id, operation_id, rules.EntryKind.RESERVE.value
        )
        if existing is not None:
            return existing.id
        domain = self.to_domain(ledger)
        updated = rules.reserve(domain, worst_case)
        await cas_update(
            session,
            BudgetLedger,
            identity={"id": ledger.id},
            expected_revision=ledger.revision,
            values={
                "reserved_json": dict(updated.reserved),
                "revision": updated.revision,
            },
            stale_code="BUDGET_CONFLICT",
            stale_status=409,
        )
        entry = await self._write_entry(
            session,
            ledger_id=ledger.id,
            operation_id=operation_id,
            kind=rules.EntryKind.RESERVE.value,
            amount=worst_case,
        )
        ledger.reserved_json = dict(updated.reserved)
        ledger.revision = updated.revision
        return entry.id

    async def settle(
        self,
        session: AsyncSession,
        *,
        ledger: BudgetLedger,
        reserved: Mapping[str, int],
        actual: Mapping[str, int],
        operation_id: str,
    ) -> None:
        existing = await self._existing_entry(
            session, ledger.id, operation_id, rules.EntryKind.SETTLE.value
        )
        if existing is not None:
            return
        domain = self.to_domain(ledger)
        updated = rules.settle(domain, reserved, actual)
        await cas_update(
            session,
            BudgetLedger,
            identity={"id": ledger.id},
            expected_revision=ledger.revision,
            values={
                "reserved_json": dict(updated.reserved),
                "consumed_json": dict(updated.consumed),
                "revision": updated.revision,
            },
            stale_code="BUDGET_CONFLICT",
            stale_status=409,
        )
        await self._write_entry(
            session,
            ledger_id=ledger.id,
            operation_id=operation_id,
            kind=rules.EntryKind.SETTLE.value,
            amount={
                key: max(reserved.get(key, 0), actual.get(key, 0))
                for key in set(reserved) | set(actual)
            },
        )
        ledger.reserved_json = dict(updated.reserved)
        ledger.consumed_json = dict(updated.consumed)
        ledger.revision = updated.revision

    async def release(
        self,
        session: AsyncSession,
        *,
        ledger: BudgetLedger,
        reserved: Mapping[str, int],
        operation_id: str,
    ) -> None:
        existing = await self._existing_entry(
            session, ledger.id, operation_id, rules.EntryKind.RELEASE.value
        )
        if existing is not None:
            return
        domain = self.to_domain(ledger)
        updated = rules.release(domain, reserved)
        await cas_update(
            session,
            BudgetLedger,
            identity={"id": ledger.id},
            expected_revision=ledger.revision,
            values={"reserved_json": dict(updated.reserved), "revision": updated.revision},
            stale_code="BUDGET_CONFLICT",
            stale_status=409,
        )
        await self._write_entry(
            session,
            ledger_id=ledger.id,
            operation_id=operation_id,
            kind=rules.EntryKind.RELEASE.value,
            amount=reserved,
        )
        ledger.reserved_json = dict(updated.reserved)
        ledger.revision = updated.revision

    # ------------------------------------------------------------------
    # Explicit draft top-up (T015 / REST contract)
    # ------------------------------------------------------------------
    async def grant_run_budget(
        self,
        session: AsyncSession,
        *,
        ledger: BudgetLedger,
        increment: Mapping[str, int],
        operation_id: str,
        note: str | None = None,
    ) -> tuple[BudgetEntry, dict[str, Any]]:
        """Explicit run-ledger top-up after an assistance request.

        The grant raises only the limits; consumed and reserved are untouched, so a
        top-up never rewrites history or clears retries.
        """
        existing = await self._existing_entry(
            session, ledger.id, operation_id, rules.EntryKind.GRANT.value
        )
        if existing is not None:
            return existing, await self.snapshot(session, ledger)

        domain = self.to_domain(ledger)
        if not domain.exhausted:
            raise AppError(
                code="BUDGET_NOT_EXHAUSTED",
                message="the run is not at its limit; a preventive top-up is not allowed",
                status=409,
                details={"remaining": dict(domain.remaining())},
            )
        try:
            updated = rules.grant(domain, increment, default_limits={})
        except AppError as error:
            if error.code == "BUDGET_AMOUNT_INVALID":
                raise AppError(
                    code="BUDGET_GRANT_INVALID",
                    message=error.message,
                    status=422,
                    details=error.details,
                ) from error
            raise
        await cas_update(
            session,
            BudgetLedger,
            identity={"id": ledger.id},
            expected_revision=ledger.revision,
            values={"limits_json": dict(updated.limits), "revision": updated.revision},
            stale_code="BUDGET_CONFLICT",
            stale_status=409,
        )
        entry = await self._write_entry(
            session,
            ledger_id=ledger.id,
            operation_id=operation_id,
            kind=rules.EntryKind.GRANT.value,
            amount=increment,
        )
        ledger.limits_json = dict(updated.limits)
        ledger.revision = updated.revision
        snapshot = await self.snapshot(session, ledger)
        snapshot["note"] = note
        return entry, snapshot

    async def grant_draft_budget(
        self,
        session: AsyncSession,
        *,
        draft_id: str,
        draft_status: str,
        draft_revision: int,
        ledger: BudgetLedger,
        increment: Mapping[str, int],
        operation_id: str,
        default_limits: Mapping[str, int],
        note: str | None = None,
    ) -> tuple[BudgetEntry, dict[str, Any]]:
        existing = await self._existing_entry(
            session, ledger.id, operation_id, rules.EntryKind.GRANT.value
        )
        if existing is not None:
            return existing, await self.snapshot(session, ledger)

        if draft_status not in {"COLLECTING", "READY"}:
            raise AppError(
                code=DRAFT_STATE_INVALID,
                message="only COLLECTING or READY drafts may receive a top-up",
                status=409,
                details={"status": draft_status},
            )
        domain = self.to_domain(ledger)
        rules.require_exhausted(domain)
        try:
            updated = rules.grant(domain, increment, default_limits=default_limits)
        except AppError as error:
            if error.code == "BUDGET_AMOUNT_INVALID":
                raise AppError(
                    code=DRAFT_BUDGET_GRANT_INVALID,
                    message=error.message,
                    status=422,
                    details=error.details,
                ) from error
            raise

        await cas_update(
            session,
            BudgetLedger,
            identity={"id": ledger.id},
            expected_revision=ledger.revision,
            values={
                "limits_json": dict(updated.limits),
                "revision": updated.revision,
            },
            stale_code=DRAFT_BUDGET_CONFLICT,
            stale_status=409,
        )

        entry = await self._write_entry(
            session,
            ledger_id=ledger.id,
            operation_id=operation_id,
            kind=rules.EntryKind.GRANT.value,
            amount=increment,
        )
        ledger.limits_json = dict(updated.limits)
        ledger.revision = updated.revision
        snapshot = await self.snapshot(session, ledger)
        snapshot["note"] = note
        return entry, snapshot

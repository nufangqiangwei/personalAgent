"""Per-call model budget metering for the official Loop.

One attempt may issue several model calls (tool turns, retries). The engine's
attempt-level reservation cannot cover them: the gateway refuses a second call
whose reservation is already settled. This meter reserves and settles once per
call, with a stable operation id, and keeps the reservation when the outcome is
unknown so a restart cannot silently free the budget.

Only the ledger is needed: the attempt carries its root run id, and the ledger
row has no run foreign key of its own.
"""

from __future__ import annotations

from sqlalchemy import select

from personal_agent.adapters.persistence.budget_models import BudgetLedger
from personal_agent.adapters.persistence.database import Database
from personal_agent.application.budget_service import BudgetService
from personal_agent.domain.common import AppError
from personal_agent.ports.contracts import ModelCallGrant, ToolCallGrant, Usage


class RunModelMeter:
    def __init__(self, database: Database, budget: BudgetService) -> None:
        self._database = database
        self._budget = budget
        self._calls: dict[str, int] = {}

    async def _ledger_id(self, root_run_id: str) -> str:
        async with self._database.session() as session:
            ledger = await session.scalar(
                select(BudgetLedger).where(BudgetLedger.root_run_id == root_run_id)
            )
        if ledger is None:
            raise AppError(
                code="BUDGET_LEDGER_NOT_FOUND",
                message="the run has no budget ledger",
                status=404,
            )
        return ledger.id

    async def begin(
        self, *, root_run_id: str, attempt_id: str, output_limit: int,
        estimated_input: int,
    ) -> ModelCallGrant:
        if not root_run_id or not attempt_id:
            raise AppError(
                code="BUDGET_LEDGER_NOT_FOUND",
                message="a model call needs its run and attempt",
                status=404,
            )
        sequence = self._calls.get(attempt_id, 0) + 1
        operation_id = f"{attempt_id}:model:{sequence}"
        reserved = max(output_limit, 1) + max(estimated_input, 0)
        async with self._database.session() as session:
            ledger = await session.scalar(
                select(BudgetLedger).where(BudgetLedger.root_run_id == root_run_id)
            )
            if ledger is None:
                raise AppError(
                    code="BUDGET_LEDGER_NOT_FOUND",
                    message="the run has no budget ledger",
                    status=404,
                )
            await self._budget.reserve(
                session,
                ledger=ledger,
                worst_case={"model_requests": 1, "tokens": reserved},
                operation_id=operation_id,
            )
            await session.commit()
        self._calls[attempt_id] = sequence
        return ModelCallGrant(operation_id, reserved)

    async def settle(
        self, *, root_run_id: str, grant: ModelCallGrant, usage: Usage,
    ) -> None:
        async with self._database.session() as session:
            ledger = await session.scalar(
                select(BudgetLedger).where(BudgetLedger.root_run_id == root_run_id)
            )
            if ledger is None:
                raise AppError(
                    code="BUDGET_LEDGER_NOT_FOUND",
                    message="the run has no budget ledger",
                    status=404,
                )
            await self._budget.settle(
                session,
                ledger=ledger,
                reserved={"model_requests": 1, "tokens": grant.reserved_tokens},
                actual={"model_requests": 1, "tokens": max(usage.tokens, 0)},
                operation_id=grant.reservation_id,
            )
            await session.commit()

    async def release(
        self, *, root_run_id: str, grant: ModelCallGrant,
    ) -> None:
        """The call never started, so its reservation is returned, not consumed."""
        async with self._database.session() as session:
            ledger = await session.scalar(
                select(BudgetLedger).where(BudgetLedger.root_run_id == root_run_id)
            )
            if ledger is None:
                return
            await self._budget.release(
                session,
                ledger=ledger,
                reserved={"model_requests": 1, "tokens": grant.reserved_tokens},
                operation_id=grant.reservation_id,
            )
            await session.commit()


class RunToolMeter:
    """Per-call tool accounting for tools invoked inside the official Loop.

    Each dimension is reserved before the call and settled with the same amounts:
    a tool call always consumes exactly one ``tool_calls``, plus its search or
    page dimension. A refused-before-execution call releases its reservation.
    """

    def __init__(self, database: Database, budget: BudgetService) -> None:
        self._database = database
        self._budget = budget
        self._calls: dict[str, int] = {}

    @staticmethod
    def _worst_case(tool_name: str) -> dict[str, int]:
        worst = {"tool_calls": 1}
        if tool_name == "search.web":
            worst["search_calls"] = 1
        if tool_name in {"http.fetch", "browser.read"}:
            worst["page_reads"] = 1
        return worst

    async def begin(
        self, *, root_run_id: str, attempt_id: str, tool_name: str,
    ) -> ToolCallGrant:
        if not root_run_id or not attempt_id:
            raise AppError(
                code="BUDGET_LEDGER_NOT_FOUND",
                message="a tool call needs its run and attempt",
                status=404,
            )
        sequence = self._calls.get(attempt_id, 0) + 1
        operation_id = f"{attempt_id}:tool:{sequence}"
        worst = self._worst_case(tool_name)
        async with self._database.session() as session:
            ledger = await session.scalar(
                select(BudgetLedger).where(BudgetLedger.root_run_id == root_run_id)
            )
            if ledger is None:
                raise AppError(
                    code="BUDGET_LEDGER_NOT_FOUND",
                    message="the run has no budget ledger",
                    status=404,
                )
            await self._budget.reserve(
                session, ledger=ledger, worst_case=worst, operation_id=operation_id,
            )
            await session.commit()
        self._calls[attempt_id] = sequence
        return ToolCallGrant(operation_id, worst)

    async def settle(self, *, root_run_id: str, grant: ToolCallGrant) -> None:
        async with self._database.session() as session:
            ledger = await session.scalar(
                select(BudgetLedger).where(BudgetLedger.root_run_id == root_run_id)
            )
            if ledger is None:
                raise AppError(
                    code="BUDGET_LEDGER_NOT_FOUND",
                    message="the run has no budget ledger",
                    status=404,
                )
            await self._budget.settle(
                session,
                ledger=ledger,
                reserved=grant.reserved,
                actual=grant.reserved,
                operation_id=grant.reservation_id,
            )
            await session.commit()

    async def release(self, *, root_run_id: str, grant: ToolCallGrant) -> None:
        async with self._database.session() as session:
            ledger = await session.scalar(
                select(BudgetLedger).where(BudgetLedger.root_run_id == root_run_id)
            )
            if ledger is None:
                return
            await self._budget.release(
                session,
                ledger=ledger,
                reserved=grant.reserved,
                operation_id=grant.reservation_id,
            )
            await session.commit()

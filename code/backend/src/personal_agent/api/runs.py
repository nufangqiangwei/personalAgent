"""Run REST endpoints (/api/v1/runs).

Starting a run only consumes a confirmed snapshot and submits the engine task; the
HTTP handler never advances step state itself.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, tuple_

from personal_agent.adapters.persistence.execution_models import WorkflowRun
from personal_agent.api.deps import context
from personal_agent.application.run_snapshots import serialize_run, serialize_steps
from personal_agent.domain.common import AppError, iso_z, parse_utc
from personal_agent.domain.pagination import decode_cursor, encode_cursor

router = APIRouter(prefix="/api/v1", tags=["runs"])


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_id: str = Field(min_length=1)


@router.post("/runs", status_code=201)
async def start_run(payload: StartRunRequest, request: Request) -> dict[str, Any]:
    app_context = context(request)
    snapshot = await app_context.coordinator.start(payload.confirmation_id)
    app_context.executor.submit(str(snapshot["id"]))
    return snapshot


@router.get("/runs")
async def list_runs(
    request: Request,
    root_run_id: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    app_context = context(request)
    # Page in SQL, not in Python: serializing every run before slicing is
    # quadratic at 10k executions (each snapshot costs several queries).
    limit = max(1, min(limit, 100))
    filter_key = f"runs:{root_run_id or ''}"
    statement = (
        select(WorkflowRun)
        .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
        .limit(limit + 1)
    )
    if root_run_id is not None:
        statement = statement.where(WorkflowRun.root_run_id == root_run_id)
    if cursor:
        sort_key, item_id = decode_cursor(cursor, filter_key=filter_key)
        try:
            sort_at = parse_utc(sort_key)
        except ValueError as error:
            raise AppError(
                code="INVALID_CURSOR", message="cursor timestamp is invalid", status=400
            ) from error
        statement = statement.where(
            tuple_(WorkflowRun.created_at, WorkflowRun.id) < (sort_at, item_id)
        )
    async with app_context.database.session() as session:
        runs = (await session.scalars(statement)).all()
        page = runs[:limit]
        snapshots = [await serialize_run(session, run) for run in page]
    next_cursor = (
        encode_cursor(
            sort_key=iso_z(page[-1].created_at),
            item_id=page[-1].id,
            filter_key=filter_key,
        )
        if len(runs) > limit and page
        else None
    )
    return {"items": snapshots, "next_cursor": next_cursor}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    app_context = context(request)
    async with app_context.database.session() as session:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            raise AppError(code="RUN_NOT_FOUND", message="run not found", status=404)
        return await serialize_run(session, run)


@router.get("/runs/{run_id}/steps")
async def get_run_steps(run_id: str, request: Request) -> dict[str, Any]:
    app_context = context(request)
    async with app_context.database.session() as session:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            raise AppError(code="RUN_NOT_FOUND", message="run not found", status=404)
        return await serialize_steps(session, run_id)

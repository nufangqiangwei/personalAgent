"""Committed execution events over SSE, plus a paged history backfill.

Only committed events are published, each with its run-wide sequence. A client first
reads a snapshot and subscribes after ``last_event_sequence`` so no gap exists between
the snapshot and the stream. Replayed events are safe: replay never re-executes an
action or re-settles a budget. A slow consumer reads at most 1000 events per poll and
never holds a database transaction while waiting; if the cursor it asked for was
already cleaned up, the stream sends a ``stream.reset`` control frame.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.execution_models import (
    ExecutionEvent,
    WorkflowRun,
)
from personal_agent.api.deps import context
from personal_agent.domain.common import AppError, iso_z

router = APIRouter(prefix="/api/v1", tags=["events"])

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000
MAX_BUFFER = 1000
HEARTBEAT_SECONDS = 15.0
POLL_SECONDS = 0.25


def _frame(event: ExecutionEvent) -> str:
    data = {
        "schema_version": event.schema_version,
        "event_id": event.id,
        "run_id": event.run_id,
        "draft_id": event.draft_id,
        "step_run_id": event.step_run_id,
        "attempt_id": event.attempt_id,
        "workflow_version_id": event.workflow_version_id,
        "sequence": event.sequence,
        "occurred_at": iso_z(event.occurred_at),
        "status": event.status,
        "reason_code": event.reason_code,
        "payload": event.payload_json,
    }
    payload = json.dumps(data, ensure_ascii=False)
    return f"id: {event.sequence}\nevent: {event.kind}\ndata: {payload}\n\n"


def _reset_frame(snapshot_url: str) -> str:
    data = {"event": "stream.reset", "snapshot_url": snapshot_url}
    return f"event: stream.reset\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _require_run(database: Database, run_id: str) -> WorkflowRun:
    async with database.session() as session:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            raise AppError(code="RUN_NOT_FOUND", message="run not found", status=404)
        return run


async def _events_after(
    database: Database, run_id: str, cursor: int, limit: int = MAX_BUFFER
) -> list[ExecutionEvent]:
    async with database.session() as session:
        rows = (
            await session.scalars(
                select(ExecutionEvent)
                .where(ExecutionEvent.run_id == run_id, ExecutionEvent.sequence > cursor)
                .order_by(ExecutionEvent.sequence)
                .limit(min(limit, MAX_BUFFER))
            )
        ).all()
        return list(rows)


async def _sequence_bounds(database: Database, run_id: str) -> tuple[int | None, int | None]:
    async with database.session() as session:
        lowest = await session.scalar(
            select(func.min(ExecutionEvent.sequence)).where(ExecutionEvent.run_id == run_id)
        )
        highest = await session.scalar(
            select(func.max(ExecutionEvent.sequence)).where(ExecutionEvent.run_id == run_id)
        )
        return (
            int(lowest) if lowest is not None else None,
            int(highest) if highest is not None else None,
        )


async def event_frames(
    database: Database,
    run_id: str,
    *,
    cursor: int | None,
    is_disconnected: Callable[[], Awaitable[bool]],
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> AsyncIterator[str]:
    """Yield committed event frames until the consumer disconnects.

    ``is_disconnected`` is injected so this cooperative loop can be driven without a
    real socket; the HTTP route passes the request's own check.
    """
    snapshot_url = f"/api/v1/runs/{run_id}"
    if cursor is None:
        # Read the snapshot sequence, then subscribe strictly after it.
        async with database.session() as session:
            value = await session.scalar(
                select(func.max(ExecutionEvent.sequence)).where(
                    ExecutionEvent.run_id == run_id
                )
            )
        cursor = int(value or 0)
    lowest, highest = await _sequence_bounds(database, run_id)
    if lowest is not None and highest is not None and cursor < lowest - 1:
        yield _reset_frame(snapshot_url)
        cursor = max(cursor, highest)

    loop = asyncio.get_running_loop()
    last_sent = loop.time()
    while True:
        if await is_disconnected():
            return
        events = await _events_after(database, run_id, cursor)
        if events:
            for event in events:
                yield _frame(event)
                cursor = max(cursor, event.sequence)
            last_sent = loop.time()
            continue
        if loop.time() - last_sent >= heartbeat_seconds:
            yield ": heartbeat\n\n"
            last_sent = loop.time()
        await asyncio.sleep(poll_seconds)


@router.get("/runs/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    after: int | None = Query(default=None, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    app_context = context(request)
    await _require_run(app_context.database, run_id)

    cursor: int | None = None
    if last_event_id:
        try:
            cursor = int(last_event_id)
        except ValueError as error:
            raise AppError(
                code="EVENT_CURSOR_INVALID",
                message="Last-Event-ID must be a run sequence number",
                status=400,
            ) from error
    if cursor is None:
        cursor = after

    async def generator() -> AsyncIterator[str]:
        async for frame in event_frames(
            app_context.database,
            run_id,
            cursor=cursor,
            is_disconnected=request.is_disconnected,
        ):
            yield frame

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/runs/{run_id}/event-history")
async def event_history(
    run_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> dict[str, Any]:
    """Paged backfill in the same shape as the stream; never re-executes actions."""
    app_context = context(request)
    await _require_run(app_context.database, run_id)
    page_size = min(limit, MAX_PAGE_SIZE)
    rows = await _events_after(app_context.database, run_id, after, limit=page_size + 1)
    page = rows[:page_size]
    next_cursor = page[-1].sequence if len(rows) > page_size and page else None
    return {
        "items": [
            {
                "id": event.sequence,
                "event": event.kind,
                "data": {
                    "schema_version": event.schema_version,
                    "event_id": event.id,
                    "run_id": event.run_id,
                    "step_run_id": event.step_run_id,
                    "attempt_id": event.attempt_id,
                    "workflow_version_id": event.workflow_version_id,
                    "sequence": event.sequence,
                    "occurred_at": iso_z(event.occurred_at),
                    "status": event.status,
                    "reason_code": event.reason_code,
                    "payload": event.payload_json,
                },
            }
            for event in page
        ],
        "next_cursor": next_cursor,
        "snapshot_url": f"/api/v1/runs/{run_id}",
    }


__all__ = ["router"]

"""Run controls: pause, cancel, resume, recovery report and trusted history.

The handler records the intent and returns at once so the UI gets feedback quickly;
the run only becomes PAUSED/CANCELLED after the current action has converged.
Resuming re-verifies the frozen run and appends a new epoch to the same run id.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from personal_agent.adapters.persistence.execution_models import WorkflowRun
from personal_agent.api.deps import context, require_if_match, revision_etag
from personal_agent.application.run_snapshots import serialize_run, serialize_steps
from personal_agent.domain.common import AppError

router = APIRouter(prefix="/api/v1", tags=["run-controls"])


class PauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=500)


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=500)
    confirmed: bool = False


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)


def _with_etag(snapshot: dict[str, Any], response: Response) -> dict[str, Any]:
    response.headers.update(revision_etag(int(snapshot["revision"])))
    return snapshot


@router.post("/runs/{run_id}/pause")
async def pause_run(
    run_id: str, payload: PauseRequest, request: Request, response: Response
) -> dict[str, Any]:
    app_context = context(request)
    require_if_match(request, payload.revision, code="RUN_REVISION_STALE")
    snapshot = await app_context.control.request_pause(
        run_id, expected_revision=payload.revision, reason=payload.reason
    )
    return _with_etag(snapshot, response)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str, payload: CancelRequest, request: Request, response: Response
) -> dict[str, Any]:
    app_context = context(request)
    require_if_match(request, payload.revision, code="RUN_REVISION_STALE")
    snapshot = await app_context.control.request_cancel(
        run_id,
        expected_revision=payload.revision,
        reason=payload.reason,
        confirmed=payload.confirmed,
    )
    return _with_etag(snapshot, response)


@router.post("/runs/{run_id}/resume")
async def resume_run(
    run_id: str, payload: ResumeRequest, request: Request, response: Response
) -> dict[str, Any]:
    app_context = context(request)
    require_if_match(request, payload.revision, code="RUN_REVISION_STALE")
    snapshot = await app_context.recovery.resume(
        run_id, expected_revision=payload.revision
    )
    return _with_etag(snapshot, response)


@router.get("/runs/{run_id}/recovery")
async def recovery_report(run_id: str, request: Request) -> dict[str, Any]:
    app_context = context(request)
    return await app_context.recovery.inspect(run_id)


@router.get("/runs/{run_id}/history")
async def run_history(run_id: str, request: Request) -> dict[str, Any]:
    """Root, parent, branches and each branch's artifacts; no Git knowledge needed."""
    app_context = context(request)
    async with app_context.database.session() as session:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            raise AppError(code="RUN_NOT_FOUND", message="run not found", status=404)
        runs = list(
            (
                await session.scalars(
                    select(WorkflowRun)
                    .where(WorkflowRun.root_run_id == run.root_run_id)
                    .order_by(WorkflowRun.created_at)
                )
            ).all()
        )
        items = [await serialize_run(session, item) for item in runs]
        return {
            "root_run_id": run.root_run_id,
            "items": [
                {
                    "id": item["id"],
                    "parent_run_id": item["parent_run_id"],
                    "fork_step_id": item["fork_step_id"],
                    "status": item["status"],
                    "reason_code": item["reason_code"],
                    "workflow_version_id": item["workflow_version_id"],
                    "completeness": item["completeness"],
                    "artifact_ids": item["artifact_ids"],
                    "steps": [
                        {
                            "step_id": step["step_id"],
                            "status": step["status"],
                            "selected_transition": step["selected_transition"],
                        }
                        for step in (await serialize_steps(session, item["id"]))["items"]
                    ],
                    "created_at": item["created_at"],
                }
                for item in items
            ],
        }

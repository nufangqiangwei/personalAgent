"""Artifact REST endpoints (/api/v1/runs/{id}/artifacts, /api/v1/artifacts)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

from personal_agent.adapters.persistence.execution_models import WorkflowRun
from personal_agent.api.deps import context
from personal_agent.domain.common import AppError

router = APIRouter(prefix="/api/v1", tags=["artifacts"])


@router.get("/runs/{run_id}/artifacts")
async def list_run_artifacts(run_id: str, request: Request) -> dict[str, Any]:
    app_context = context(request)
    async with app_context.database.session() as session:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            raise AppError(code="RUN_NOT_FOUND", message="run not found", status=404)
    items = await app_context.artifacts.list_for_run(run_id)
    return {"items": items}


@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str, request: Request, response: Response) -> dict[str, Any]:
    app_context = context(request)
    metadata = await app_context.artifacts.get(artifact_id)
    response.headers["ETag"] = f'"{metadata["manifest_hash"]}"'
    return metadata


@router.get("/artifacts/{artifact_id}/content")
async def get_artifact_content(artifact_id: str, request: Request) -> Response:
    app_context = context(request)
    data, metadata = await app_context.artifacts.content(artifact_id)
    return Response(
        content=data,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{metadata["filename"]}"',
            "ETag": f'"{metadata["sha256"]}"',
            "X-Content-SHA256": metadata["sha256"],
            "Cache-Control": "no-store",
        },
    )

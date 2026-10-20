"""Workflow registry REST endpoints (/api/v1/workflows)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from personal_agent.api.context import AppContext
from personal_agent.api.deps import context, require_if_match, revision_etag
from personal_agent.application.workflow_registry import (
    WorkflowRegistry,
    serialize_version,
    serialize_workflow,
)
from personal_agent.domain.pagination import decode_cursor, encode_cursor

router = APIRouter(prefix="/api/v1", tags=["workflows"])


class VersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version_id: str = Field(min_length=1)
    definition: dict[str, Any]
    change_summary: str = Field(min_length=1, max_length=1000)
    confirm_executable: bool


class DisableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=400)


def registry(app_context: AppContext) -> WorkflowRegistry:
    return WorkflowRegistry(app_context.database, app_context.clock)


@router.get("/workflows")
async def list_workflows(
    request: Request, cursor: str | None = None, limit: int = 20
) -> dict[str, Any]:
    app_context = context(request)
    page_limit = max(1, min(limit, 100))
    summaries = sorted(
        await registry(app_context).list_summaries(),
        key=lambda item: (str(item.get("name", "")), str(item.get("id", ""))),
    )
    filter_key = "workflows"
    if cursor:
        name, workflow_id = decode_cursor(cursor, filter_key=filter_key)
        summaries = [
            item for item in summaries
            if (str(item.get("name", "")), str(item.get("id", ""))) > (name, workflow_id)
        ]
    page = summaries[:page_limit]
    next_cursor = (
        encode_cursor(
            sort_key=str(page[-1].get("name", "")),
            item_id=str(page[-1].get("id", "")),
            filter_key=filter_key,
        )
        if len(summaries) > page_limit and page
        else None
    )
    return {"items": page, "next_cursor": next_cursor}


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str, request: Request, response: Response) -> dict[str, Any]:
    app_context = context(request)
    summary = await registry(app_context).get_workflow(workflow_id)
    response.headers.update(revision_etag(int(summary["revision"])))
    return summary


@router.get("/workflows/{workflow_id}/versions")
async def list_versions(
    workflow_id: str, request: Request, cursor: str | None = None, limit: int = 20
) -> dict[str, Any]:
    app_context = context(request)
    versions = await registry(app_context).list_versions(workflow_id)
    page_limit = max(1, min(limit, 100))
    items = sorted(
        [serialize_version(version) for version in versions],
        key=lambda item: (int(item.get("version_no", 0)), str(item.get("id", ""))),
        reverse=True,
    )
    filter_key = f"workflow-versions:{workflow_id}"
    if cursor:
        sort_key, item_id = decode_cursor(cursor, filter_key=filter_key)
        items = [
            item for item in items
            if (f"{int(item.get('version_no', 0)):010d}", str(item.get("id", "")))
            < (sort_key, item_id)
        ]
    page = items[:page_limit]
    next_cursor = (
        encode_cursor(
            sort_key=f"{int(page[-1].get('version_no', 0)):010d}",
            item_id=str(page[-1].get("id", "")),
            filter_key=filter_key,
        )
        if len(items) > page_limit and page
        else None
    )
    return {"items": page, "next_cursor": next_cursor}


@router.get("/workflows/{workflow_id}/versions/{version_id}")
async def get_version(
    workflow_id: str, version_id: str, request: Request, response: Response
) -> dict[str, Any]:
    app_context = context(request)
    version = await registry(app_context).get_version(workflow_id, version_id)
    response.headers.update({"ETag": f'"{version.definition_hash}"'})
    return serialize_version(version)


@router.post("/workflows/{workflow_id}/versions", status_code=201)
async def save_version(
    workflow_id: str, payload: VersionRequest, request: Request, response: Response
) -> dict[str, Any]:
    app_context = context(request)
    outcome = await registry(app_context).save_version(
        workflow_id=workflow_id,
        base_version_id=payload.base_version_id,
        definition=payload.definition,
        change_summary=payload.change_summary,
        confirm_executable=payload.confirm_executable,
    )
    response.status_code = 201 if outcome.created else 200
    response.headers.update({"ETag": f'"{outcome.version.definition_hash}"'})
    body = serialize_version(outcome.version)
    body["created"] = outcome.created
    body["promoted"] = outcome.promoted
    return body


@router.post("/workflows/{workflow_id}/disable")
async def disable_workflow(
    workflow_id: str, payload: DisableRequest, request: Request
) -> dict[str, Any]:
    app_context = context(request)
    workflow = await registry(app_context).get_workflow(workflow_id)
    require_if_match(request, int(workflow["revision"]), code="WORKFLOW_REVISION_STALE")
    disabled = await registry(app_context).disable(workflow_id, payload.reason)
    return serialize_workflow(disabled)

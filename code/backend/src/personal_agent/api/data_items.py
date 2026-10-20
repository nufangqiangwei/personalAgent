"""Saved data items, deletion records and retention status.

Deletion takes effect immediately for reads and reuse; the physical removal is
asynchronous and the record can be polled. Nothing here exposes content that was
deleted, and a shared blob is kept while another available artifact still needs it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from personal_agent.api.context import AppContext
from personal_agent.api.deps import context
from personal_agent.application.data_lifecycle import DataLifecycleService

router = APIRouter(prefix="/api/v1", tags=["data-lifecycle"])


class DeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_ids: list[str] = Field(min_length=1)
    acknowledge_resume_impact: bool = False
    reason: str | None = Field(default=None, max_length=2000)


def lifecycle_of(app_context: AppContext) -> DataLifecycleService:
    return DataLifecycleService(
        app_context.database,
        app_context.clock,
        app_context.writer.store,
        retention=app_context.config.policy.retention,
        data_dir=app_context.config.data_dir,
    )


@router.get("/data-items")
async def list_data_items(
    request: Request,
    type: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    app_context = context(request)
    return await lifecycle_of(app_context).list_items(
        kind=type, cursor=cursor, limit=min(max(limit, 1), 100)
    )


@router.post("/deletions", status_code=202)
async def request_deletion(payload: DeletionRequest, request: Request) -> dict[str, Any]:
    app_context = context(request)
    record = await lifecycle_of(app_context).request_deletion(
        payload.item_ids,
        acknowledge_resume_impact=payload.acknowledge_resume_impact,
        reason=payload.reason,
    )
    return {
        **record,
        "status_url": f"/api/v1/deletions/{record['id']}",
    }


@router.get("/deletions/{deletion_id}")
async def get_deletion(deletion_id: str, request: Request) -> dict[str, Any]:
    return await lifecycle_of(context(request)).get_deletion(deletion_id)


__all__ = ["router"]

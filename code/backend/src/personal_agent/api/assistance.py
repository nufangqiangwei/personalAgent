"""Assistance endpoints: read the request, record an explicit decision.

Silence, viewing or chatting never resumes a run; only an explicit decision does,
and a top-up plus "continue" are separate fields in the same request.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from personal_agent.api.deps import context, require_if_match, revision_etag
from personal_agent.domain.common import AppError

router = APIRouter(prefix="/api/v1", tags=["assistance"])


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    decision: str = Field(
        pattern="^(DISCUSS|GRANT_AND_CONTINUE|PAUSE|CANCEL|CHANGE_REQUIREMENTS|RECONCILE)$"
    )
    approved_increment: dict[str, int] | None = None
    continue_authorized: bool = False
    acknowledge_cost: bool = False
    review_result: dict[str, Any] | None = None
    note: str | None = Field(default=None, max_length=2000)
    evidence_refs: list[str] = Field(default_factory=list)


@router.get("/runs/{run_id}/assistance")
async def list_assistance(run_id: str, request: Request) -> dict[str, Any]:
    app_context = context(request)
    return {"items": await app_context.assistance.list_for_run(run_id)}


@router.get("/assistance/{assistance_id}")
async def get_assistance(assistance_id: str, request: Request) -> dict[str, Any]:
    app_context = context(request)
    return await app_context.assistance.get(assistance_id)


@router.post("/assistance/{assistance_id}/decisions")
async def decide(
    assistance_id: str,
    payload: DecisionRequest,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    return await _decide(assistance_id, payload, request, response)


@router.post("/runs/{run_id}/assistance/{assistance_id}/decisions")
async def decide_for_run(
    run_id: str,
    assistance_id: str,
    payload: DecisionRequest,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """Contract path: a decision always belongs to the run that raised the request."""
    app_context = context(request)
    existing = await app_context.assistance.get(assistance_id)
    if existing["run_id"] != run_id:
        raise AppError(
            code="ASSISTANCE_NOT_FOUND",
            message="assistance request not found",
            status=404,
        )
    return await _decide(assistance_id, payload, request, response)


async def _decide(
    assistance_id: str,
    payload: DecisionRequest,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    app_context = context(request)
    require_if_match(request, payload.revision, code="ASSISTANCE_REVISION_STALE")
    result = await app_context.assistance.decide(
        assistance_id,
        expected_revision=payload.revision,
        decision=payload.decision,
        approved_increment=payload.approved_increment,
        continue_authorized=payload.continue_authorized,
        acknowledge_cost=payload.acknowledge_cost,
        review_result=payload.review_result,
        note=payload.note,
        evidence_refs=payload.evidence_refs,
    )
    run = result.get("run")
    if isinstance(run, dict) and "revision" in run:
        response.headers.update(revision_etag(int(run["revision"])))
    return result

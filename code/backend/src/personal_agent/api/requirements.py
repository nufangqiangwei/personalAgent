"""Requirement draft REST endpoints (/api/v1/requirements, /api/v1/operations)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from personal_agent.api.context import AppContext
from personal_agent.api.deps import context, require_if_match, revision_etag
from personal_agent.application.requirement_analysis import RequirementAnalysisService
from personal_agent.application.requirement_confirmation import RequirementService
from personal_agent.domain.common import AppError

router = APIRouter(prefix="/api/v1", tags=["requirements"])


class CreateDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_version_id: str = Field(min_length=1)


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=20_000)


class SuggestionAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_revision: int = Field(ge=1)


class SelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_version_id: str = Field(min_length=1)
    draft_revision: int = Field(ge=1)


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_revision: int = Field(ge=1)
    approved_increment: dict[str, int]
    explicit_grant: bool
    acknowledge_cost_and_ledger: bool
    note: str | None = None


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_revision: int = Field(ge=1)
    summary_hash: str = Field(min_length=1)
    workflow_version_id: str = Field(min_length=1)
    budget_policy_hash: str = Field(min_length=1)
    data_policy_version: str = Field(min_length=1)
    action: str = Field(pattern="^(START|BRANCH)$")
    explicit_start_confirmation: bool


def requirement_service(app_context: AppContext) -> RequirementService:
    policy = app_context.config.policy
    return RequirementService(
        app_context.database,
        app_context.clock,
        app_context.budget,
        policy_hash=policy.policy_hash,
        policy_version=policy.version,
        draft_limits=policy.draft.as_dict(),
        run_limits=policy.budget.as_dict(),
    )


def analysis_service(app_context: AppContext) -> RequirementAnalysisService:
    policy = app_context.config.policy
    return RequirementAnalysisService(
        app_context.database,
        app_context.clock,
        app_context.gateway,
        requirement_service(app_context),
        model_id=(
            "mock-deepseek-phase1"
            if app_context.config.mode == "mock"
            else "deepseek-v4-flash"
        ),
        timeout_seconds=float(policy.model.timeout_seconds),
        max_output_tokens=policy.model.max_output_tokens,
    )


@router.post("/requirements", status_code=201)
async def create_requirement(
    payload: CreateDraftRequest, request: Request
) -> dict[str, Any]:
    app_context = context(request)
    return await requirement_service(app_context).create_draft(payload.workflow_version_id)


@router.get("/requirements/{draft_id}")
async def get_requirement(
    draft_id: str, request: Request, response: Response
) -> dict[str, Any]:
    app_context = context(request)
    snapshot = await requirement_service(app_context).get(draft_id)
    response.headers.update(revision_etag(int(snapshot["revision"])))
    return snapshot


@router.get("/requirements/{draft_id}/budget")
async def get_draft_budget(draft_id: str, request: Request) -> dict[str, Any]:
    app_context = context(request)
    return await requirement_service(app_context).budget_snapshot(draft_id)


@router.post("/requirements/{draft_id}/budget-grants", status_code=201)
async def grant_draft_budget(
    draft_id: str, payload: GrantRequest, request: Request
) -> dict[str, Any]:
    app_context = context(request)
    require_if_match(request, payload.draft_revision, code="DRAFT_REVISION_STALE")
    key = request.headers.get("idempotency-key")
    if not key:
        raise AppError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="Idempotency-Key is required",
            status=400,
        )
    return await requirement_service(app_context).grant_budget(
        draft_id,
        draft_revision=payload.draft_revision,
        increment=payload.approved_increment,
        operation_id=key,
        note=payload.note,
        explicit_grant=payload.explicit_grant,
        acknowledge_cost_and_ledger=payload.acknowledge_cost_and_ledger,
    )


@router.post("/requirements/{draft_id}/messages", status_code=202)
async def add_message(
    draft_id: str, payload: MessageRequest, request: Request
) -> dict[str, Any]:
    app_context = context(request)
    operation = await analysis_service(app_context).analyze(draft_id, payload.text)
    return {
        "message_id": operation.get("message_id"),
        "analysis_operation_id": operation.get("id"),
        "status": operation.get("status"),
    }


@router.post("/requirements/{draft_id}/suggestions/{suggestion_id}/accept")
async def accept_suggestion(
    draft_id: str,
    suggestion_id: str,
    payload: SuggestionAcceptRequest,
    request: Request,
) -> dict[str, Any]:
    app_context = context(request)
    require_if_match(request, payload.draft_revision, code="DRAFT_REVISION_STALE")
    return await requirement_service(app_context).accept_suggestion(
        draft_id, suggestion_id, draft_revision=payload.draft_revision
    )


@router.post("/requirements/{draft_id}/suggestions/{suggestion_id}/reject")
async def reject_suggestion(
    draft_id: str,
    suggestion_id: str,
    payload: SuggestionAcceptRequest,
    request: Request,
) -> dict[str, Any]:
    app_context = context(request)
    require_if_match(request, payload.draft_revision, code="DRAFT_REVISION_STALE")
    return await requirement_service(app_context).reject_suggestion(
        draft_id, suggestion_id, draft_revision=payload.draft_revision
    )


@router.post("/requirements/{draft_id}/selection")
async def change_selection(
    draft_id: str, payload: SelectionRequest, request: Request
) -> dict[str, Any]:
    app_context = context(request)
    require_if_match(request, payload.draft_revision, code="DRAFT_REVISION_STALE")
    return await requirement_service(app_context).change_selection(
        draft_id, payload.workflow_version_id, draft_revision=payload.draft_revision
    )


@router.post("/requirements/{draft_id}/confirm", status_code=201)
async def confirm_requirement(
    draft_id: str, payload: ConfirmRequest, request: Request
) -> dict[str, Any]:
    app_context = context(request)
    require_if_match(request, payload.draft_revision, code="DRAFT_REVISION_STALE")
    return await requirement_service(app_context).confirm(
        draft_id,
        draft_revision=payload.draft_revision,
        summary_hash=payload.summary_hash,
        workflow_version_id=payload.workflow_version_id,
        budget_policy_hash=payload.budget_policy_hash,
        data_policy_version=payload.data_policy_version,
        action=payload.action,
        explicit_start_confirmation=payload.explicit_start_confirmation,
    )


@router.get("/operations/{operation_id}")
async def get_operation(operation_id: str, request: Request) -> dict[str, Any]:
    app_context = context(request)
    return await analysis_service(app_context).operation(operation_id)

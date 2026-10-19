"""Health and policy endpoints.

``/health`` reports process liveness separately from whether execution is
available. It never contains credentials or a claim that the real runtime passed
its adoption gate.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from personal_agent.api.context import AppContext

router = APIRouter()


def context_of(request: Request) -> AppContext:
    context = getattr(request.app.state, "context", None)
    if context is None:  # pragma: no cover - lifespan always sets it
        from personal_agent.domain.common import AppError

        raise AppError(
            code="NOT_READY",
            message="the application is still starting",
            status=503,
        )
    return context


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    context = context_of(request)
    database_ok = True
    database_detail = "ok"
    try:
        await context.database.integrity_check()
    except Exception as error:  # pragma: no cover - defensive
        database_ok = False
        database_detail = type(error).__name__
    return {
        "status": "ok" if database_ok else "degraded",
        "mode": context.config.mode,
        "dependency_checks": {
            "database": {"status": database_detail},
            "migrations": {
                "status": "ok" if context.migrations_up_to_date else "pending",
                "current": context.migration_current,
                "head": context.migration_head,
            },
            "runtime": {
                "status": "ok" if context.runtime_capabilities else "unavailable",
                "compatible": bool(context.runtime_capabilities.get("tool_policy_enforced")),
            },
        },
        "execution_available": context.execution_available,
    }


@router.get("/policies")
async def policies(request: Request) -> dict[str, Any]:
    context = context_of(request)
    policy = context.config.policy
    document = policy.as_document()
    return {
        "version": policy.version,
        "hash": policy.policy_hash,
        "retention": document["retention"],
        "budget_defaults": document["budget_defaults"],
        "draft_budget_defaults": document["draft_budget_defaults"],
        "storage_scope": policy.storage_scope,
        "allowed_actions": [
            {"action": "ACKNOWLEDGE_POLICIES", "enabled": True, "disabled_reason": None},
        ],
    }

"""Shared request dependencies: ETag/If-Match and the local app context."""

from __future__ import annotations

from fastapi import Request

from personal_agent.api.context import AppContext
from personal_agent.domain.common import (
    AppError,
    ErrorCategory,
    etag,
    precondition_failed,
    precondition_required,
)


def context(request: Request) -> AppContext:
    app_context = getattr(request.app.state, "context", None)
    if app_context is None:  # pragma: no cover - lifespan always sets it
        raise AppError(
            code="NOT_READY", message="the application is still starting", status=503
        )
    return app_context


def require_if_match(request: Request, expected_revision: int, *, code: str) -> int:
    """Validate the If-Match header against the body revision.

    Missing header -> 428; mismatch (header, body or current state) -> 412.
    """
    header = request.headers.get("if-match")
    if header is None or header.strip() == "":
        raise precondition_required()
    try:
        header_revision = int(header.strip().strip('"'))
    except ValueError as error:
        raise precondition_failed(code) from error
    if header_revision != expected_revision:
        raise precondition_failed(code)
    return header_revision


def revision_etag(revision: int) -> dict[str, str]:
    return {"ETag": etag(revision)}


def conflict(code: str, message: str) -> AppError:
    return AppError(
        code=code,
        message=message,
        category=ErrorCategory.VALIDATION,
        status=409,
    )

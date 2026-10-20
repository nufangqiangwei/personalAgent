"""Structured, redacted error responses."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from personal_agent.domain.common import AppError
from personal_agent.domain.redaction import sanitize

logger = logging.getLogger("personal_agent.api")

_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    410: "GONE",
    412: "PRECONDITION_FAILED",
    413: "PAYLOAD_TOO_LARGE",
    422: "UNPROCESSABLE_ENTITY",
    428: "PRECONDITION_REQUIRED",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "unknown"))


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, error: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status,
            content=sanitize(error.payload(_request_id(request))),
            headers={"X-Request-ID": _request_id(request)},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "location": [str(part) for part in item.get("loc", ())],
                "type": str(item.get("type", "invalid")),
                "message": str(item.get("msg", "invalid value")),
            }
            for item in error.errors()
        ]
        payload = AppError(
            code="REQUEST_INVALID",
            message="the request body or query is not valid",
            status=400,
            details={"violations": details},
        )
        return JSONResponse(
            status_code=400,
            content=sanitize(payload.payload(_request_id(request))),
            headers={"X-Request-ID": _request_id(request)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(
        request: Request, error: StarletteHTTPException
    ) -> JSONResponse:
        code = _STATUS_CODES.get(error.status_code, "HTTP_ERROR")
        payload = AppError(
            code=code,
            message=str(error.detail) if error.detail else code,
            status=error.status_code,
        )
        return JSONResponse(
            status_code=error.status_code,
            content=sanitize(payload.payload(_request_id(request))),
            headers={"X-Request-ID": _request_id(request)},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, error: Exception) -> JSONResponse:
        logger.exception(
            "unhandled error",
            extra={"request_id": _request_id(request), "path": request.url.path},
        )
        payload = AppError(
            code="INTERNAL_ERROR",
            message="the request failed unexpectedly",
            status=500,
        )
        return JSONResponse(
            status_code=500,
            content=payload.payload(_request_id(request)),
            headers={"X-Request-ID": _request_id(request)},
        )

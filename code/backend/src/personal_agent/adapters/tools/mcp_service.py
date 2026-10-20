"""Managed tool service (MCP boundary).

The service is the runtime-facing tool boundary: it validates the scope token,
epoch, stop flag and argument shape before dispatching to a restricted handler.
Handlers never expose management APIs, shells, arbitrary scripts or outbound
writes. An empty domain list denies network access rather than allowing it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from personal_agent.adapters.tools.browser_read import BrowserReader
from personal_agent.adapters.tools.extract import ContentExtractor
from personal_agent.adapters.tools.http_fetch import HttpFetcher, fetch_result
from personal_agent.adapters.tools.scope import ToolScopeRegistry
from personal_agent.adapters.tools.search import BraveSearchTool
from personal_agent.domain.common import AppError, ErrorCategory
from personal_agent.ports.contracts import Clock, ToolRequest, ToolResult

TOOL_NAMES = {"search.web", "http.fetch", "browser.read", "content.extract"}
FORBIDDEN_PREFIXES = ("api.", "admin.", "shell", "fs.", "file.", "plugin.", "agent.")
TOOL_ARGUMENTS: dict[str, set[str]] = {
    "search.web": {"query"},
    "http.fetch": {"url"},
    "browser.read": {"url"},
    "content.extract": {"html", "schema"},
}


@dataclass
class ToolHandlers:
    search: BraveSearchTool | None = None
    fetch: HttpFetcher | None = None
    browser: BrowserReader | None = None
    extract: ContentExtractor | None = None


class ToolService:
    def __init__(
        self,
        handlers: ToolHandlers,
        scopes: ToolScopeRegistry,
        clock: Clock,
        *,
        should_stop: Callable[[], bool] | None = None,
        budget_guard: Callable[[], None] | None = None,
        context_check: Callable[[Any], Awaitable[bool]] | None = None,
    ) -> None:
        self._handlers = handlers
        self._scopes = scopes
        self._clock = clock
        self._should_stop = should_stop or (lambda: False)
        self._budget_guard = budget_guard
        self._context_check = context_check

    async def execute(self, request: ToolRequest) -> ToolResult:
        if request.deadline <= self._clock.now():
            raise AppError(
                code="TOOL_DEADLINE_EXPIRED",
                message="the tool deadline has expired; no new action may start",
                category=ErrorCategory.TIMEOUT,
                status=504,
            )
        if self._should_stop():
            raise AppError(
                code="INTERRUPTED",
                message="a stop request blocks new tool actions",
                category=ErrorCategory.INTERRUPTED,
                status=409,
            )
        normalized = request.tool_name.strip().lower()
        if normalized not in TOOL_NAMES:
            raise AppError(
                code="TOOL_SCOPE_DENIED",
                message=f"tool {request.tool_name!r} is not registered",
                category=ErrorCategory.PERMISSION,
                status=403,
            )
        if any(normalized.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
            raise AppError(
                code="TOOL_SCOPE_DENIED",
                message="management, shell and filesystem tools are forbidden",
                category=ErrorCategory.PERMISSION,
                status=403,
            )
        grant = self._scopes.validate(
            token=request.scope_token,
            tool_name=normalized,
            epoch=request.epoch,
            now=self._clock.now(),
        )
        # The token must belong to the requesting run and attempt, and that
        # attempt must still be the persisted active one (stop/attempt change/
        # resume revokes the old authorization).
        if request.run_id != grant.run_id or request.attempt_id != grant.attempt_id:
            raise AppError(
                code="SCOPE_TOKEN_REVOKED",
                message="the scope token does not belong to this run and attempt",
                category=ErrorCategory.PERMISSION,
                status=403,
            )
        if self._context_check is not None and not await self._context_check(grant):
            raise AppError(
                code="SCOPE_TOKEN_REVOKED",
                message="the run or attempt is no longer active",
                category=ErrorCategory.PERMISSION,
                status=403,
            )
        if self._budget_guard is not None:
            self._budget_guard()
        allowed_domains = grant.allowed_domains
        args = dict(request.args)
        allowed_args = TOOL_ARGUMENTS.get(normalized, set())
        unknown = sorted(set(args) - allowed_args)
        if unknown:
            raise AppError(
                code="TOOL_ARGUMENTS_INVALID",
                message="unknown tool arguments are rejected",
                status=422,
                details={"unknown": unknown},
            )

        if normalized == "search.web":
            if self._handlers.search is None:
                raise AppError(
                    code="TOOL_UNAVAILABLE",
                    message="the search tool is not configured",
                    category=ErrorCategory.DEPENDENCY,
                    status=503,
                )
            _require_args(args, ("query",))
            return await self._handlers.search.execute(args, allowed_domains=allowed_domains)
        if normalized == "http.fetch":
            if self._handlers.fetch is None:
                raise AppError(
                    code="TOOL_UNAVAILABLE",
                    message="the HTTP tool is not configured",
                    category=ErrorCategory.DEPENDENCY,
                    status=503,
                )
            _require_args(args, ("url",))
            payload = await self._handlers.fetch.fetch(
                str(args["url"]), allowed_domains=allowed_domains
            )
            return fetch_result(payload)
        if normalized == "browser.read":
            if self._handlers.browser is None:
                raise AppError(
                    code="TOOL_UNAVAILABLE",
                    message="the browser tool is not configured",
                    category=ErrorCategory.DEPENDENCY,
                    status=503,
                )
            _require_args(args, ("url",))
            return await self._handlers.browser.execute(args, allowed_domains=allowed_domains)
        if self._handlers.extract is None:
            raise AppError(
                code="TOOL_UNAVAILABLE",
                message="the extract tool is not configured",
                category=ErrorCategory.DEPENDENCY,
                status=503,
            )
        _require_args(args, ("html",))
        parsed: dict[str, Any] = await self._handlers.extract.extract(
            html=str(args["html"]), schema=args.get("schema")
        )
        return ToolResult(
            status="ok",
            result_ref=str(parsed.get("char_count", 0)),
            source_refs=[],
            usage=_usage(normalized),
            content=parsed,
        )


def _require_args(args: dict[str, Any], required: tuple[str, ...]) -> None:
    for name in required:
        value = args.get(name)
        if not isinstance(value, str) or not value.strip():
            raise AppError(
                code="TOOL_ARGUMENTS_INVALID",
                message=f"argument {name!r} is required",
                status=422,
            )


def _usage(tool_name: str) -> Any:
    from personal_agent.ports.contracts import Usage

    return Usage(
        tool_calls=1,
        search_calls=1 if tool_name == "search.web" else 0,
        page_reads=1 if tool_name in {"http.fetch", "browser.read"} else 0,
    )

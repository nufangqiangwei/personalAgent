"""Deterministic tool service for Mock mode.

The real adapters (search/HTTP/browser/extract) are added by T054–T056. This mock
replays the fixed observations so the engine and UI can be developed and tested
offline. It never opens a network connection and still rejects invalid epochs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from personal_agent.domain.common import AppError, ErrorCategory, stable_hash
from personal_agent.ports.contracts import Clock, ToolRequest, ToolResult, Usage

ALLOWED_TOOLS = {"search.web", "http.fetch", "browser.read", "content.extract"}


class MockToolService:
    def __init__(self, observations: Mapping[str, Any], clock: Clock) -> None:
        self._observations = observations
        self._clock = clock

    async def execute(self, request: ToolRequest) -> ToolResult:
        if request.epoch < 1:
            raise AppError(
                code="TOOL_SCOPE_DENIED",
                message="a stale epoch cannot execute tools",
                category=ErrorCategory.PERMISSION,
                status=403,
            )
        if request.tool_name not in ALLOWED_TOOLS:
            raise AppError(
                code="TOOL_SCOPE_DENIED",
                message=f"tool {request.tool_name!r} is not allowed",
                category=ErrorCategory.PERMISSION,
                status=403,
            )
        if request.tool_name == "search.web":
            query = str(request.args.get("query") or "")
            queries = self._observations.get("search", {}).get("queries", {})
            entry: dict[str, Any] = queries.get(query) or next(
                iter(queries.values()), {"results": []}
            )
            urls = [item["url"] for item in entry.get("results", [])]
            return ToolResult(
                status="ok",
                result_ref=stable_hash({"search": query, "urls": urls}),
                source_refs=urls,
                usage=Usage(tool_calls=1, search_calls=1),
                content={"query": query, "results": entry.get("results", [])},
            )
        pages = self._observations.get("http", {}).get("pages", {})
        url = str(request.args.get("url") or "")
        page = pages.get(url) or next(iter(pages.values()), None)
        if page is None:
            return ToolResult(
                status="error",
                error=AppError(
                    code="SOURCE_UNAVAILABLE",
                    message="no fixture page is available",
                    category=ErrorCategory.DEPENDENCY,
                ),
            )
        return ToolResult(
            status="ok",
            result_ref=stable_hash({"page": page.get("source_id"), "body": page.get("body")}),
            source_refs=[str(page.get("source_id", url))],
            usage=Usage(
                tool_calls=1,
                page_reads=1 if request.tool_name in {"http.fetch", "browser.read"} else 0,
            ),
            content={
                "url": url,
                "source_id": page.get("source_id"),
                "title": page.get("title"),
                "body": page.get("body"),
            },
        )


class UnavailableToolService:
    """Live-mode placeholder until the restricted adapters pass adoption."""

    async def execute(self, request: ToolRequest) -> ToolResult:
        return ToolResult(
            status="error",
            error=AppError(
                code="RUNTIME_INCOMPATIBLE",
                message="real tool adapters are not enabled in this build",
                category=ErrorCategory.DEPENDENCY,
            ),
        )

"""Brave Search adapter.

The API key is read from the controlled credential provider and never logged.
If the key is absent the tool fails with ``CREDENTIAL_UNAVAILABLE`` instead of
scraping a search engine or silently returning fabricated results.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from personal_agent.adapters.tools.scope import domain_allowed, validate_url
from personal_agent.domain.common import AppError, ErrorCategory, stable_hash
from personal_agent.ports.contracts import ToolResult, Usage

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


@dataclass
class BraveSearchTool:
    client: httpx.AsyncClient
    api_key_provider: Callable[[], str | None]
    resolver: Callable[[str], Iterable[str]] | None = None
    max_results: int = 8

    async def execute(
        self, args: dict[str, Any], *, allowed_domains: tuple[str, ...]
    ) -> ToolResult:
        api_key = self.api_key_provider()
        if not api_key:
            raise AppError(
                code="CREDENTIAL_UNAVAILABLE",
                message="the search credential is not configured",
                category=ErrorCategory.PERMISSION,
                status=503,
            )
        query = str(args.get("query") or "").strip()
        if not query:
            raise AppError(code="TOOL_ARGUMENTS_INVALID", message="query is required", status=422)
        if not allowed_domains:
            raise AppError(
                code="DOMAIN_NOT_ALLOWED",
                message="an empty domain list denies network access",
                category=ErrorCategory.PERMISSION,
                status=403,
            )
        try:
            response = await self.client.get(
                BRAVE_ENDPOINT,
                params={
                    "q": query,
                    "count": min(int(args.get("count") or self.max_results), self.max_results),
                    "safesearch": "moderate",
                },
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": api_key,
                },
                timeout=10.0,
            )
        except httpx.HTTPError as error:
            raise AppError(
                code="SOURCE_UNAVAILABLE",
                message="the search provider is unavailable",
                category=ErrorCategory.DEPENDENCY,
                status=503,
            ) from error
        if response.status_code != 200:
            raise AppError(
                code="SOURCE_UNAVAILABLE",
                message=f"search provider returned {response.status_code}",
                category=ErrorCategory.DEPENDENCY,
                status=503,
            )
        payload = response.json()
        results: list[dict[str, Any]] = []
        for item in (payload.get("web", {}) or {}).get("results", [])[: self.max_results]:
            url = str(item.get("url") or "")
            try:
                validate_url(url, allowed_domains=allowed_domains, resolver=self.resolver)
            except AppError:
                # Results outside the authorized scope are not returned.
                continue
            results.append(
                {
                    "url": url,
                    "title": str(item.get("title") or ""),
                    "snippet": str(item.get("description") or ""),
                }
            )
        return ToolResult(
            status="ok",
            result_ref=stable_hash({"query": query, "results": results}),
            source_refs=[item["url"] for item in results],
            usage=Usage(tool_calls=1, search_calls=1),
        )


def filter_results(
    results: list[dict[str, Any]], allowed_domains: Iterable[str]
) -> list[dict[str, Any]]:
    return [item for item in results if domain_allowed(str(item.get("url", "")), allowed_domains)]

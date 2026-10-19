"""Browser reading with per-run isolation and cooperative stop.

Playwright is only used for public pages that need rendering. Each run gets its
own context (no user cookies), every subresource request is validated against the
node's allowed domains, native timeouts are used, and a stop request is honoured
cooperatively by closing the context. ``asyncio.Task.cancel`` is deliberately not
used to pretend the browser stopped.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from personal_agent.adapters.tools.scope import validate_url
from personal_agent.domain.common import AppError, ErrorCategory, iso_z, stable_hash
from personal_agent.ports.contracts import ToolResult, Usage


class BrowserDriver(Protocol):
    async def read(
        self,
        url: str,
        *,
        allowed_domains: tuple[str, ...],
        should_stop: Callable[[], bool],
        resolver: Callable[[str], Iterable[str]] | None,
    ) -> dict[str, Any]: ...


@dataclass
class BrowserReader:
    driver: BrowserDriver
    should_stop: Callable[[], bool]
    resolver: Callable[[str], Iterable[str]] | None = None

    async def execute(
        self, args: dict[str, Any], *, allowed_domains: tuple[str, ...]
    ) -> ToolResult:
        url = str(args.get("url") or "")
        validate_url(url, allowed_domains=allowed_domains, resolver=self.resolver)
        if self.should_stop():
            raise AppError(
                code="INTERRUPTED",
                message="a stop request was received before the browser action",
                category=ErrorCategory.INTERRUPTED,
                status=409,
            )
        page = await self.driver.read(
            url,
            allowed_domains=allowed_domains,
            should_stop=self.should_stop,
            resolver=self.resolver,
        )
        if self.should_stop():
            raise AppError(
                code="INTERRUPTED",
                message="the browser action observed a stop request",
                category=ErrorCategory.INTERRUPTED,
                status=409,
            )
        payload = {
            "url": url,
            "final_url": page.get("final_url", url),
            "text": page.get("text", ""),
            "headings": page.get("headings", []),
            "fetched_at": iso_z(datetime.now(UTC)),
        }
        return ToolResult(
            status="ok",
            result_ref=stable_hash(payload),
            source_refs=[str(payload["final_url"])],
            usage=Usage(tool_calls=1, page_reads=1),
        )


class PlaywrightBrowserDriver:
    """Real driver; lazy import so offline tests never require a browser."""

    def __init__(self, *, action_timeout_seconds: float = 30.0) -> None:
        self._timeout_ms = int(action_timeout_seconds * 1000)

    async def read(
        self,
        url: str,
        *,
        allowed_domains: tuple[str, ...],
        should_stop: Callable[[], bool],
        resolver: Callable[[str], Iterable[str]] | None,
    ) -> dict[str, Any]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as error:  # pragma: no cover - optional dependency
            raise AppError(
                code="RUNTIME_INCOMPATIBLE",
                message="Playwright is not installed",
                category=ErrorCategory.DEPENDENCY,
                status=503,
            ) from error
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()
            try:
                blocked: list[str] = []

                async def route(route_handler: Any) -> None:
                    target = route_handler.request.url
                    try:
                        validate_url(
                            target,
                            allowed_domains=allowed_domains,
                            resolver=resolver,
                        )
                    except AppError:
                        blocked.append(target)
                        await route_handler.abort()
                        return
                    await route_handler.continue_()

                await context.route("**/*", route)
                page = await context.new_page()
                page.set_default_timeout(self._timeout_ms)
                if should_stop():
                    raise AppError(
                        code="INTERRUPTED",
                        message="stop requested before navigation",
                        category=ErrorCategory.INTERRUPTED,
                        status=409,
                    )
                await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
                html = await page.content()
                from personal_agent.adapters.tools.extract import extract_text

                extracted = extract_text(html)
                return {
                    "final_url": page.url,
                    "text": extracted["text"],
                    "headings": extracted["headings"],
                    "blocked_subresources": blocked,
                }
            finally:
                # Close the context first, then the browser. This is the
                # cooperative stop path; no Task.cancel is involved.
                await context.close()
                await browser.close()

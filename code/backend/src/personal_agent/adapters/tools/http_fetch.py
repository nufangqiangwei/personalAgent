"""HTTP fetch adapter with manual, validated redirects.

TLS verification stays enabled and redirects are followed manually so every hop
is checked against the scope and resolved to a public address. Responses are
size-limited and hashed; the body is evidence, never executable content.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from personal_agent.adapters.tools.extract import extract_text
from personal_agent.adapters.tools.scope import (
    MAX_REDIRECTS,
    validate_url,
)
from personal_agent.domain.common import AppError, ErrorCategory, iso_z, stable_hash
from personal_agent.ports.contracts import ToolResult, Usage

MAX_BODY_BYTES = 2 * 1024 * 1024


@dataclass
class HttpFetcher:
    client: httpx.AsyncClient
    resolver: Callable[[str], Iterable[str]] | None = None
    max_body_bytes: int = MAX_BODY_BYTES

    async def fetch(
        self, url: str, *, allowed_domains: tuple[str, ...]
    ) -> dict[str, Any]:
        validate_url(url, allowed_domains=allowed_domains, resolver=self.resolver)
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                response = await self.client.get(
                    current,
                    headers={"Accept": "text/html,application/xhtml+xml,text/plain"},
                    follow_redirects=False,
                    timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
                )
            except httpx.HTTPError as error:
                raise AppError(
                    code="SOURCE_UNAVAILABLE",
                    message="the source could not be fetched",
                    category=ErrorCategory.DEPENDENCY,
                    status=503,
                ) from error
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise AppError(
                        code="SOURCE_UNAVAILABLE",
                        message="a redirect without a location was returned",
                        category=ErrorCategory.DEPENDENCY,
                        status=503,
                    )
                current = urljoin(current, location)
                validate_url(current, allowed_domains=allowed_domains, resolver=self.resolver)
                continue
            if response.status_code >= 400:
                raise AppError(
                    code="SOURCE_UNAVAILABLE",
                    message=f"source returned {response.status_code}",
                    category=ErrorCategory.DEPENDENCY,
                    status=503,
                )
            raw = response.content
            if len(raw) > self.max_body_bytes:
                raw = raw[: self.max_body_bytes]
            encoding = response.encoding or "utf-8"
            try:
                text = raw.decode(encoding, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            content_type = response.headers.get("content-type", "")
            extracted = (
                extract_text(text) if "html" in content_type.lower() else {"text": text}
            )
            return {
                "url": url,
                "final_url": current,
                "status": response.status_code,
                "content_type": content_type,
                "sha256": stable_hash({"body": text}),
                "fetched_at": iso_z(datetime.now(UTC)),
                "text": extracted.get("text", ""),
                "headings": extracted.get("headings", []),
                "truncated": len(raw) >= self.max_body_bytes,
            }
        raise AppError(
            code="REDIRECT_LIMIT",
            message="too many redirects",
            category=ErrorCategory.PERMISSION,
            status=403,
        )


def fetch_result(payload: dict[str, Any]) -> ToolResult:
    return ToolResult(
        status="ok",
        result_ref=stable_hash(payload),
        source_refs=[str(payload.get("final_url") or payload.get("url"))],
        usage=Usage(tool_calls=1, page_reads=1),
        content=payload,
    )

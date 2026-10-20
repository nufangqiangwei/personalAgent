"""HTTP fetch adapter with manual, validated redirects and pinned connections.

Each request is resolved once, validated, and then connected to that exact public
address; the original hostname stays in the Host header and the TLS SNI so a
second DNS lookup cannot rebind the connection. Bodies are streamed with a hard
byte limit, honour the stop flag, and use the injected clock for timestamps.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from personal_agent.adapters.tools.extract import extract_text
from personal_agent.adapters.tools.scope import (
    MAX_REDIRECTS,
    resolve_url,
)
from personal_agent.domain.common import AppError, ErrorCategory, iso_z, stable_hash
from personal_agent.ports.contracts import Clock, ToolResult, Usage

MAX_BODY_BYTES = 2 * 1024 * 1024


def _pinned_url(url: str, address: str) -> str:
    parts = urlsplit(url)
    host = f"[{address}]" if ":" in address else address
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@dataclass
class HttpFetcher:
    client: httpx.AsyncClient
    resolver: Callable[[str], Iterable[str]] | None = None
    max_body_bytes: int = MAX_BODY_BYTES
    clock: Clock | None = None
    should_stop: Callable[[], bool] | None = None

    def _now(self) -> datetime:
        return self.clock.now() if self.clock is not None else datetime.now(UTC)

    async def fetch(
        self, url: str, *, allowed_domains: tuple[str, ...]
    ) -> dict[str, Any]:
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            _validated, addresses = resolve_url(
                current, allowed_domains=allowed_domains, resolver=self.resolver
            )
            host = urlsplit(current).hostname or ""
            target = _pinned_url(current, addresses[0])
            try:
                async with self.client.stream(
                    "GET",
                    target,
                    headers={
                        "Host": host,
                        "Accept": "text/html,application/xhtml+xml,text/plain",
                    },
                    follow_redirects=False,
                    timeout=httpx.Timeout(
                        connect=10.0, read=30.0, write=10.0, pool=10.0
                    ),
                    extensions={"sni_hostname": host},
                ) as response:
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
                        continue
                    if response.status_code >= 400:
                        raise AppError(
                            code="SOURCE_UNAVAILABLE",
                            message=f"source returned {response.status_code}",
                            category=ErrorCategory.DEPENDENCY,
                            status=503,
                        )
                    raw = bytearray()
                    truncated = False
                    async for chunk in response.aiter_bytes():
                        if self.should_stop is not None and self.should_stop():
                            raise AppError(
                                code="INTERRUPTED",
                                message="a stop request aborted the page read",
                                category=ErrorCategory.INTERRUPTED,
                                status=409,
                            )
                        remaining = self.max_body_bytes - len(raw)
                        if remaining <= 0:
                            truncated = True
                            break
                        raw.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            truncated = True
                            break
                    encoding = response.encoding or "utf-8"
                    try:
                        text = bytes(raw).decode(encoding, errors="replace")
                    except LookupError:
                        text = bytes(raw).decode("utf-8", errors="replace")
                    content_type = response.headers.get("content-type", "")
                    extracted = (
                        extract_text(text)
                        if "html" in content_type.lower()
                        else {"text": text}
                    )
                    return {
                        "url": url,
                        "final_url": current,
                        "status": response.status_code,
                        "content_type": content_type,
                        "sha256": stable_hash({"body": text}),
                        "fetched_at": iso_z(self._now()),
                        "text": extracted.get("text", ""),
                        "headings": extracted.get("headings", []),
                        "truncated": truncated,
                    }
            except httpx.HTTPError as error:
                raise AppError(
                    code="SOURCE_UNAVAILABLE",
                    message="the source could not be fetched",
                    category=ErrorCategory.DEPENDENCY,
                    status=503,
                ) from error
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

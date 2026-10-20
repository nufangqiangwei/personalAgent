"""Tool scope, handshake tokens and target validation.

Every tool call carries a scope token bound to one run, attempt, epoch and tool.
The token expires, cannot be replayed for another step, and is checked again by
the tool service even when the runtime guard already approved the call.

Network targets are validated at call time: scheme, port, userinfo, allowed
domain suffix, and the resolved address (rejecting loopback, private, link-local,
metadata and unspecified addresses). Redirects and browser subresource requests
are validated hop by hop, which also defeats DNS rebinding for the validated
resolution.
"""

from __future__ import annotations

import ipaddress
import secrets
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from personal_agent.domain.common import AppError, ErrorCategory, iso_z, stable_hash

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443}
MAX_REDIRECTS = 3
METADATA_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}


def _deny(code: str, message: str, status: int = 403) -> AppError:
    return AppError(
        code=code,
        message=message,
        category=ErrorCategory.PERMISSION,
        status=status,
    )


@dataclass(frozen=True, slots=True)
class ScopeGrant:
    token: str
    run_id: str
    attempt_id: str
    epoch: int
    tool_name: str
    allowed_domains: tuple[str, ...]
    expires_at: datetime


@dataclass
class ToolScopeRegistry:
    ttl: timedelta = timedelta(minutes=30)
    _grants: dict[str, ScopeGrant] = field(default_factory=dict)

    def issue(
        self,
        *,
        run_id: str,
        attempt_id: str,
        epoch: int,
        tool_name: str,
        allowed_domains: Iterable[str],
        now: datetime,
    ) -> str:
        token = secrets.token_urlsafe(32)
        self._grants[token] = ScopeGrant(
            token=token,
            run_id=run_id,
            attempt_id=attempt_id,
            epoch=epoch,
            tool_name=tool_name,
            allowed_domains=tuple(allowed_domains),
            expires_at=now + self.ttl,
        )
        return token

    def validate(
        self, *, token: str, tool_name: str, epoch: int, now: datetime
    ) -> ScopeGrant:
        grant = self._grants.get(token)
        if grant is None:
            raise _deny("SCOPE_TOKEN_INVALID", "the scope token is unknown or already used")
        if grant.expires_at <= now:
            self._grants.pop(token, None)
            raise _deny("SCOPE_TOKEN_EXPIRED", "the scope token expired")
        if grant.epoch != epoch:
            raise _deny("EPOCH_MISMATCH", "a stale epoch cannot execute tools")
        if grant.tool_name != tool_name:
            raise _deny("TOOL_SCOPE_DENIED", "the token was issued for another tool")
        return grant

    def revoke(self, token: str) -> None:
        self._grants.pop(token, None)

    def clear(self) -> None:
        self._grants.clear()


def is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast:
        return False
    if ip.is_unspecified or ip.is_reserved:
        return False
    # IPv6 unique-local addresses are private but double-check the range.
    if isinstance(ip, ipaddress.IPv6Address) and ip in ipaddress.ip_network("fc00::/7"):
        return False
    return True


def domain_allowed(host: str, allowed_domains: Iterable[str]) -> bool:
    allowed = [item.lower().rstrip(".") for item in allowed_domains]
    if not allowed:
        # An empty list means "no network", never "allow everything".
        return False
    host = host.lower().rstrip(".")
    for candidate in allowed:
        if host == candidate or host.endswith("." + candidate):
            return True
    return False


def resolve_url(
    url: str,
    *,
    allowed_domains: Iterable[str],
    resolver: Callable[[str], Iterable[str]] | None = None,
) -> tuple[str, list[str]]:
    """Validate the URL and return the exact public addresses to connect to.

    The caller must connect to one of the returned addresses instead of resolving
    again: a second lookup is the DNS-rebinding window this function closes.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise _deny("TARGET_FORBIDDEN", "only http and https targets are allowed")
    if parts.username or parts.password:
        raise _deny("TARGET_FORBIDDEN", "URLs with embedded credentials are rejected")
    host = parts.hostname or ""
    if not host:
        raise _deny("TARGET_FORBIDDEN", "the URL has no host")
    if host.lower().rstrip(".") in METADATA_HOSTS:
        raise _deny("TARGET_FORBIDDEN", "cloud metadata hosts are rejected")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as error:
        raise _deny("TARGET_FORBIDDEN", "the URL port is invalid") from error
    if port not in ALLOWED_PORTS:
        raise _deny("TARGET_FORBIDDEN", f"port {port} is not allowed")
    if not domain_allowed(host, allowed_domains):
        raise _deny("DOMAIN_NOT_ALLOWED", f"host {host!r} is outside the allowed domains")
    resolve = resolver or _default_resolver
    try:
        addresses = list(resolve(host))
    except OSError as error:
        raise AppError(
            code="SOURCE_UNAVAILABLE",
            message="the host could not be resolved",
            category=ErrorCategory.DEPENDENCY,
            status=503,
        ) from error
    if not addresses:
        raise AppError(
            code="SOURCE_UNAVAILABLE",
            message="the host resolved to no address",
            category=ErrorCategory.DEPENDENCY,
            status=503,
        )
    for address in addresses:
        if not is_public_address(address):
            raise _deny("TARGET_FORBIDDEN", "the host resolves to a non-public address")
    return url, addresses


def validate_url(
    url: str,
    *,
    allowed_domains: Iterable[str],
    resolver: Callable[[str], Iterable[str]] | None = None,
) -> str:
    return resolve_url(url, allowed_domains=allowed_domains, resolver=resolver)[0]


def _default_resolver(host: str) -> Iterable[str]:
    return {str(item[4][0]) for item in socket.getaddrinfo(host, None)}


def validate_redirect_chain(
    *,
    start_url: str,
    locations: Iterable[str],
    allowed_domains: Iterable[str],
    resolver: Callable[[str], Iterable[str]] | None = None,
) -> list[str]:
    chain = [start_url]
    current = start_url
    for location in locations:
        from urllib.parse import urljoin

        current = urljoin(current, location)
        chain.append(current)
        if len(chain) > MAX_REDIRECTS + 1:
            raise _deny("REDIRECT_LIMIT", "too many redirects")
        validate_url(current, allowed_domains=allowed_domains, resolver=resolver)
    return chain


def scope_hash(scope: ScopeGrant) -> str:
    return stable_hash(
        {
            "run_id": scope.run_id,
            "attempt_id": scope.attempt_id,
            "epoch": scope.epoch,
            "tool": scope.tool_name,
            "domains": list(scope.allowed_domains),
            "expires_at": iso_z(scope.expires_at),
        }
    )

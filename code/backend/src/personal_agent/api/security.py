"""Local single-user session, Host/Origin and CSRF checks.

There are no accounts, roles or tenants. The application issues a one-time local
session cookie (HttpOnly, SameSite=Strict) plus a readable double-submit CSRF
cookie. Every unsafe request must echo the CSRF value in ``X-CSRF-Token`` and, if
it carries an ``Origin`` header, that origin must be the local app. This is a
local-user boundary, not an authentication system.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from starlette.requests import Request

from personal_agent.domain.common import AppError, ErrorCategory

SESSION_COOKIE = "pa_session"
CSRF_COOKIE = "pa_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass
class LocalSession:
    session_id: str
    csrf_token: str
    created_at: datetime


@dataclass
class SessionManager:
    """In-memory sessions for the local process; restart requires re-bootstrap."""

    ttl: timedelta = timedelta(hours=24)
    _sessions: dict[str, LocalSession] = field(default_factory=dict)

    def create(self) -> LocalSession:
        session = LocalSession(
            session_id=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            created_at=datetime.now(UTC),
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str | None) -> LocalSession | None:
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if datetime.now(UTC) - session.created_at > self.ttl:
            self._sessions.pop(session_id, None)
            return None
        return session

    def clear(self) -> None:
        self._sessions.clear()


@dataclass(frozen=True)
class OriginPolicy:
    host: str
    port: int
    dev_origin: str | None = None

    def allowed_origins(self) -> set[str]:
        allowed = {
            f"http://{self.host}:{self.port}",
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
        }
        if self.dev_origin:
            allowed.add(self.dev_origin)
        return allowed

    def check_scope(self, scope: Any) -> None:
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        self._check(headers.get("host", ""), headers.get("origin"))

    def check(self, request: Request) -> None:
        self._check(request.headers.get("host", ""), request.headers.get("origin"))

    def _check(self, host_header: str, origin: str | None) -> None:
        hostname = host_header.split(":")[0].lower()
        if hostname not in {"127.0.0.1", "localhost", "::1", "[::1]"}:
            raise AppError(
                code="HOST_NOT_ALLOWED",
                message="this application only serves loopback hosts",
                category=ErrorCategory.PERMISSION,
                status=403,
            )
        if origin is not None and origin not in self.allowed_origins():
            raise AppError(
                code="ORIGIN_NOT_ALLOWED",
                message="the request origin is not allowed",
                category=ErrorCategory.PERMISSION,
                status=403,
            )


def require_csrf(request: Request) -> None:
    if request.method.upper() in SAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise AppError(
            code="CSRF_FAILED",
            message="a valid CSRF token is required for write requests",
            category=ErrorCategory.PERMISSION,
            status=403,
        )
    if request.cookies.get(SESSION_COOKIE) is None:
        raise AppError(
            code="SESSION_REQUIRED",
            message="the local session cookie is missing",
            category=ErrorCategory.PERMISSION,
            status=403,
        )


def session_cookie_kwargs() -> dict[str, object]:
    return {
        "httponly": True,
        "samesite": "strict",
        "secure": False,
        "path": "/",
    }


def csrf_cookie_kwargs() -> dict[str, object]:
    return {
        "httponly": False,
        "samesite": "strict",
        "secure": False,
        "path": "/",
    }

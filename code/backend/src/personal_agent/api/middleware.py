"""ASGI middleware: request identity, local session/CSRF, idempotency.

The idempotency layer hashes the method, route and request body and stores the
first successful JSON response. A retry with the same key and body replays it; the
same key with a different body is a conflict. Execution start, branch creation and
budget grants must never be duplicated by a browser retry.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from personal_agent.adapters.persistence.request_models import RequestRecord
from personal_agent.api.security import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SAFE_METHODS,
    SESSION_COOKIE,
    OriginPolicy,
    SessionManager,
    csrf_cookie_kwargs,
    session_cookie_kwargs,
)
from personal_agent.domain.common import AppError, new_id, stable_hash

MAX_STORED_RESPONSE_BYTES = 256 * 1024


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _cookies(scope: Scope) -> dict[str, str]:
    raw = _header(scope, b"cookie")
    if not raw:
        return {}
    parsed: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            parsed[key.strip()] = value.strip()
    return parsed


def _json_response(status: int, payload: dict[str, Any]) -> tuple[Message, Message]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    start: Message = {
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    }
    return start, {"type": "http.response.body", "body": body, "more_body": False}


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _header(scope, b"x-request-id") or uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_id)


class LocalSessionMiddleware:
    """Issue the local session and enforce Origin/CSRF on unsafe requests."""

    def __init__(
        self, app: ASGIApp, sessions: SessionManager, origin_policy: OriginPolicy
    ) -> None:
        self.app = app
        self.sessions = sessions
        self.origin_policy = origin_policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cookies = _cookies(scope)
        session = self.sessions.get(cookies.get(SESSION_COOKIE))
        issued = None
        if session is None:
            issued = self.sessions.create()

        try:
            self.origin_policy.check_scope(scope)
            if scope["method"].upper() not in SAFE_METHODS:
                self._check_csrf(scope, cookies)
        except AppError as error:
            start, body = _json_response(
                error.status,
                error.payload(str(scope.get("state", {}).get("request_id", "unknown"))),
            )
            await send(start)
            await send(body)
            return

        set_cookies: list[tuple[bytes, bytes]] = []
        if issued is not None:
            set_cookies.append(
                (
                    b"set-cookie",
                    (
                        f"{SESSION_COOKIE}={issued.session_id}; "
                        "HttpOnly; SameSite=Strict; Path=/"
                    ).encode(),
                )
            )
            set_cookies.append(
                (
                    b"set-cookie",
                    f"{CSRF_COOKIE}={issued.csrf_token}; SameSite=Strict; Path=/".encode(),
                )
            )

        async def send_with_cookies(message: Message) -> None:
            if message["type"] == "http.response.start" and set_cookies:
                headers = list(message.get("headers", []))
                headers.extend(set_cookies)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cookies)

    @staticmethod
    def _check_csrf(scope: Scope, cookies: dict[str, str]) -> None:
        cookie_value = cookies.get(CSRF_COOKIE)
        header_value = _header(scope, CSRF_HEADER.encode())
        if not cookie_value or not header_value or cookie_value != header_value:
            raise AppError(
                code="CSRF_FAILED",
                message="a valid CSRF token is required for write requests",
                status=403,
            )
        if SESSION_COOKIE not in cookies:
            raise AppError(
                code="SESSION_REQUIRED",
                message="the local session cookie is missing",
                status=403,
            )


class IdempotencyMiddleware:
    """Persist and replay responses for unsafe requests carrying a key."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"].upper() in SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        key = _header(scope, b"idempotency-key")
        if not key or len(key) > 200:
            start_message, body_message = _json_response(
                400,
                AppError(
                    code="IDEMPOTENCY_KEY_REQUIRED",
                    message="every write request must carry an Idempotency-Key",
                    status=400,
                ).payload(str(scope.get("state", {}).get("request_id", "unknown"))),
            )
            await send(start_message)
            await send(body_message)
            return

        body_bytes, replay_receive = await _buffer_body(receive)
        route = str(scope.get("path", ""))
        request_hash = stable_hash(
            {
                "method": scope["method"].upper(),
                "route": route,
                "query": str(scope.get("query_string", b"")),
                "body": body_bytes.decode("utf-8", errors="replace"),
            }
        )

        context = getattr(scope["app"].state, "context", None)
        if context is None:  # pragma: no cover - application wiring bug
            await self.app(scope, replay_receive, send)
            return

        stored = await self._load(context, route, key)
        if stored is not None:
            if stored.request_hash != request_hash:
                start_message, body_message = _json_response(
                    409,
                    AppError(
                        code="IDEMPOTENCY_CONFLICT",
                        message="this Idempotency-Key was used for a different request",
                        status=409,
                    ).payload(str(scope.get("state", {}).get("request_id", "unknown"))),
                )
                await send(start_message)
                await send(body_message)
                return
            if stored.status_code and stored.response_json is not None:
                start_message, body_message = _json_response(
                    stored.status_code, stored.response_json
                )
                await send(start_message)
                await send(body_message)
                return
            # An earlier attempt failed before producing a response: run again.

        captured: dict[str, Any] = {"status": None, "headers": [], "chunks": []}

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                captured["headers"] = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                captured["chunks"].append(message.get("body", b""))
            await send(message)

        await self.app(scope, replay_receive, capture)

        status = captured["status"]
        content_type = dict(captured["headers"]).get(b"content-type", b"").decode("latin-1")
        response_body = b"".join(captured["chunks"])
        if (
            isinstance(status, int)
            and 200 <= status < 300
            and content_type.startswith("application/json")
            and len(response_body) <= MAX_STORED_RESPONSE_BYTES
        ):
            try:
                payload = json.loads(response_body.decode("utf-8"))
            except ValueError:  # pragma: no cover - defensive
                return
            await self._store(context, route, key, request_hash, status, payload)

    @staticmethod
    async def _load(context: Any, route: str, key: str) -> RequestRecord | None:
        async with context.database.session() as session:
            return await session.scalar(
                select(RequestRecord).where(
                    RequestRecord.workspace_id == "local",
                    RequestRecord.route == route,
                    RequestRecord.idempotency_key == key,
                )
            )

    @staticmethod
    async def _store(
        context: Any,
        route: str,
        key: str,
        request_hash: str,
        status: int,
        payload: dict[str, Any],
    ) -> None:
        async with context.database.session() as session:
            record = RequestRecord(
                id=new_id(),
                workspace_id="local",
                route=route,
                idempotency_key=key,
                request_hash=request_hash,
                status_code=status,
                response_json=payload,
                resource_id=str(payload.get("id") or payload.get("run_id") or "") or None,
                created_at=context.clock.now(),
                revision=1,
            )
            session.add(record)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()


async def _buffer_body(receive: Receive) -> tuple[bytes, Receive]:
    buffered: list[Message] = []
    body = b""
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        buffered.append(message)
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    index = 0

    async def replay() -> Message:
        nonlocal index
        if index < len(buffered):
            message = buffered[index]
            index += 1
            return message
        return {"type": "http.request", "body": b"", "more_body": False}

    return body, replay


# Ensure the session/CSRF cookie helpers are importable for tests.
__all__ = [
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "SESSION_COOKIE",
    "IdempotencyMiddleware",
    "LocalSessionMiddleware",
    "RequestIdMiddleware",
    "csrf_cookie_kwargs",
    "session_cookie_kwargs",
]

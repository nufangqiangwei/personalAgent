"""API foundation contract tests (T008).

Covers the local session/CSRF boundary, idempotent replay and conflict,
If-Match semantics and redacted errors. Domain endpoints are added by their
stories; these contracts apply to every one of them.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI, Request

from personal_agent.api.deps import require_if_match
from personal_agent.api.middleware import IdempotencyMiddleware, LocalSessionMiddleware
from personal_agent.api.security import OriginPolicy, SessionManager
from personal_agent.domain.common import AppError


async def test_health_and_policies_never_leak_secrets(client: httpx.AsyncClient) -> None:
    health = await client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["mode"] == "mock"
    assert body["execution_available"] is True
    assert "dependency_checks" in body
    assert "secret" not in health.text.lower()

    policies = await client.get("/policies")
    assert policies.status_code == 200
    document = policies.json()
    assert document["hash"].startswith("sha256:")
    assert document["retention"]["business_content_days"] == 90
    assert document["budget_defaults"]["model_requests"] == 30


def _idempotent_app(database: Any, clock: Any) -> tuple[FastAPI, dict[str, int]]:
    app = FastAPI()
    app.state.context = SimpleNamespace(database=database, clock=clock)
    calls = {"count": 0}

    @app.post("/echo")
    async def echo(payload: dict[str, Any]) -> dict[str, Any]:
        calls["count"] += 1
        return {"id": "resource-1", "echo": payload.get("value"), "n": calls["count"]}

    app.add_middleware(IdempotencyMiddleware)
    return app, calls


async def test_write_requires_an_idempotency_key(database: Any, clock: Any) -> None:
    app, calls = _idempotent_app(database, clock)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        response = await http.post("/echo", json={"value": 1})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert calls["count"] == 0


async def test_same_key_and_body_replays_without_duplicating(database: Any, clock: Any) -> None:
    app, calls = _idempotent_app(database, clock)
    headers = {"Idempotency-Key": "key-1"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        first = await http.post("/echo", json={"value": 1}, headers=headers)
        second = await http.post("/echo", json={"value": 1}, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert calls["count"] == 1


async def test_same_key_with_a_different_body_conflicts(database: Any, clock: Any) -> None:
    app, calls = _idempotent_app(database, clock)
    headers = {"Idempotency-Key": "key-2"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        await http.post("/echo", json={"value": 1}, headers=headers)
        conflict = await http.post("/echo", json={"value": 2}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert calls["count"] == 1


async def test_csrf_is_required_for_writes_but_not_reads() -> None:
    sessions = SessionManager()
    app = FastAPI()
    app.add_middleware(
        LocalSessionMiddleware,
        sessions=sessions,
        origin_policy=OriginPolicy("127.0.0.1", 8000),
    )

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/ping")
    async def ping_write() -> dict[str, str]:
        return {"ok": "written"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as http:
        assert (await http.get("/ping")).status_code == 200
        csrf = http.cookies.get("pa_csrf")
        assert csrf
        missing = await http.post("/ping")
        assert missing.status_code == 403
        assert missing.json()["error"]["code"] == "CSRF_FAILED"
        allowed = await http.post("/ping", headers={"x-csrf-token": csrf})
        assert allowed.status_code == 200


async def test_foreign_origin_is_rejected() -> None:
    sessions = SessionManager()
    app = FastAPI()
    app.add_middleware(
        LocalSessionMiddleware,
        sessions=sessions,
        origin_policy=OriginPolicy("127.0.0.1", 8000),
    )

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as http:
        response = await http.get("/ping", headers={"origin": "https://evil.example"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"


async def test_if_match_is_required_and_must_match() -> None:
    from personal_agent.api.errors import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)

    @app.post("/guarded")
    async def guarded(request: Request) -> dict[str, int]:
        revision = require_if_match(request, 3, code="DEMO_STALE")
        return {"revision": revision}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        missing = await http.post("/guarded")
        assert missing.status_code == 428
        stale = await http.post("/guarded", headers={"If-Match": '"2"'})
        assert stale.status_code == 412
        assert stale.json()["error"]["code"] == "DEMO_STALE"
        ok = await http.post("/guarded", headers={"If-Match": '"3"'})
        assert ok.status_code == 200
        assert ok.json()["revision"] == 3


async def test_errors_are_redacted_and_carry_a_request_id() -> None:
    app = FastAPI()

    @app.get("/boom")
    async def boom() -> None:
        raise AppError(
            code="DEMO",
            message="failed",
            status=422,
            details={"api_key": "sk-secret", "safe": "visible"},
        )

    from personal_agent.api.errors import install_error_handlers
    from personal_agent.api.middleware import RequestIdMiddleware

    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        response = await http.get("/boom")
    payload = response.json()["error"]
    assert response.status_code == 422
    assert payload["code"] == "DEMO"
    assert payload["request_id"]
    assert payload["details"]["api_key"] == "<redacted>"
    assert payload["details"]["safe"] == "visible"
    assert "sk-secret" not in response.text

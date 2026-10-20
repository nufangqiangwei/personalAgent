"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from personal_agent.api.artifacts import router as artifacts_router
from personal_agent.api.assistance import router as assistance_router
from personal_agent.api.branches import router as branches_router
from personal_agent.api.context import AppContext
from personal_agent.api.data_items import router as data_items_router
from personal_agent.api.errors import install_error_handlers
from personal_agent.api.events import router as events_router
from personal_agent.api.health import router as health_router
from personal_agent.api.maintenance import router as maintenance_router
from personal_agent.api.middleware import (
    IdempotencyMiddleware,
    LocalSessionMiddleware,
    RequestIdMiddleware,
)
from personal_agent.api.requirements import router as requirements_router
from personal_agent.api.run_controls import router as run_controls_router
from personal_agent.api.runs import router as runs_router
from personal_agent.api.security import OriginPolicy, SessionManager
from personal_agent.api.settings import router as settings_router
from personal_agent.api.workflows import router as workflows_router
from personal_agent.bootstrap.config import AppConfig
from personal_agent.bootstrap.lifespan import build_context, mark_interrupted_runs


def create_app(config: AppConfig, *, context: AppContext | None = None) -> FastAPI:
    sessions = context.sessions if context is not None else SessionManager()
    origin_policy = (
        context.origin_policy
        if context is not None
        else OriginPolicy(config.host, config.port, config.dev_origin)
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = app.state.context is None
        if owned:
            app.state.context = await build_context(
                config, sessions=sessions, origin_policy=origin_policy
            )
            await mark_interrupted_runs(
                app.state.context.database, app.state.context.clock
            )
        try:
            yield
        finally:
            if owned and app.state.context is not None:
                await app.state.context.executor.shutdown()
                await app.state.context.database.dispose()

    app = FastAPI(
        title="Personal-Agent",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.context = context
    app.state.session_manager = sessions
    app.state.origin_policy = origin_policy

    # Middleware executes in reverse registration order: request id outermost,
    # then local session/CSRF, then idempotency around the route.
    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(
        LocalSessionMiddleware, sessions=sessions, origin_policy=origin_policy
    )
    app.add_middleware(RequestIdMiddleware)

    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(workflows_router)
    app.include_router(requirements_router)
    app.include_router(runs_router)
    app.include_router(run_controls_router)
    app.include_router(assistance_router)
    app.include_router(branches_router)
    app.include_router(events_router)
    app.include_router(artifacts_router)
    app.include_router(data_items_router)
    app.include_router(settings_router)
    app.include_router(maintenance_router)

    dist = config.frontend_dist
    if dist is not None and (dist / "index.html").exists():
        import mimetypes

        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        # Windows may map .js to text/plain in the registry; browsers reject a
        # module script served with the wrong MIME type.
        mimetypes.add_type("text/javascript", ".js")
        mimetypes.add_type("text/javascript", ".mjs")
        mimetypes.add_type("text/css", ".css")
        mimetypes.add_type("image/svg+xml", ".svg")

        assets = dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            if path.startswith("api/") or path in {"health", "policies"}:
                from fastapi import HTTPException

                raise HTTPException(status_code=404)
            candidate = (dist / path).resolve()
            if candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return app

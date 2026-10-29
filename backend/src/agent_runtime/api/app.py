"""FastAPI 应用工厂。设计文档：docs/09_api/。

中间件顺序（由外到内）：CORS → 接入闸门 → 路由。闸门不碰请求体，对 SSE 透明。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent_runtime.api.deps import ApiServices
from agent_runtime.api.errors import TRACE_HEADER, install_handlers
from agent_runtime.api.routes import (
    files,
    mcp,
    prompts,
    sessions,
    settings,
    skills,
    system,
    tasks,
    workspaces,
)
from agent_runtime.api.security import AccessGate
from agent_runtime.api.token import FailureLimiter

API_PREFIX = "/api/v1"


def create_app(services: ApiServices, *, token: str | None) -> FastAPI:
    server = services.settings.server
    app = FastAPI(
        title="BaseAgent API",
        version="1.0.0",
        docs_url="/docs" if server.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if server.docs_enabled else None,
    )
    app.state.services = services
    install_handlers(app)
    for module in (workspaces, sessions, settings, tasks, files, system, prompts, skills, mcp):
        app.include_router(module.router, prefix=API_PREFIX)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"name": "BaseAgent", "api": API_PREFIX}

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    app.add_middleware(
        AccessGate,
        token=token if server.auth.enabled else None,
        allowed_hosts=server.allowed_hosts,
        allowed_origins=server.cors_allow_origins,
        max_body_bytes=server.request_max_bytes,
        large_body_paths=[
            ("/files/content", services.settings.tools.files.write_max_bytes * 2 + 4096)
        ],
        docs_enabled=server.docs_enabled,
        limiter=FailureLimiter(server.auth.fail_limit, server.auth.fail_window_sec),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=server.cors_allow_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID", TRACE_HEADER],
        expose_headers=[TRACE_HEADER],
    )
    return app

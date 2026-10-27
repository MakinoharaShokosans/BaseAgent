"""错误模型：领域异常到状态码的一张表，以及统一的错误信封。设计文档：docs/09_api/03 §6。

领域层不感知 HTTP：映射集中在这里，沿 MRO 自下而上查找。500 不外泄内部细节，
每个错误响应都带 ``trace_id``，中间件与路由用同一套形状。
"""

from __future__ import annotations

import json
from typing import Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent_runtime.approval.errors import (
    ApprovalMismatchError,
    InvalidDecisionError,
    NotWaitingForApprovalError,
)
from agent_runtime.memory import (
    MemoryError_,
    SessionNotFound,
    WorkspaceNotFound,
    WorkspacePathConflict,
)
from agent_runtime.runtime import (
    ApprovalRequiredFirstError,
    FullPermissionsNotConfirmedError,
    TaskAlreadyExistsError,
    TaskAlreadyRunningError,
    TaskNotFoundError,
    TaskNotResumableError,
    TaskQueueFullError,
)
from agent_runtime.runtime.service import (
    ArtifactNotFoundError,
    TaskGoalError,
    TaskStateConflictError,
)
from tools.builtin.paths import PathEscapeError
from tools.core.errors import DependencyUnavailableError

TRACE_HEADER: Final = "X-Trace-ID"
_MAX_DETAILS: Final = 10


class ApiError(Exception):
    """路由层直接抛的错误（请求本身不合规）：带稳定的 code。"""

    def __init__(
        self, status: int, code: str, message: str, details: dict[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.details = details or {}


#: 领域异常 -> (状态码, code, 面向人的话)。同一语义在所有端点是同一个 code
DOMAIN_ERRORS: Final[dict[type[Exception], tuple[int, str, str]]] = {
    WorkspaceNotFound: (404, "WORKSPACE_NOT_FOUND", "工作区不存在"),
    SessionNotFound: (404, "SESSION_NOT_FOUND", "会话不存在"),
    TaskNotFoundError: (404, "TASK_NOT_FOUND", "任务不存在"),
    ArtifactNotFoundError: (404, "ARTIFACT_NOT_FOUND", "产物不存在"),
    WorkspacePathConflict: (409, "WORKSPACE_PATH_CONFLICT", "该路径已有工作区"),
    TaskStateConflictError: (409, "TASK_STATE_CONFLICT", "任务当前状态不允许该操作"),
    TaskAlreadyRunningError: (409, "TASK_STATE_CONFLICT", "任务当前状态不允许该操作"),
    TaskNotResumableError: (409, "TASK_STATE_CONFLICT", "任务当前状态不允许该操作"),
    ApprovalRequiredFirstError: (409, "TASK_STATE_CONFLICT", "任务正在等待审批"),
    NotWaitingForApprovalError: (409, "TASK_STATE_CONFLICT", "任务当前不在等待审批"),
    TaskAlreadyExistsError: (409, "TASK_STATE_CONFLICT", "任务已存在"),
    ApprovalMismatchError: (409, "APPROVAL_MISMATCH", "审批 ID 与待审批请求不一致"),
    TaskQueueFullError: (429, "TASK_QUEUE_FULL", "并发任务已满"),
    FullPermissionsNotConfirmedError: (
        422,
        "FULL_PERMISSIONS_NOT_CONFIRMED",
        "以 full_permissions 提交任务需要显式确认",
    ),
    TaskGoalError: (400, "VALIDATION_ERROR", "任务目标不合规"),
    InvalidDecisionError: (400, "VALIDATION_ERROR", "审批决定不合规"),
    PathEscapeError: (422, "PATH_ESCAPE_DETECTED", "路径越出了允许的范围"),
    DependencyUnavailableError: (503, "DEPENDENCY_UNAVAILABLE", "下游服务不可用"),
    MemoryError_: (422, "INVALID_REQUEST", "请求被拒绝"),
}


def classify(exc: Exception) -> tuple[int, str, str] | None:
    for klass in type(exc).__mro__:
        found = DOMAIN_ERRORS.get(klass)
        if found is not None:
            return found
    return None


def error_body(
    code: str, message: str, trace_id: str, details: dict[str, object] | None = None
) -> dict[str, object]:
    return {
        "error": {"code": code, "message": message, "details": details or {}, "trace_id": trace_id}
    }


def error_response(
    status: int,
    code: str,
    message: str,
    trace_id: str,
    details: dict[str, object] | None = None,
) -> JSONResponse:
    return JSONResponse(
        error_body(code, message, trace_id, details),
        status_code=status,
        headers={TRACE_HEADER: trace_id},
    )


def trace_id_of(request: Request) -> str:
    return str(request.scope.get("state", {}).get("trace_id") or "-")


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(request: Request, exc: ApiError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return error_response(exc.status, exc.code, exc.message, trace_id_of(request), exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        problems = [
            {"loc": [str(p) for p in e["loc"]], "msg": str(e["msg"]), "type": str(e["type"])}
            for e in exc.errors()[:_MAX_DETAILS]  # 不回显请求体：输入值里可能有敏感内容
        ]
        return error_response(
            400, "VALIDATION_ERROR", "请求不合规", trace_id_of(request), {"errors": problems}
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        code = {404: "ROUTE_NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(
            exc.status_code, "HTTP_ERROR"
        )
        return error_response(exc.status_code, code, str(exc.detail), trace_id_of(request))

    async def _domain(request: Request, exc: Exception) -> JSONResponse:
        mapped = classify(exc)
        assert mapped is not None
        status, code, message = mapped
        return error_response(status, code, message, trace_id_of(request), _context(exc))

    for klass in DOMAIN_ERRORS:  # 逐个注册：走内层的异常中间件，不会被当成未处理的 500 再抛出
        app.add_exception_handler(klass, _domain)

    @app.exception_handler(Exception)
    async def _any(request: Request, exc: Exception) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        trace_id = trace_id_of(request)
        logger.opt(exception=exc).error("未处理的异常 trace_id={}", trace_id)
        return error_response(500, "INTERNAL_ERROR", "服务内部错误", trace_id)


def _context(exc: Exception) -> dict[str, object]:
    """领域异常的说明文字本身不含敏感信息（由领域层保证），作为 detail 返回。"""
    text = str(exc)
    return {"reason": text} if text else {}


def dumps(body: dict[str, object]) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()

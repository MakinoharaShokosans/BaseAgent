"""bash_shell sidecar 的 HTTP 应用。设计文档：docs/05_security/02_shell_sandbox.md §2。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from baseagent_config.models import BashShellConfig
from services.bash_shell.audit import audit
from services.bash_shell.distill import DistillLimits
from services.bash_shell.paths import PathEscape, check_write_targets, extract_write_targets
from services.bash_shell.quota import GlobalMemoryBudget, Queued
from services.bash_shell.runner import (
    RunLimits,
    minimal_env,
    run_command,
    safe_component,
)
from services.common import LoopbackGuard

PREFIX = "/api/v1"


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(min_length=1, max_length=128)
    workspace_root: str = Field(min_length=1, max_length=4096)
    task_id: str = Field(min_length=1, max_length=128)
    step_id: int = Field(ge=0)
    command: str = Field(max_length=100_000)
    timeout_sec: float | None = Field(default=None, gt=0)
    #: 调用方声明的写入路径，与命令里的显式写入目标一并校验
    write_paths: list[str] = Field(default_factory=list, max_length=100)


@dataclass(frozen=True, slots=True)
class ShellSettings:
    config: BashShellConfig
    artifacts_dir: Path
    scratch_dir: Path
    source_env: dict[str, str] | None = None


def create_app(settings: ShellSettings) -> FastAPI:
    cfg = settings.config
    budget = GlobalMemoryBudget(cfg.memory_pool_mb, cfg.max_concurrent)
    limits = RunLimits(
        cfg.rlimit_as_mb,
        cfg.rlimit_fsize_mb,
        cfg.rlimit_cpu_sec,
        float(cfg.sigterm_grace_sec),
        cfg.stream_chunk_bytes,
    )
    distill = DistillLimits(
        cfg.distill_head_lines,
        cfg.distill_tail_lines,
        cfg.distill_max_keyword_lines,
        cfg.structured_sniff_max_bytes,
    )
    app = FastAPI(title="BaseAgent bash_shell", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        problems = [
            {"loc": [str(p) for p in e["loc"]], "msg": str(e["msg"])} for e in exc.errors()[:10]
        ]
        return JSONResponse(
            {"detail": {"code": "VALIDATION_ERROR", "errors": problems}}, status_code=400
        )

    @app.get(f"{PREFIX}/health")
    @app.get("/health")
    async def health() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return {
            "status": "ok",
            "active": budget.active,
            "waiting": budget.waiting,
            "pool_free_mb": budget.free_mb,
        }

    @app.post(f"{PREFIX}/shell/execute")
    async def execute(body: ExecuteRequest) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        root = Path(body.workspace_root)
        if not root.is_absolute() or not await asyncio.to_thread(root.is_dir):
            raise HTTPException(
                422, {"code": "WORKSPACE_INVALID", "reason": "工作区根路径必须是已存在的绝对目录"}
            )
        if not safe_component(body.task_id):
            raise HTTPException(
                422, {"code": "PATH_ESCAPE_DETECTED", "reason": "task_id 不是安全的目录名"}
            )
        hit = audit(body.command, cfg.extra_deny_patterns)
        if hit is not None:
            raise HTTPException(
                403,
                {
                    "code": "AUDIT_REJECTED",
                    "rule": hit.rule,
                    "reason": hit.reason,
                    "matched": hit.matched,
                },
            )
        try:
            check_write_targets(root, [*extract_write_targets(body.command), *body.write_paths])
        except PathEscape as exc:
            raise HTTPException(
                422,
                {
                    "code": "PATH_ESCAPE_DETECTED",
                    "rule": "WRITE_TARGET_OUTSIDE_WORKSPACE",
                    "reason": str(exc),
                },
            ) from exc

        try:
            grant = await budget.acquire(cfg.rlimit_as_mb)
        except Queued as queued:
            return {
                "status": "QUEUED",
                "queue_wait_sec": 0.0,
                "estimated_wait_sec": queued.estimated_wait_sec,
            }
        began = time.monotonic()
        try:
            scratch = settings.scratch_dir / body.task_id
            scratch.mkdir(parents=True, exist_ok=True)
            result = await run_command(
                body.command,
                cwd=root,
                timeout_sec=min(
                    body.timeout_sec or float(cfg.timeout_sec), float(cfg.max_timeout_sec)
                ),
                env=minimal_env(cfg.child_env_allowlist, scratch, settings.source_env),
                log_path=settings.artifacts_dir / body.task_id / f"step_{body.step_id}_bash.log",
                limits=limits,
                distill=distill,
            )
        finally:
            await budget.release(grant, duration_sec=time.monotonic() - began)
        return {
            "status": result.status,
            "exit_code": result.exit_code,
            "distilled_stdout": result.stdout,
            "distilled_stderr": result.stderr,
            "is_truncated": result.truncated,
            "artifact_path": result.artifact_path,
            "execution_time_ms": result.elapsed_ms,
            "timed_out": result.timed_out,
        }

    app.add_middleware(LoopbackGuard)
    return app

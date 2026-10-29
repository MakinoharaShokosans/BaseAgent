"""系统与自省。设计文档：docs/09_api/01_endpoints.md §6-7。所有 GET 只读、无副作用。"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from agent_runtime.api.deps import ApiServices, services_of
from agent_runtime.api.errors import ApiError
from agent_runtime.api.fsguard import BrowseDeniedError, list_directories
from agent_runtime.api.schemas import DirectoryListing

router = APIRouter(tags=["system"])
Services = Annotated[ApiServices, Depends(services_of)]
_PICKER_LOCK = asyncio.Lock()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/dependencies")
async def health_dependencies(svc: Services) -> dict[str, str]:
    return dict(await svc.dependency_checks())


@router.get("/system/limits")
async def limits(svc: Services) -> dict[str, int]:
    """运行时的并发上限。任务没有 Token、步数或时间预算（docs/12_decisions/0006）。只读。"""
    return {
        "max_concurrent_tasks": svc.settings.server.max_concurrent_tasks,
    }


@router.get("/system/fs/directories", response_model=DirectoryListing)
async def directories(
    svc: Services, path: Annotated[str | None, Query(max_length=4096)] = None
) -> DirectoryListing:
    fs = svc.settings.server.filesystem
    try:
        listing = await asyncio.to_thread(
            list_directories,
            path,
            roots=fs.browse_roots,
            forbidden=svc.settings.server.workspace.forbidden_roots,
            max_entries=fs.browse_max_entries,
        )
    except BrowseDeniedError as exc:
        raise ApiError(422, "PATH_ESCAPE_DETECTED", str(exc)) from exc
    except (NotADirectoryError, FileNotFoundError, PermissionError) as exc:
        raise ApiError(422, "PATH_NOT_A_DIRECTORY", "不是可浏览的目录") from exc
    return DirectoryListing(**listing)  # type: ignore[arg-type]


@router.post("/system/fs/pick-directory")
async def pick_directory(svc: Services) -> dict[str, str | None]:
    """同一时刻只允许一个原生选择窗口；超时有上限。"""
    fs = svc.settings.server.filesystem
    if not fs.native_picker_enabled or svc.native_picker is None:
        raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "原生目录选择不可用")
    if _PICKER_LOCK.locked():
        raise ApiError(409, "PICKER_BUSY", "已有一个目录选择窗口在打开")
    async with _PICKER_LOCK:
        try:
            chosen = await asyncio.wait_for(
                svc.native_picker(float(fs.native_picker_timeout_sec)),
                timeout=fs.native_picker_timeout_sec + 1,
            )
        except TimeoutError as exc:
            raise ApiError(504, "PICKER_TIMEOUT", "目录选择超时") from exc
    return {"path": chosen}


# ------------------------------------------------------------------ 自省


@router.get("/tools")
async def tools(svc: Services) -> list[dict[str, Any]]:
    return svc.introspection.tools()


@router.get("/models")
async def models(svc: Services) -> list[dict[str, Any]]:
    return svc.introspection.models()

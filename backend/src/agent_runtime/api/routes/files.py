"""工作区内的文件接口。设计文档：docs/09_api/01_endpoints.md §5。"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query

from agent_runtime.api.deps import ApiServices, services_of
from agent_runtime.api.errors import ApiError
from agent_runtime.api.fsguard import (
    FileHashConflictError,
    FileTooLargeError,
    NotTextFileError,
    build_tree,
    read_workspace_file,
    save_workspace_file,
)
from agent_runtime.api.schemas import FileContentOut, FileSave, FileSaveOut

router = APIRouter(tags=["files"])
Services = Annotated[ApiServices, Depends(services_of)]
WorkspaceId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
READ_MAX_BYTES = 5 * 1024 * 1024


async def _root(svc: ApiServices, workspace_id: str) -> Any:
    from pathlib import Path as FsPath

    return FsPath((await svc.memory.get_workspace(workspace_id)).root_path)


@router.get("/workspaces/{workspace_id}/files/tree")
async def file_tree(workspace_id: WorkspaceId, svc: Services) -> dict[str, object]:
    root = await _root(svc, workspace_id)
    return await asyncio.to_thread(build_tree, root, svc.settings.tools.files.ignored_dirs)


@router.get("/workspaces/{workspace_id}/files/content", response_model=FileContentOut)
async def read_file(
    workspace_id: WorkspaceId,
    svc: Services,
    path: Annotated[str, Query(min_length=1, max_length=4096)],
) -> FileContentOut:
    root = await _root(svc, workspace_id)
    try:
        text, digest, size = await asyncio.to_thread(
            read_workspace_file, root, path, READ_MAX_BYTES
        )
    except FileNotFoundError as exc:
        raise ApiError(404, "FILE_NOT_FOUND", "文件不存在") from exc
    except FileTooLargeError as exc:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", str(exc)) from exc
    except NotTextFileError as exc:
        raise ApiError(422, "NOT_A_TEXT_FILE", "不是 UTF-8 文本文件") from exc
    return FileContentOut(path=path, content=text, hash=digest, size=size)


@router.put("/workspaces/{workspace_id}/files/content", response_model=FileSaveOut)
async def save_file(workspace_id: WorkspaceId, body: FileSave, svc: Services) -> FileSaveOut:
    root = await _root(svc, workspace_id)
    limit = svc.settings.tools.files.write_max_bytes
    try:
        saved = await asyncio.to_thread(
            save_workspace_file,
            root,
            body.path,
            body.content,
            base_hash=body.base_hash,
            max_bytes=limit,
        )
    except FileTooLargeError as exc:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", str(exc)) from exc
    except FileHashConflictError as exc:
        raise ApiError(409, "FILE_CONFLICT", "文件在读取之后被改动过，请刷新后重试") from exc
    return FileSaveOut(path=body.path, hash=saved.hash, size=saved.size)

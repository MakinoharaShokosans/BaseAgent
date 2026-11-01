"""工作区与工作区记忆。设计文档：docs/09_api/01_endpoints.md §1。"""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Response

from agent_runtime.api.deps import ApiServices, services_of
from agent_runtime.api.errors import ApiError
from agent_runtime.api.fsguard import WorkspacePathError, validate_workspace_root
from agent_runtime.api.routes.sessions import session_out
from agent_runtime.api.schemas import (
    MemoryItemCreate,
    MemoryLayer,
    SessionOut,
    WorkspaceCreate,
    WorkspaceMemoryOut,
    WorkspaceOut,
    WorkspaceSettingsIn,
    WorkspaceSettingsOut,
    WorkspaceUpdate,
)
from agent_runtime.memory import Workspace, WorkspaceMemory
from agent_runtime.runtime.overrides import (
    WorkspaceSettings,
    load_workspace_settings,
    save_workspace_settings,
)
from agent_runtime.runtime.service import TaskStateConflictError

router = APIRouter(tags=["workspaces"])
Services = Annotated[ApiServices, Depends(services_of)]
WorkspaceId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


def workspace_out(w: Workspace) -> WorkspaceOut:
    return WorkspaceOut(
        workspace_id=w.workspace_id,
        name=w.name,
        root_path=w.root_path,
        description=w.description,
        created_at=w.created_at,
    )


def memory_out(m: WorkspaceMemory) -> WorkspaceMemoryOut:
    return WorkspaceMemoryOut(
        profile=list(m.profile),
        guidelines=list(m.guidelines),
        architecture=list(m.architecture),
        pitfalls=list(m.pitfalls),
    )


@router.post("/workspaces", response_model=WorkspaceOut)
async def create_workspace(
    body: WorkspaceCreate, response: Response, svc: Services
) -> WorkspaceOut:
    ws_cfg = svc.settings.server.workspace
    try:
        root = await asyncio.to_thread(
            validate_workspace_root,
            body.root_path,
            forbidden=ws_cfg.forbidden_roots,
            forbid_home=ws_cfg.forbid_home_itself,
            home=svc.home,
        )
    except WorkspacePathError as exc:
        raise ApiError(422, "WORKSPACE_PATH_INVALID", str(exc)) from exc
    existing = await svc.memory.workspace_by_path(str(root))
    if existing is not None:
        if body.workspace_id not in (None, existing.workspace_id):
            raise ApiError(
                409,
                "WORKSPACE_PATH_CONFLICT",
                "该路径已有工作区",
                {"workspace_id": existing.workspace_id},
            )
        return workspace_out(existing)  # 按路径幂等
    workspace_id = body.workspace_id or "ws_" + _short_id()
    await svc.memory.create_workspace(workspace_id, body.name, str(root), body.description)
    response.status_code = 201
    return workspace_out(await svc.memory.get_workspace(workspace_id))


@router.get("/workspaces", response_model=list[WorkspaceOut])
async def list_workspaces(svc: Services) -> list[WorkspaceOut]:
    return [workspace_out(w) for w in await svc.memory.list_workspaces()]


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(workspace_id: WorkspaceId, svc: Services) -> WorkspaceOut:
    return workspace_out(await svc.memory.get_workspace(workspace_id))


@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace(
    workspace_id: WorkspaceId, body: WorkspaceUpdate, svc: Services
) -> WorkspaceOut:
    return workspace_out(
        await svc.memory.update_workspace(
            workspace_id, name=body.name, description=body.description
        )
    )


@router.get("/workspaces/{workspace_id}/settings", response_model=WorkspaceSettingsOut)
async def get_workspace_settings(workspace_id: WorkspaceId, svc: Services) -> WorkspaceSettingsOut:
    await svc.memory.get_workspace(workspace_id)
    current = await load_workspace_settings(svc.memory, workspace_id)
    defaults = {
        "permission_level": svc.settings.runtime.approval.default_level,
        "model": None,
        "rules_enabled": svc.settings.context.rules.allow_workspace,
        "disabled_tools": [],
        "enabled_mcp_servers": None,
        "disabled_skills": [],
    }
    return WorkspaceSettingsOut(
        permission_level=current.permission_level,  # type: ignore[arg-type]
        model=current.model,
        rules_enabled=current.rules_enabled,
        disabled_tools=current.disabled_tools,
        enabled_mcp_servers=current.enabled_mcp_servers,
        disabled_skills=current.disabled_skills,
        defaults=defaults,
    )


@router.put("/workspaces/{workspace_id}/settings", response_model=WorkspaceSettingsOut)
@router.patch("/workspaces/{workspace_id}/settings", response_model=WorkspaceSettingsOut)
async def update_workspace_settings(
    workspace_id: WorkspaceId, body: WorkspaceSettingsIn, svc: Services
) -> WorkspaceSettingsOut:
    await svc.memory.get_workspace(workspace_id)
    if body.model is not None:
        known = {str(item.get("name")) for item in svc.introspection.models()}
        if body.model not in known:
            raise ApiError(422, "VALIDATION_ERROR", f"未知的模型端点：{body.model}")
    current = await load_workspace_settings(svc.memory, workspace_id)
    provided = body.model_dump(exclude_unset=True)
    settings = WorkspaceSettings(
        permission_level=(
            body.permission_level if "permission_level" in provided else current.permission_level
        ),
        model=body.model if "model" in provided else current.model,
        rules_enabled=body.rules_enabled if "rules_enabled" in provided else current.rules_enabled,
        disabled_tools=(
            body.disabled_tools
            if "disabled_tools" in provided and body.disabled_tools is not None
            else current.disabled_tools
        ),
        enabled_mcp_servers=(
            body.enabled_mcp_servers
            if "enabled_mcp_servers" in provided
            else current.enabled_mcp_servers
        ),
        disabled_skills=(
            body.disabled_skills
            if "disabled_skills" in provided and body.disabled_skills is not None
            else current.disabled_skills
        ),
    )
    await save_workspace_settings(svc.memory, workspace_id, settings)
    return await get_workspace_settings(workspace_id, svc)


@router.delete("/workspaces/{workspace_id}", status_code=204)
async def delete_workspace(workspace_id: WorkspaceId, svc: Services) -> Response:
    await svc.memory.get_workspace(workspace_id)
    task_ids = await svc.memory.task_ids_of_workspace(workspace_id)
    await _ensure_none_running(svc, task_ids)
    await svc.memory.delete_workspace(workspace_id)  # 元数据由外键级联删除
    for task_id in task_ids:
        await svc.tasks.forget_task(task_id)
    return Response(status_code=204)


async def _ensure_none_running(svc: ApiServices, task_ids: list[str]) -> None:
    for task_id in task_ids:
        record = await svc.tasks.get(task_id)
        if record.status == "running":
            raise TaskStateConflictError("有任务正在运行，先取消再删除")


# ------------------------------------------------------------------ 工作区记忆


@router.get("/workspaces/{workspace_id}/memory", response_model=WorkspaceMemoryOut)
async def get_workspace_memory(workspace_id: WorkspaceId, svc: Services) -> WorkspaceMemoryOut:
    return memory_out(await svc.memory.get_workspace_memory(workspace_id))


async def _append(
    svc: ApiServices, workspace_id: str, layer: MemoryLayer, text: str
) -> WorkspaceMemoryOut:
    current = await svc.memory.get_workspace_memory(workspace_id)
    items = tuple(getattr(current, layer))
    clean = text.strip()
    if not clean:
        raise ApiError(400, "VALIDATION_ERROR", "内容不能为空")
    if clean not in items:
        current = WorkspaceMemory(**{**_layers(current), layer: (*items, clean)})
        await svc.memory.set_workspace_memory(workspace_id, current)
    return memory_out(current)


def _layers(m: WorkspaceMemory) -> dict[str, tuple[str, ...]]:
    return {
        "profile": m.profile,
        "guidelines": m.guidelines,
        "architecture": m.architecture,
        "pitfalls": m.pitfalls,
    }


@router.post("/workspaces/{workspace_id}/memory/facts", response_model=WorkspaceMemoryOut)
async def add_fact(
    workspace_id: WorkspaceId, body: MemoryItemCreate, svc: Services
) -> WorkspaceMemoryOut:
    return await _append(svc, workspace_id, body.layer or "guidelines", body.text)


@router.post("/workspaces/{workspace_id}/memory/failures", response_model=WorkspaceMemoryOut)
async def add_failure(
    workspace_id: WorkspaceId, body: MemoryItemCreate, svc: Services
) -> WorkspaceMemoryOut:
    return await _append(svc, workspace_id, "pitfalls", body.text)


@router.delete(
    "/workspaces/{workspace_id}/memory/{kind}/{index}", response_model=WorkspaceMemoryOut
)
async def delete_memory_item(
    workspace_id: WorkspaceId,
    kind: Literal["profile", "guidelines", "architecture", "pitfalls"],
    index: Annotated[int, Path(ge=0)],
    svc: Services,
) -> WorkspaceMemoryOut:
    current = await svc.memory.get_workspace_memory(workspace_id)
    items = list(getattr(current, kind))
    if index >= len(items):
        raise ApiError(404, "MEMORY_ITEM_NOT_FOUND", "该条记忆不存在")
    del items[index]
    updated = WorkspaceMemory(**{**_layers(current), kind: tuple(items)})
    await svc.memory.set_workspace_memory(workspace_id, updated)
    return memory_out(updated)


@router.post("/workspaces/{workspace_id}/sessions", response_model=SessionOut, status_code=201)
async def create_session(workspace_id: WorkspaceId, svc: Services) -> SessionOut:
    session_id = "sess_" + _short_id()
    await svc.memory.create_session(session_id, workspace_id)
    return session_out(await svc.memory.get_session(session_id))


@router.get("/workspaces/{workspace_id}/sessions", response_model=list[SessionOut])
async def list_sessions(workspace_id: WorkspaceId, svc: Services) -> list[SessionOut]:
    return [session_out(s) for s in await svc.memory.list_sessions(workspace_id)]


def _short_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]

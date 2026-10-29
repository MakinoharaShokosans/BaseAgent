"""MCP 管理：总开关、逐服务器开关、暴露方式、工具级开关、重连。

服务器定义仍在 ``config.toml``；这里只改数据库覆盖层。覆盖层在任务装配时读一次，
运行中的连接保留但不再暴露，下一次任务按新状态装配。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path

from agent_runtime.api.deps import ApiServices, services_of
from agent_runtime.api.errors import ApiError
from agent_runtime.api.schemas import McpServerIn, McpSettingsIn, McpSettingsOut
from agent_runtime.runtime.overrides import McpPolicy, load_mcp_policy, save_mcp_policy

router = APIRouter(tags=["mcp"])
Services = Annotated[ApiServices, Depends(services_of)]
ServerName = Annotated[str, Path(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]


def _manager(svc: ApiServices) -> Any:
    if svc.mcp_control is None:
        raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "MCP 未启用")
    return svc.mcp_control


def _apply(svc: ApiServices, policy: McpPolicy) -> None:
    manager = _manager(svc)
    manager.apply_policy(
        enabled=policy.enabled,
        servers={name: server.to_dict() for name, server in policy.servers.items()},
    )


@router.get("/mcp/settings", response_model=McpSettingsOut)
async def get_mcp_settings(svc: Services) -> McpSettingsOut:
    if svc.mcp_control is None:
        return McpSettingsOut(enabled=False)
    return McpSettingsOut(enabled=svc.mcp_control.global_enabled)


@router.put("/mcp/settings", response_model=McpSettingsOut)
async def put_mcp_settings(body: McpSettingsIn, svc: Services) -> McpSettingsOut:
    policy = await load_mcp_policy(svc.memory)
    policy.enabled = body.enabled
    await save_mcp_policy(svc.memory, policy)
    _apply(svc, policy)
    return McpSettingsOut(enabled=_manager(svc).global_enabled)


@router.get("/mcp/servers")
async def list_servers(svc: Services) -> list[dict[str, Any]]:
    if svc.mcp_control is None:
        return svc.introspection.mcp_servers()
    return svc.mcp_control.status()


@router.put("/mcp/servers/{name}")
async def update_server(name: ServerName, body: McpServerIn, svc: Services) -> dict[str, Any]:
    manager = _manager(svc)
    current = manager.server_status(name)
    if current is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", f"没有 MCP 服务器 {name}")
    policy = await load_mcp_policy(svc.memory)
    server = policy.server(name)
    if body.enabled is not None:
        server.enabled = body.enabled
    if body.exposure is not None:
        server.exposure = body.exposure
    if body.disabled_tools is not None:
        server.disabled_tools = [tool for tool in dict.fromkeys(body.disabled_tools) if tool]
    policy.servers[name] = server
    await save_mcp_policy(svc.memory, policy)
    _apply(svc, policy)
    updated = manager.server_status(name)
    if updated is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", f"没有 MCP 服务器 {name}")
    return updated


@router.post("/mcp/servers/{name}/reconnect")
async def reconnect(name: ServerName, svc: Services) -> dict[str, Any]:
    manager = _manager(svc)
    updated = await manager.reconnect(name)
    if updated is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", f"没有 MCP 服务器 {name}")
    return updated

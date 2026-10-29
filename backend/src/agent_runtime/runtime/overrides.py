"""界面设置的数据库覆盖层。

配置文件是基线，界面改动只写 ``app_settings``，不碰 ``config.toml``。任务装配时读一次快照，
所以改动只在下一个任务生效；运行中的任务不受影响。

键约定：
- ``skills.policy``：来源开关与逐技能禁用
- ``mcp.policy``：总开关、逐服务器开关、暴露方式、被禁用的工具
- ``workspace.settings.<id>``：工作区默认权限、默认模型、项目规则开关
- ``prompt.<name>`` / ``system_prompt``：提示词覆盖
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from agent_runtime.memory import SYSTEM_PROMPT_KEY, MemoryStore

SKILL_POLICY_KEY: Final = "skills.policy"
MCP_POLICY_KEY: Final = "mcp.policy"
WORKSPACE_SETTINGS_PREFIX: Final = "workspace.settings."

SkillSourceName = Literal["builtin", "global", "workspace"]
_SKILL_SOURCES: Final = ("builtin", "global", "workspace")
_EXPOSURES: Final = ("pinned", "deferred")


@dataclass(slots=True)
class SkillPolicy:
    #: 只记录用户显式改过的来源；缺省沿用配置
    sources: dict[str, bool] = field(default_factory=dict)
    #: 被禁用的技能名
    disabled: set[str] = field(default_factory=set)

    def source_enabled(self, source: SkillSourceName, default: bool) -> bool:
        return bool(self.sources.get(source, default))

    def to_json(self) -> str:
        return json.dumps(
            {"sources": self.sources, "disabled": sorted(self.disabled)}, ensure_ascii=False
        )

    @classmethod
    def from_json(cls, raw: str | None) -> SkillPolicy:
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except ValueError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        sources: dict[str, bool] = {}
        raw_sources = data.get("sources")
        if isinstance(raw_sources, dict):
            for name in _SKILL_SOURCES:
                value = raw_sources.get(name)
                if isinstance(value, bool):
                    sources[name] = value
        raw_disabled = data.get("disabled")
        disabled = (
            {str(name) for name in raw_disabled if isinstance(name, str)}
            if isinstance(raw_disabled, list)
            else set()
        )
        return cls(sources=sources, disabled=disabled)


@dataclass(slots=True)
class McpServerPolicy:
    enabled: bool | None = None
    exposure: str | None = None
    disabled_tools: list[str] = field(default_factory=list[str])

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "exposure": self.exposure,
            "disabled_tools": self.disabled_tools,
        }

    @classmethod
    def from_dict(cls, data: Any) -> McpServerPolicy:
        if not isinstance(data, dict):
            return cls()
        enabled = data.get("enabled")
        exposure = data.get("exposure")
        tools = data.get("disabled_tools")
        return cls(
            enabled=enabled if isinstance(enabled, bool) else None,
            exposure=exposure if exposure in _EXPOSURES else None,
            disabled_tools=(
                [str(t) for t in tools if isinstance(t, str)] if isinstance(tools, list) else []
            ),
        )


@dataclass(slots=True)
class McpPolicy:
    #: None 表示沿用配置里的总开关
    enabled: bool | None = None
    servers: dict[str, McpServerPolicy] = field(default_factory=dict[str, McpServerPolicy])

    def server(self, name: str) -> McpServerPolicy:
        return self.servers.get(name) or McpServerPolicy()

    def to_json(self) -> str:
        return json.dumps(
            {
                "enabled": self.enabled,
                "servers": {name: policy.to_dict() for name, policy in self.servers.items()},
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | None) -> McpPolicy:
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except ValueError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        enabled = data.get("enabled")
        servers: dict[str, McpServerPolicy] = {}
        raw_servers = data.get("servers")
        if isinstance(raw_servers, dict):
            for name, value in raw_servers.items():
                if isinstance(name, str):
                    servers[name] = McpServerPolicy.from_dict(value)
        return cls(enabled=enabled if isinstance(enabled, bool) else None, servers=servers)


@dataclass(slots=True)
class WorkspaceSettings:
    permission_level: str | None = None
    model: str | None = None
    rules_enabled: bool | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "permission_level": self.permission_level,
            "model": self.model,
            "rules_enabled": self.rules_enabled,
        }

    @classmethod
    def from_dict(cls, data: Any) -> WorkspaceSettings:
        if not isinstance(data, dict):
            return cls()
        level = data.get("permission_level")
        model = data.get("model")
        rules = data.get("rules_enabled")
        return cls(
            permission_level=level if isinstance(level, str) and level else None,
            model=model if isinstance(model, str) and model else None,
            rules_enabled=rules if isinstance(rules, bool) else None,
        )


# ------------------------------------------------------------------ 读写


async def load_skill_policy(store: MemoryStore) -> SkillPolicy:
    return SkillPolicy.from_json(await store.get_app_setting(SKILL_POLICY_KEY))


async def save_skill_policy(store: MemoryStore, policy: SkillPolicy) -> None:
    await store.set_app_setting(SKILL_POLICY_KEY, policy.to_json())


async def load_mcp_policy(store: MemoryStore) -> McpPolicy:
    return McpPolicy.from_json(await store.get_app_setting(MCP_POLICY_KEY))


async def save_mcp_policy(store: MemoryStore, policy: McpPolicy) -> None:
    await store.set_app_setting(MCP_POLICY_KEY, policy.to_json())


def workspace_settings_key(workspace_id: str) -> str:
    return f"{WORKSPACE_SETTINGS_PREFIX}{workspace_id}"


async def load_workspace_settings(store: MemoryStore, workspace_id: str) -> WorkspaceSettings:
    raw = await store.get_app_setting(workspace_settings_key(workspace_id))
    if not raw:
        return WorkspaceSettings()
    try:
        data = json.loads(raw)
    except ValueError:
        return WorkspaceSettings()
    return WorkspaceSettings.from_dict(data)


async def save_workspace_settings(
    store: MemoryStore, workspace_id: str, settings: WorkspaceSettings
) -> None:
    await store.set_app_setting(
        workspace_settings_key(workspace_id),
        json.dumps(settings.to_dict(), ensure_ascii=False),
    )


def prompt_key(name: str) -> str:
    """系统提示词沿用旧键，其他模板用 ``prompt.<name>``。"""
    return SYSTEM_PROMPT_KEY if name == "system" else f"prompt.{name}"

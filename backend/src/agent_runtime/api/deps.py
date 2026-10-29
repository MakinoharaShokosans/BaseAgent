"""API 的依赖容器：路由只通过它拿到运行时门面与配置，不碰内部。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Request

from agent_runtime.context import PromptLibrary
from agent_runtime.memory import CompactionResult, MemoryStore
from agent_runtime.observability import EventHub, Redactor
from agent_runtime.runtime.service import TaskService
from baseagent_config import Settings

JsonList = Callable[[], list[dict[str, Any]]]


async def _no_checks() -> Mapping[str, str]:
    return {}


@dataclass(slots=True)
class Introspection:
    """“系统此刻装配了什么”的只读视图。装配层提供，缺省为空。"""

    skills: JsonList = lambda: []
    tools: JsonList = lambda: []
    mcp_servers: JsonList = lambda: []
    models: JsonList = lambda: []


@dataclass(slots=True)
class ApiServices:
    settings: Settings
    memory: MemoryStore
    tasks: TaskService
    hub: EventHub
    redactor: Redactor
    #: 工作区文件接口的根之外的家目录（用于 ``~`` 展开与主目录自身检查）
    home: Path = field(default_factory=Path.home)
    introspection: Introspection = field(default_factory=Introspection)
    #: 内置提示词；设置接口用它给出"恢复默认"的原文
    prompts: PromptLibrary = field(default_factory=PromptLibrary)
    dependency_checks: Callable[[], Awaitable[Mapping[str, str]]] = _no_checks
    #: 会话的上下文检视；装配层提供，缺省不可用
    context_inspector: Callable[[str], Awaitable[Mapping[str, Any]]] | None = None
    #: 单段提示词的渲染全文；缺省不可用
    context_segment: Callable[[str, str], Awaitable[Mapping[str, Any] | None]] | None = None
    #: 手动压缩会话记忆；装配层提供，缺省不可用
    compact_session: Callable[[str], Awaitable[CompactionResult]] | None = None
    #: 唤起宿主机原生目录选择窗口；缺省不可用
    native_picker: Callable[[float], Awaitable[str | None]] | None = None
    #: 按首轮对话生成会话标题；缺省不生成
    title_generator: Callable[[str], Awaitable[str | None]] | None = None
    #: 技能目录（含各工作区来源）；缺省只用自省里的内置/全局
    skill_catalog: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None
    #: 读取某个技能的正文（工作区来源需给出 workspace_id）
    skill_body: Callable[[str, str | None], Awaitable[dict[str, Any] | None]] | None = None
    #: MCP 运行时覆盖控制，缺省不可用
    mcp_control: Any | None = None


def services_of(request: Request) -> ApiServices:
    return request.app.state.services  # type: ignore[no-any-return]

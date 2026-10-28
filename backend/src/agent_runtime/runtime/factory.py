"""任务级运行时的装配：把网关、工具、护栏、提示词、事件接成一个 ``TaskRuntime``。

每次运行与续跑都重建（预算守卫、工具绑定、事件序号等都不入检查点）。这里是装配层：
只组合，不含业务规则。设计文档：docs/01_architecture/03_code_layout_and_roadmap.md。
"""

from __future__ import annotations

import json
import platform
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, Final

from loguru import logger

from agent_runtime.checkpoint import CheckpointStore
from agent_runtime.context import ObservationGovernor, ObservationStore, PromptLibrary, RulesCache
from agent_runtime.core import AgentState, AgentTool, Message, ToolSchema, new_state
from agent_runtime.guardrails.envelope import observation_envelope
from agent_runtime.llm import LLMGateway, PreferredEndpoint
from agent_runtime.llm.tokens import estimate_tokens
from agent_runtime.loop import (
    McpProxy,
    ModelDeps,
    TaskRuntime,
    ToolRunnerDeps,
    make_model_step,
    make_tool_runner,
    scratch_tools,
)
from agent_runtime.memory import SYSTEM_PROMPT_KEY, MemoryStore, SessionPreferences
from agent_runtime.observability import EventHub, Redactor
from agent_runtime.runtime.assembly import (
    APPROVAL_HOOKS,
    assembler_from_settings,
    build_prompt_assets,
    file_limits_from_settings,
    loop_limits_from_settings,
    observation_limits_from_settings,
    permission_policy_from_settings,
    rules_from_settings,
    skill_limits_from_settings,
    skill_roots_from_settings,
)
from agent_runtime.runtime.events import TaskEvents
from agent_runtime.runtime.memory_flow import MemoryFlow
from agent_runtime.skills import SkillRegistry
from agent_runtime.subagents.delegate import (
    DelegateLimits,
    RunnerLimits,
    SpawnSubagentTool,
    SubagentRunner,
)
from agent_runtime.websearch import (
    ReportLimits,
    SidecarSearchClient,
    WebSearchLimits,
    WebSearchRunner,
    WebSearchTool,
)
from baseagent_config import LoadedSettings
from mcps import McpCallTool, McpDescribeTool, McpManager, McpTool, render_index
from tools.builtin import ShellBinding, TaskArtifacts, build_builtin_tools
from tools.core import SidecarClient, ToolDispatcher, ToolRegistry

#: 内置工具的幂等性（重复执行是否安全）：崩溃恢复据此决定重试还是标记“结果未知”
_IDEMPOTENT: Final = frozenset(
    {"view_file", "list_dir", "grep", "read_artifact", "load_skill", "update_plan", "mcp_describe"}
)


@dataclass(slots=True)
class _Assembled:
    """``RuntimeFactory._assemble`` 的结果：一个任务发给模型的东西，不含运行时。"""

    events: TaskEvents
    artifacts: TaskArtifacts
    registry: ToolRegistry
    deferred: list[str]
    pinned: list[str]
    context: Any  # SessionContext
    gateway: Any
    assets: Any  # PromptAssets
    assembler: Any  # PromptAssembler


#: 检视用的占位需求：只是让装配器把会话里的历史对话放进请求里，它自己不计入任何一段
_PROBE_GOAL: Final = "（占位）"


@dataclass(slots=True)
class RuntimeFactory:
    loaded: LoadedSettings
    gateway: LLMGateway
    checkpoints: CheckpointStore
    memory: MemoryStore
    hub: EventHub
    redactor: Redactor
    flow: MemoryFlow
    shell: SidecarClient | None = None
    web: SidecarClient | None = None
    #: MCP 外部工具；没有任何启用的服务器时它什么也不做
    mcp: McpManager | None = None
    library: PromptLibrary = field(default_factory=PromptLibrary)
    rules_cache: RulesCache = field(default_factory=RulesCache)
    count_tokens: Callable[[str], int] = estimate_tokens
    clock: Callable[[], float] = time.monotonic
    #: 任务累计 Token（统计值）；没有接入时恒为 0
    usage: Callable[[str], int] = lambda task_id: 0
    #: 会话级“永久放行”的签名集合：跨续跑存活，不入检查点
    _allow: defaultdict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def is_idempotent(self, name: str) -> bool:
        return name in _IDEMPOTENT or (self.mcp is not None and self.mcp.is_idempotent(name))

    def make_canary(self, session_id: str, task_id: str) -> str:
        import hashlib

        salt = self._canary_salt()
        digest = hashlib.sha256(f"{salt}:{session_id}:{task_id}".encode()).hexdigest()
        return f"cny-{digest[:24]}"

    def _canary_salt(self) -> str:
        path = self.loaded.paths.canary_salt
        if not path.is_file():
            import os
            import secrets

            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(secrets.token_hex(32))
        return path.read_text(encoding="utf-8").strip()

    # ------------------------------------------------------------------ 构建

    async def _assemble(self, state: AgentState) -> _Assembled:
        """任务的工具表与冻结的提示词资产。真实任务（``build``）与上下文检视共用同一套装配。"""
        settings = self.loaded.settings
        paths = self.loaded.paths
        workspace = await self.memory.get_workspace(state.workspace_id)
        root = Path(state.workspace_path)

        artifacts = TaskArtifacts()
        skills = SkillRegistry.scan(
            skill_roots_from_settings(settings, root), skill_limits_from_settings(settings)
        )
        events = TaskEvents(
            self.hub,
            state.task_id,
            self.redactor,
            canary=state.canary_token,
            tool_result_clip=settings.events.tool_result_clip_chars,
            turn_offset=await self.hub.count(state.task_id, "turn.finished"),
            usage=lambda: self.usage(state.task_id),
        )
        tools = build_builtin_tools(
            workspace_root=root,
            files=file_limits_from_settings(settings),
            artifacts=artifacts,
            artifacts_root=paths.artifacts_dir,
            grep_max_results=settings.tools.files.grep_max_results,
            backup_dir=self.loaded.home / "backups" / state.task_id,
            ignored_dirs=settings.tools.files.ignored_dirs,
            list_dir_max_entries=settings.tools.files.list_dir_max_entries,
            shell=self._shell_binding(state, root),
            skills=skills,
        )
        tools.extend(scratch_tools())
        websearch = self._websearch_tool(state, events)
        if websearch is not None:
            tools.append(websearch)
        mcp_tools = await self._mcp_tools()
        tools.extend(mcp_tools)
        deferred = [t.name for t in mcp_tools if self._is_deferred(t)]
        pinned = [t.name for t in mcp_tools if t.name not in set(deferred)]
        by_name = {t.name: t for t in tools}
        extra_untrusted: list[str] = []
        if deferred:
            # 代理工具：索引里的工具经 mcp_describe 取定义，再经 mcp_call 调用（决策 0007）
            tools.append(
                McpDescribeTool(
                    lambda n: _hidden_schema(by_name, set(deferred), n),
                    lambda: sorted(deferred),
                )
            )
            tools.append(McpCallTool())
            extra_untrusted.append("mcp_describe")
        parent_tools = {t.name: t for t in tools}  # 派发时刻父工具表的快照：子级只能是它的子集
        spawn = self._spawn_tool(state, events, parent_tools)
        if spawn is not None:
            tools.append(spawn)
        # 不可信工具必须逐名授权：研究工具，加上用户在配置里启用的 MCP 服务器通过消毒的工具
        untrusted = [t.name for t in (websearch, *mcp_tools) if t is not None] + extra_untrusted
        registry = ToolRegistry(
            tools,
            allow_untrusted=bool(untrusted),
            untrusted_allowlist=untrusted or None,
            hidden=deferred,
        )
        context = await self.flow.prepare(state.session_id)
        prefs = SessionPreferences.from_mapping(
            await self.memory.get_session_settings(state.session_id)
        )
        # 用户选的模型只作为主 Agent 的首选端点；研究与子智能体仍走各自配置的档位
        gateway = PreferredEndpoint(self.gateway, prefs.model) if prefs.model else self.gateway
        mcp_cfg = settings.mcp
        index = render_index(
            [(t.server, t.name, t.description) for t in mcp_tools if t.name in set(deferred)],
            max_listed=mcp_cfg.index_max_listed,
            desc_chars=mcp_cfg.index_desc_chars,
        )
        base = build_prompt_assets(
            self.library,
            workspace_name=workspace.name,
            workspace_description=workspace.description,
            rules=rules_from_settings(settings, root, self.rules_cache),
            skills=skills,
            mcp_index=index,
            tool_names=[s.name for s in registry.schemas()],
        )
        system_override = await self.memory.get_app_setting(SYSTEM_PROMPT_KEY)
        if system_override:
            base = replace(base, system=system_override)
        assets = replace(context.apply_to(base), environment=self._environment(root))
        assembler = assembler_from_settings(
            settings, assets, self.count_tokens, context_window=self._context_window()
        )
        return _Assembled(
            events=events,
            artifacts=artifacts,
            registry=registry,
            deferred=deferred,
            pinned=pinned,
            context=context,
            gateway=gateway,
            assets=assets,
            assembler=assembler,
        )

    async def build(self, state: AgentState) -> TaskRuntime:
        settings = self.loaded.settings
        paths = self.loaded.paths
        a = await self._assemble(state)
        events, artifacts, registry = a.events, a.artifacts, a.registry
        deferred, pinned, gateway, assembler = a.deferred, a.pinned, a.gateway, a.assembler

        loop_cfg = settings.runtime.loop
        limits = loop_limits_from_settings(settings)
        model = make_model_step(
            ModelDeps(
                gateway=gateway,
                assemble=assembler,
                tools=registry.schemas(),
                tier=loop_cfg.model_tier,
                call_timeout_sec=float(loop_cfg.call_timeout_sec),
            )
        )
        tool_runner = make_tool_runner(
            ToolRunnerDeps(
                dispatcher=ToolDispatcher(registry),
                policy=permission_policy_from_settings(settings),
                limits=limits,
                governor=ObservationGovernor(
                    observation_limits_from_settings(settings),
                    self.count_tokens,
                    ObservationStore(paths.artifacts_dir),
                ),
                approval=APPROVAL_HOOKS,
                allowlist=self._allow[state.session_id],
                emit=events.emit,
                envelope=observation_envelope,
                bind=lambda s: artifacts.bind(s.artifacts),
                proxy=McpProxy(deferred, pinned) if deferred else None,
            )
        )
        return TaskRuntime(
            model=model,
            tools=tool_runner,
            checkpoints=self.checkpoints,
            events=events,
            max_consecutive_blocks=limits.max_consecutive_blocks,
        )

    async def inspect_context(self, session_id: str) -> Mapping[str, Any]:
        """会话下一次请求里各段提示词占多少 Token。

        与真实任务同一套装配（``_assemble``）与同一个装配器，所以数字就是实际会发给模型的内容，
        不是前端另外估的。系统提示词被用户改写后、记忆变化后、工具或技能变化后，再取一次就是新的。
        """
        info = await self.memory.get_session(session_id)
        workspace = await self.memory.get_workspace(info.workspace_id)
        probe = new_state(
            workspace_id=info.workspace_id,
            workspace_path=workspace.root_path,
            session_id=session_id,
            task_id="inspect",
            task_goal=_PROBE_GOAL,
            permission_level="read_only",
            canary_token=self.make_canary(session_id, "inspect"),
        )
        a = await self._assemble(probe)
        state = replace(
            probe, messages=[*a.context.history, Message(role="user", content=_PROBE_GOAL)]
        )
        layers = a.assembler.describe(state)
        count = self.count_tokens
        system_message = a.assembler(state)[0]
        tools_json = json.dumps(
            [
                {"name": t.name, "description": t.description, "parameters": dict(t.parameters)}
                for t in a.registry.schemas()
            ],
            ensure_ascii=False,
        )
        history = sum(count(m.content) for m in a.context.history)
        override = await self.memory.get_app_setting(SYSTEM_PROMPT_KEY)

        def seg(sid: str, tokens: int, **extra: object) -> dict[str, object]:
            return {"id": sid, "tokens": tokens, **extra}

        segments = [
            seg("system", count(system_message.content), customized=bool(override)),
            seg("tools", count(tools_json), count=layers["tools"]["count"]),
            seg("skills", layers["skills"]["tokens"]),
            seg("mcp_index", layers["mcp_index"]["tokens"]),
            seg(
                "project_rules",
                layers["project_rules"]["tokens"],
                status=layers["project_rules"]["status"],
            ),
            seg("workspace", count(_workspace_text(layers))),
            seg("workspace_memory", layers["workspace_memory"]["tokens"]),
            seg("session_memory", layers["session_memory"]["tokens"]),
            seg("task_data", layers["task_data"]["tokens"]),
            seg("history", history, turns=len(a.context.history)),
        ]
        return {
            "segments": segments,
            "total_tokens": sum(int(str(x["tokens"])) for x in segments),
            "context_window": self._context_window(),
            "skipped": layers["skipped"],
        }

    def _is_deferred(self, tool: McpTool) -> bool:
        return self.mcp is not None and self.mcp.exposure_of(tool.server) == "deferred"

    async def _mcp_tools(self) -> list[McpTool]:
        """MCP 是增强项：任何故障都只是少几个工具，绝不让任务起不来。"""
        if self.mcp is None:
            return []
        try:
            return list(await self.mcp.tools())
        except Exception as exc:
            logger.warning("准备 MCP 工具失败，本任务不带外部工具：{!r}", exc)
            return []

    def _websearch_tool(self, state: AgentState, events: TaskEvents) -> WebSearchTool | None:
        """研究是主 Agent 获取外部信息的唯一入口；关闭或没有 sidecar 客户端时根本不注册。"""
        cfg = self.loaded.settings.websearch
        if not cfg.enabled or self.web is None:
            return None
        runner = WebSearchRunner(
            self.gateway,
            SidecarSearchClient(
                self.web, self.loaded.paths.web_artifacts_dir, max_source_chars=cfg.max_source_chars
            ),
            WebSearchLimits(
                cfg.max_rounds,
                cfg.max_sources,
                cfg.max_source_chars,
                ReportLimits(
                    cfg.max_findings,
                    cfg.max_answer_chars,
                    cfg.max_code_examples,
                    cfg.max_code_chars,
                    cfg.max_report_chars,
                ),
            ),
            plan_prompt=self.library.get("websearch_plan"),
            extract_prompt=self.library.get("websearch_extract"),
            tier=cfg.model_tier,
            emit=events.emit,
            extract_reserve_sec=float(cfg.extract_reserve_sec),
        )
        return WebSearchTool(
            runner,
            task_id=state.task_id,
            max_wall_time_sec=float(cfg.max_wall_time_sec),
            max_calls=cfg.max_calls_per_task,
            max_report_chars=cfg.max_report_chars,
        )

    def _spawn_tool(
        self,
        state: AgentState,
        events: TaskEvents,
        parent_tools: Mapping[str, AgentTool],
    ) -> SpawnSubagentTool | None:
        cfg = self.loaded.settings.subagent
        if not cfg.enabled:
            return None
        policy = permission_policy_from_settings(self.loaded.settings)
        limits = RunnerLimits(
            max_steps=cfg.max_steps,
            max_observation_chars=cfg.max_observation_chars,
            report=DelegateLimits(
                cfg.max_findings, cfg.max_answer_chars, cfg.max_citations, cfg.max_report_chars
            ),
        )

        def make_runner(registry: ToolRegistry) -> SubagentRunner:
            return SubagentRunner(
                self.gateway,
                registry,
                policy,
                state.permission_level,
                limits,
                system_prompt=self.library.get("subagent"),
                extract_prompt=self.library.get("subagent_report"),
                tier=cfg.model_tier,
                canary=state.canary_token,
                depth=1,
                emit=events.emit,
            )

        return SpawnSubagentTool(
            make_runner,
            lambda: parent_tools,
            enabled=True,
            depth=0,
            max_depth=cfg.max_depth,
            denied_tools=cfg.denied_tools,
            max_assigned_tools=cfg.max_assigned_tools,
            max_steps=cfg.max_steps,
            max_wall_time_sec=float(cfg.max_wall_time_sec),
            max_report_chars=cfg.max_report_chars,
        )

    def skill_listing(self) -> list[dict[str, object]]:
        """自省用：内置与全局技能（工作区技能随工作区变化，不在这里）。"""
        settings = self.loaded.settings
        registry = SkillRegistry.scan(
            skill_roots_from_settings(
                settings, self.loaded.home, global_dir=self.loaded.home / "skills"
            ),
            skill_limits_from_settings(settings),
        )
        out: list[dict[str, object]] = []
        for name in registry.names():
            record = registry.get(name)
            if record is not None:
                out.append(
                    {
                        "name": record.name,
                        "source": record.source,
                        "description": record.description,
                        "triggers": list(record.triggers),
                        "required_tools": list(record.required_tools),
                        "high_privilege": list(record.high_privilege),
                        "warnings": list(record.warnings),
                    }
                )
        return out

    def tool_listing(self) -> list[dict[str, object]]:
        """自省用：内置工具的名称与 Schema（不含任务相关的绑定）。"""
        registry = ToolRegistry(
            build_builtin_tools(
                workspace_root=self.loaded.home,
                files=file_limits_from_settings(self.loaded.settings),
                artifacts=TaskArtifacts(),
                artifacts_root=self.loaded.paths.artifacts_dir,
                grep_max_results=self.loaded.settings.tools.files.grep_max_results,
            )
        )
        return [
            {"name": s.name, "description": s.description, "parameters": dict(s.parameters)}
            for s in registry.schemas()
        ]

    def _shell_binding(self, state: AgentState, root: Path) -> ShellBinding | None:
        if self.shell is None:
            return None
        cfg = self.loaded.settings.bash_shell
        return ShellBinding(
            client=self.shell,
            workspace_id=state.workspace_id,
            workspace_root=str(root),
            task_id=state.task_id,
            default_timeout_sec=float(cfg.timeout_sec),
            max_timeout_sec=float(cfg.max_timeout_sec),
        )

    def _context_window(self) -> int:
        endpoints = self.loaded.endpoints.get("fast", ())
        windows = [e.context_window for e in endpoints]
        return min(windows) if windows else 64_000

    @staticmethod
    def _environment(root: Path) -> tuple[tuple[str, str], ...]:
        return (
            ("工作区根目录", str(root)),
            ("操作系统", f"{platform.system()} {platform.release()}"),
            ("今天", date.today().isoformat()),
        )


def _hidden_schema(
    tools: Mapping[str, AgentTool], deferred: set[str], name: str
) -> ToolSchema | None:
    tool = tools.get(name)
    return tool.schema() if tool is not None and name in deferred else None


def _workspace_text(layers: Mapping[str, Any]) -> str:
    """“工作区”一段只有名称与路径，按它们的文字估算。"""
    workspace = layers["workspace"]
    return f"{workspace['name']}{workspace['path']}"

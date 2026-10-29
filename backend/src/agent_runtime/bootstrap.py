"""应用的组合根：打开存储、装配网关/工具/运行时/服务，交出 API 需要的依赖。

进程里只有这一处把所有部件接到一起；其余代码都只依赖自己的协议。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import httpx
from loguru import logger

from agent_runtime.api import ApiServices, Introspection
from agent_runtime.checkpoint import CheckpointStore, scan_for_recovery
from agent_runtime.context import PromptLibrary
from agent_runtime.core import Message
from agent_runtime.llm import LLMGateway, LLMRequest
from agent_runtime.llm.tokens import estimate_tokens
from agent_runtime.memory import MemoryStore, SessionPreferences
from agent_runtime.observability import (
    EventHub,
    EventStore,
    Redactor,
    TrajectoryRecorder,
    setup_logging,
)
from agent_runtime.runtime.assembly import (
    max_concurrent_tasks,
    memory_flow_from_settings,
    resolve_permission_level,
    skill_limits_from_settings,
    skill_roots_from_settings,
)
from agent_runtime.runtime.events import LlmCallEvents
from agent_runtime.runtime.extractor import GatewayExtractor
from agent_runtime.runtime.factory import RuntimeFactory
from agent_runtime.runtime.overrides import (
    load_mcp_policy,
    load_skill_policy,
    prompt_key,
)
from agent_runtime.runtime.run import RunnerDeps
from agent_runtime.runtime.runner import TaskRunner
from agent_runtime.runtime.service import TaskService
from agent_runtime.skills import SkillRegistry
from baseagent_config import LoadedSettings
from mcps import McpManager
from tools.core import SidecarClient


def redactor_from_settings(loaded: LoadedSettings) -> Redactor:
    r = loaded.settings.redaction
    return Redactor(r.key_patterns, r.value_patterns, enabled=r.enabled)


@dataclass(slots=True)
class Application:
    loaded: LoadedSettings
    memory: MemoryStore
    checkpoints: CheckpointStore
    events: EventStore | None
    hub: EventHub
    factory: RuntimeFactory
    runner: TaskRunner
    tasks: TaskService
    shell: SidecarClient
    web: SidecarClient
    redactor: Redactor
    mcp: McpManager | None = None

    @classmethod
    async def create(
        cls,
        loaded: LoadedSettings,
        *,
        gateway: Any | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Self:
        """``gateway`` 仅供测试注入；缺省按配置装配真实网关。"""
        settings, paths = loaded.settings, loaded.paths
        memory = MemoryStore(paths.metadata_db)
        checkpoints = CheckpointStore(paths.checkpoint_db)
        events = EventStore(paths.events_db) if settings.events.persist else None
        opened: list[Any] = []
        try:
            for store in (memory, checkpoints, events):
                if store is not None:
                    await store.open()
                    opened.append(store)
        except BaseException:
            for store in reversed(opened):
                await store.close()
            raise

        redactor = redactor_from_settings(loaded)
        hub = EventHub(
            events,
            buffer_events=settings.server.sse_buffer_events,
            subscriber_queue=settings.server.sse_subscriber_queue,
            clip_chars=settings.events.summary_clip_chars,
        )
        trajectory = TrajectoryRecorder(paths.traces_dir)
        hub.add_sink(trajectory.record)
        llm_events = LlmCallEvents(hub)
        llm = gateway or LLMGateway.from_loaded(loaded, env=env, observer=llm_events)
        library = PromptLibrary()
        for name in library.names():
            override = await memory.get_app_setting(prompt_key(name))
            if override:
                library.set_override(name, override)
        flow = memory_flow_from_settings(
            settings, memory, GatewayExtractor(llm, library), estimate_tokens
        )
        shell = SidecarClient(
            settings.services.shell_url, timeout_sec=float(settings.services.timeout_sec)
        )
        web = SidecarClient(
            settings.services.web_url, timeout_sec=float(settings.services.timeout_sec)
        )
        mcp = McpManager(settings.mcp, environ=env) if settings.mcp.enabled else None
        if mcp is not None:
            mcp_policy = await load_mcp_policy(memory)
            mcp.apply_policy(
                enabled=mcp_policy.enabled,
                servers={name: server.to_dict() for name, server in mcp_policy.servers.items()},
            )
        factory = RuntimeFactory(
            loaded=loaded,
            gateway=llm,
            checkpoints=checkpoints,
            memory=memory,
            hub=hub,
            redactor=redactor,
            flow=flow,
            shell=shell,
            web=web,
            mcp=mcp,
            library=library,
            usage=llm_events.tokens_of,
        )
        runner = TaskRunner(
            RunnerDeps(
                checkpoints=checkpoints,
                build_runtime=factory.build,
                is_idempotent=factory.is_idempotent,
                make_canary=factory.make_canary,
            ),
            max_concurrent=max_concurrent_tasks(settings),
        )
        tasks = TaskService(
            runner=runner,
            memory=memory,
            checkpoints=checkpoints,
            hub=hub,
            redactor=redactor,
            resolve_level=lambda level, confirmed: resolve_permission_level(
                settings, level, confirmed=confirmed
            ),
            goal_max_chars=settings.server.task_goal_max_chars,
            flow=flow,
            artifacts_root=paths.artifacts_dir,
            traces_root=paths.traces_dir,
            trajectory=trajectory,
            usage=llm_events.tokens_of,
        )
        runner.attach_observer(tasks.observer(llm_events.drain))
        app = cls(
            loaded,
            memory,
            checkpoints,
            events,
            hub,
            factory,
            runner,
            tasks,
            shell,
            web,
            redactor,
            mcp,
        )
        await app._recover()
        return app

    async def _recover(self) -> None:
        report = await scan_for_recovery(self.checkpoints)
        await self.runner.adopt(report)
        for task_id in report.interrupted:
            await self.memory.update_task(task_id, status="failed", termination_reason="进程中断")
        for task_id in report.waiting_for_approval:
            await self.memory.update_task(task_id, status="waiting_for_approval")
        if report.interrupted or report.waiting_for_approval:
            logger.warning(
                "上次进程留下 {} 个中断任务、{} 个等待审批的任务",
                len(report.interrupted),
                len(report.waiting_for_approval),
            )

    async def aclose(self) -> None:
        await self.runner.shutdown()
        if self.mcp is not None:
            await self.mcp.aclose()  # 先于存储关闭：回收全部 MCP 子进程
        await self.shell.aclose()
        await self.web.aclose()
        for store in (self.events, self.checkpoints, self.memory):
            if store is not None:
                await store.close()

    # ------------------------------------------------------------------ 对 API 的依赖

    # ------------------------------------------------------------------ 界面覆盖层

    def _skill_records(
        self, workspace_root: Path, sources: Mapping[str, bool]
    ) -> list[dict[str, object]]:
        registry = SkillRegistry.scan(
            skill_roots_from_settings(
                self.loaded.settings,
                workspace_root,
                global_dir=self.loaded.home / "skills",
                sources=sources,
            ),
            skill_limits_from_settings(self.loaded.settings),
        )
        return [
            {
                "name": record.name,
                "source": record.source,
                "description": record.description,
                "triggers": list(record.triggers),
                "required_tools": list(record.required_tools),
                "high_privilege": list(record.high_privilege),
                "warnings": list(record.warnings),
                "body": record.body,
                "helper_files": {k: list(v) for k, v in record.helper_files.items()},
            }
            for record in registry.records()
        ]

    async def skill_catalog(self) -> list[dict[str, Any]]:
        policy = await load_skill_policy(self.memory)
        cfg = self.loaded.settings.skills
        effective = {
            "builtin": policy.source_enabled("builtin", cfg.allow_builtin),
            "global": policy.source_enabled("global", cfg.allow_global),
            "workspace": policy.source_enabled("workspace", cfg.allow_workspace),
        }
        out: list[dict[str, Any]] = []
        base = self._skill_records(
            self.loaded.home,
            {"builtin": effective["builtin"], "global": effective["global"], "workspace": False},
        )
        for record in base:
            record["enabled"] = (
                effective[str(record["source"])] and record["name"] not in policy.disabled
            )
            record["trusted"] = record["source"] != "workspace"
            record.pop("body", None)
            record.pop("helper_files", None)
            out.append(record)
        for workspace in await self.memory.list_workspaces():
            records = self._skill_records(
                Path(workspace.root_path),
                {"builtin": False, "global": False, "workspace": True},
            )
            for record in records:
                record["enabled"] = effective["workspace"] and record["name"] not in policy.disabled
                record["trusted"] = False
                record["workspace_id"] = workspace.workspace_id
                record["workspace_name"] = workspace.name
                record.pop("body", None)
                record.pop("helper_files", None)
                out.append(record)
        return out

    async def skill_body(self, name: str, workspace_id: str | None = None) -> dict[str, Any] | None:
        if workspace_id is None:
            records = self._skill_records(
                self.loaded.home,
                {"builtin": True, "global": True, "workspace": False},
            )
            return next((r for r in records if r["name"] == name), None)
        workspace = await self.memory.get_workspace(workspace_id)
        records = self._skill_records(
            Path(workspace.root_path),
            {"builtin": False, "global": False, "workspace": True},
        )
        return next((r for r in records if r["name"] == name), None)

    async def generate_title(self, session_id: str) -> str | None:
        """首轮后生成标题：优先用会话选择的端点，失败时退回首条消息。"""
        turn = await self.memory.first_user_turn(session_id)
        if turn is None:
            return None
        compact = " ".join(turn.content.split())
        for stop in ("。", "！", "？", ".", "!", "?"):
            compact = compact.split(stop, 1)[0]
        fallback = compact[:24].rstrip("，,；;：: ") or None
        prefs = SessionPreferences.from_mapping(await self.memory.get_session_settings(session_id))
        preferred = prefs.model
        tier = "fast"
        if preferred:
            for name, endpoints in self.loaded.endpoints.items():
                if any(endpoint.name == preferred for endpoint in endpoints):
                    tier = name
                    break
        request = LLMRequest(
            tier=tier,  # type: ignore[arg-type]
            messages=(
                Message(
                    role="system",
                    content=(
                        "你为一段对话生成标题。只输出标题本身：不超过 16 个汉字，"
                        "不要引号、不要句号、不要解释。"
                    ),
                ),
                Message(role="user", content=turn.content[:2000]),
            ),
            purpose="title",
            max_output_tokens=512,
            reasoning_effort="low",
            prefer_endpoint=preferred,
        )
        try:
            response = await self.factory.gateway.invoke(request)
        except Exception as exc:  # 标题是增强项，失败不阻断任何流程
            logger.warning("生成会话标题失败，使用首条消息兜底：{!r}", exc)
            return fallback
        title = " ".join(response.message.content.split())
        title = title.strip("`'\"“”‘’#*- ").strip()
        return (title[:60] or fallback) if title else fallback

    def api_services(self) -> ApiServices:
        return ApiServices(
            settings=self.loaded.settings,
            memory=self.memory,
            tasks=self.tasks,
            hub=self.hub,
            redactor=self.redactor,
            home=Path(os.path.expanduser("~")),
            introspection=Introspection(
                skills=self.factory.skill_listing,
                tools=self.factory.tool_listing,
                mcp_servers=self.mcp.status if self.mcp is not None else list,
                models=self._model_listing,
            ),
            dependency_checks=self._dependency_checks,
            prompts=self.factory.library,
            compact_session=self.factory.flow.compact,
            context_inspector=self.factory.inspect_context,
            context_segment=self.factory.inspect_segment,
            title_generator=self.generate_title,
            skill_catalog=self.skill_catalog,
            skill_body=self.skill_body,
            mcp_control=self.mcp,
        )

    def _model_listing(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for tier, endpoints in self.loaded.endpoints.items():
            for endpoint in endpoints:
                out.append(
                    {
                        "tier": tier,
                        "name": endpoint.name,
                        "model": endpoint.model,
                        "base_url": endpoint.base_url,
                        "context_window": endpoint.context_window,
                        "capabilities": endpoint.capabilities.model_dump(),
                    }
                )  # 不含密钥，也不含密钥所在的环境变量值
        return out

    async def _dependency_checks(self) -> dict[str, str]:
        services = self.loaded.settings.services
        urls = {"bash_shell": services.shell_url, "web_search": services.web_url}

        async def probe(client: httpx.AsyncClient, url: str) -> str:
            try:
                response = await client.get(url.rstrip("/") + "/health")
            except httpx.HTTPError:
                return "down"
            return "ok" if response.status_code == 200 else "degraded"

        async with httpx.AsyncClient(timeout=2.0) as client:
            results = await asyncio.gather(*(probe(client, u) for u in urls.values()))
        return dict(zip(urls, results, strict=True))


def configure_logging(loaded: LoadedSettings) -> None:
    """日志：控制台与 JSONL 文件，带任务上下文与统一脱敏。幂等。"""
    log = loaded.settings.logging
    setup_logging(
        level=log.level,
        console=log.console,
        file=loaded.paths.logging_file,
        rotation_mb=log.rotation_mb,
        retention_files=log.retention_files,
        redactor=redactor_from_settings(loaded),
    )

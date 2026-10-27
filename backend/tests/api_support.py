"""任务服务与 API 测试共用的假阶段与装配。"""

from __future__ import annotations

import tomllib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from agent_runtime.api import ApiServices, create_app
from agent_runtime.checkpoint import CheckpointStore
from agent_runtime.core import AgentState, Message, ToolCall
from agent_runtime.loop import TaskRuntime
from agent_runtime.memory import MemoryStore
from agent_runtime.observability import EventHub, Redactor
from agent_runtime.runtime import (
    FullPermissionsNotConfirmedError,
    RunnerDeps,
    TaskEvents,
    TaskRunner,
)
from agent_runtime.runtime.service import TaskService
from baseagent_config import Settings
from loop_support import ScriptedWorld

CALL = ToolCall(id="c1", name="bash", args={"command": "curl x", "api_key": "hunter2"})
ASSISTANT = Message(role="assistant", content="", tool_calls=(CALL,))
TOOL = Message(role="tool", content="ok canary-s1-task-1", tool_call_id="c1", name="bash")
REDACTOR = Redactor(["key", "token", "secret", "password"], [])


class World(ScriptedWorld):
    """API 测试用：事件走真实的 ``TaskEvents``，工具结果里带金丝雀以验证脱敏。"""

    def __init__(self, store: CheckpointStore, hub: EventHub) -> None:
        super().__init__(store)
        self.hub = hub
        self.tool_content = "ok {canary}"

    async def build(self, state: AgentState) -> TaskRuntime:
        self.events = TaskEvents(self.hub, state.task_id, REDACTOR, canary=state.canary_token)
        return await super().build(state)


def resolve(level: str | None, confirmed: bool) -> str:
    chosen = level or "workspace_write"
    if chosen == "full_permissions" and not confirmed:
        raise FullPermissionsNotConfirmedError("需要确认")
    return chosen


# ==============================================================================
# API 装配
# ==============================================================================

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "t0ken-" + "a" * 40
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class ApiRig:
    """一整套真实的存储 + 假阶段 + 真实的 FastAPI 应用。"""

    def __init__(self) -> None:
        self.app: FastAPI
        self.client: httpx.AsyncClient
        self.world: World
        self.services: ApiServices
        self.root: Path
        self.tmp: Path

    async def create_workspace(self, name: str = "proj") -> str:
        response = await self.client.post(
            "/api/v1/workspaces", json={"name": name, "root_path": str(self.root)}, headers=HEADERS
        )
        assert response.status_code in (200, 201), response.text
        return str(response.json()["workspace_id"])

    async def create_session(self) -> str:
        wid = await self.create_workspace()
        response = await self.client.post(f"/api/v1/workspaces/{wid}/sessions", headers=HEADERS)
        assert response.status_code == 201, response.text
        return str(response.json()["session_id"])

    async def submit(self, goal: str = "fix it") -> dict[str, Any]:
        sid = await self.create_session()
        response = await self.client.post(
            f"/api/v1/sessions/{sid}/tasks", json={"task_goal": goal}, headers=HEADERS
        )
        assert response.status_code == 202, response.text
        return dict(response.json())


def make_settings(tmp_path: Path, **server: Any) -> Settings:
    raw = tomllib.loads((REPO_ROOT / "config" / "config.toml").read_text(encoding="utf-8"))
    raw.setdefault("server", {}).update(server)
    return Settings.model_validate(raw)


async def build_rig(
    tmp_path: Path, *, token: str | None = TOKEN, **server: Any
) -> AsyncIterator[ApiRig]:
    rig = ApiRig()
    rig.tmp = tmp_path
    rig.root = tmp_path / "work"
    rig.root.mkdir(parents=True)
    settings = make_settings(tmp_path, **server)
    async with (
        MemoryStore(tmp_path / "meta.db") as memory,
        CheckpointStore(tmp_path / "state.db") as checkpoints,
    ):
        hub = EventHub(clip_chars=300)
        world = World(checkpoints, hub)
        deps = RunnerDeps(
            checkpoints=checkpoints,
            build_runtime=world.build,
            is_idempotent=lambda name: False,
            make_canary=lambda session, task: f"canary-{session}-{task}",
        )
        runner = TaskRunner(deps, max_concurrent=1, new_task_id=lambda: next(world.ids))
        tasks = TaskService(
            runner=runner,
            memory=memory,
            checkpoints=checkpoints,
            hub=hub,
            redactor=REDACTOR,
            resolve_level=resolve,
            goal_max_chars=settings.server.task_goal_max_chars,
            artifacts_root=tmp_path / "artifacts",
            traces_root=tmp_path / "traces",
        )
        runner.attach_observer(tasks.observer())
        services = ApiServices(
            settings=settings,
            memory=memory,
            tasks=tasks,
            hub=hub,
            redactor=REDACTOR,
            home=tmp_path / "home",
        )
        rig.world, rig.services = world, services
        rig.app = create_app(services, token=token)
        transport = httpx.ASGITransport(app=rig.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8000"
        ) as client:
            rig.client = client
            yield rig
        await runner.shutdown()

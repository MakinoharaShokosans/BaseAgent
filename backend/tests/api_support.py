"""任务服务与 API 测试共用的假阶段与装配。"""

from __future__ import annotations

import asyncio
import tomllib
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from aegis_config import Settings
from agent_runtime.api import ApiServices, create_app
from agent_runtime.approval import compute_approval_id
from agent_runtime.checkpoint import CheckpointStore
from agent_runtime.core import (
    AgentState,
    ApprovalRequired,
    Message,
    Milestone,
    PendingApproval,
    ToolCall,
)
from agent_runtime.loop import Stage, TaskRuntime
from agent_runtime.memory import MemoryStore
from agent_runtime.observability import EventHub, Redactor
from agent_runtime.runtime import (
    FullPermissionsNotConfirmedError,
    RunnerDeps,
    TaskEvents,
    TaskRunner,
)
from agent_runtime.runtime.service import TaskService

DONE_MS = Milestone(id=0, title="m", status="completed")
OPEN_MS = Milestone(id=0, title="m")
CALL = ToolCall(id="c1", name="bash", args={"command": "curl x", "api_key": "hunter2"})
ASSISTANT = Message(role="assistant", content="", tool_calls=(CALL,))
TOOL = Message(role="tool", content="ok canary-s1-task-1", tool_call_id="c1", name="bash")
REDACTOR = Redactor(["key", "token", "secret", "password"], [])


class World:
    def __init__(self, store: CheckpointStore, hub: EventHub) -> None:
        self.store = store
        self.hub = hub
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.fail: BaseException | None = None
        self.approval = False
        self.terminate = False
        self.ids = iter(f"task-{i}" for i in range(1, 100))
        self.artifact: Path | None = None

    def stage(self, name: str, events: TaskEvents) -> Stage:
        async def planner(state: AgentState) -> Mapping[str, object]:
            done = any(m.role == "tool" for m in state.messages)
            return {"milestones": [DONE_MS if done else OPEN_MS]}

        async def executor(state: AgentState) -> Mapping[str, object]:
            self.entered.set()
            if self.gate is not None:
                await self.gate.wait()
            if self.fail is not None:
                raise self.fail
            if self.terminate:
                return {"should_terminate": True, "termination_reason": "步数达到上限（3/3）"}
            return {"messages": [ASSISTANT], "step_count": state.step_count + 1}

        async def tool_runner(state: AgentState) -> Mapping[str, object]:
            if self.approval and state.approval_decision is None:
                raise ApprovalRequired(
                    PendingApproval(
                        approval_id=compute_approval_id(state.task_id, state.step_count, ("c1",)),
                        call_ids=("c1",),
                        required_level="full_permissions",
                        current_level="workspace_write",
                        action_type="network_egress",
                        command="curl x",
                        reason="r",
                        escalation_count=1,
                    )
                )
            leaky = Message(
                role="tool", content=f"ok {state.canary_token}", tool_call_id="c1", name="bash"
            )
            update: dict[str, object] = {
                "messages": [leaky],
                "pending_approval": None,
                "approval_decision": None,
                "replan_needed": True,
            }
            if self.artifact is not None:
                update["artifacts"] = {"obs_1": str(self.artifact)}
            return update

        async def noop(state: AgentState) -> Mapping[str, object]:
            return {}

        return {"planner": planner, "executor": executor, "tool_runner": tool_runner}.get(
            name, noop
        )

    async def build(self, state: AgentState) -> TaskRuntime:
        events = TaskEvents(self.hub, state.task_id, REDACTOR, canary=state.canary_token)
        names = ("planner", "budget_guard", "executor", "tool_runner", "evaluator")
        return TaskRuntime(
            stages={n: self.stage(n, events) for n in names},  # type: ignore[misc]
            checkpoints=self.store,
            events=events,
        )


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

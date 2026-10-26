"""run_task：新任务入口。设计文档：docs/01_architecture/02_task_lifecycle.md §2.1-2.2。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from agent_runtime.checkpoint import CheckpointStore
from agent_runtime.core import AgentState, FailedAttempt, Message, new_state
from agent_runtime.loop import TaskOutcome, TaskRuntime, drive
from agent_runtime.runtime.errors import TaskAlreadyExistsError


@dataclass(frozen=True, slots=True)
class RunnerDeps:
    checkpoints: CheckpointStore
    #: 装配任务级对象（预算守卫、账本、裁剪器、工具注册表、事件总线…），每次运行/续跑重建
    build_runtime: Callable[[AgentState], Awaitable[TaskRuntime]]
    #: 工具名 -> 重复执行是否安全；未知工具应返回 False
    is_idempotent: Callable[[str], bool]
    #: 金丝雀由会话与任务派生
    make_canary: Callable[[str, str], str]


@dataclass(frozen=True, slots=True)
class TaskRequest:
    workspace_id: str
    workspace_path: str
    session_id: str
    goal: str
    #: 已解析的权限基线（见 ``resolve_permission_level``）
    permission_level: str
    #: 会话里此前的活跃对话轮次；目标作为最新的用户消息接在其后
    history: Sequence[Message] = ()
    #: 会话记忆：继承来的摘要、事实、失败尝试
    rolling_summary: str = ""
    confirmed_facts: Sequence[str] = ()
    failed_attempts: Sequence[FailedAttempt] = ()


async def create_task(deps: RunnerDeps, request: TaskRequest, task_id: str) -> AgentState:
    """校验请求、构造初始状态并提交初始检查点。

    先于第一个阶段落盘：此后任务即使崩在第一个阶段里，也能被找回并由启动扫描发现。
    任何校验失败都不会写库；已有同名任务的检查点不会被覆盖。
    """
    if not request.goal.strip():
        raise ValueError("goal 不能为空")
    state = new_state(
        workspace_id=request.workspace_id,
        workspace_path=request.workspace_path,
        session_id=request.session_id,
        task_id=task_id,
        task_goal=request.goal,
        permission_level=request.permission_level,
        canary_token=deps.make_canary(request.session_id, task_id),
        messages=[*request.history, Message(role="user", content=request.goal)],
        rolling_summary=request.rolling_summary,
        confirmed_facts=request.confirmed_facts,
        failed_attempts=request.failed_attempts,
    )
    if await deps.checkpoints.load(task_id) is not None:
        raise TaskAlreadyExistsError(f"任务 {task_id} 已存在，不会覆盖")
    await deps.checkpoints.commit(state)
    return state


async def run_task(deps: RunnerDeps, request: TaskRequest, task_id: str) -> TaskOutcome:
    state = await create_task(deps, request, task_id)
    return await drive(await deps.build_runtime(state), state)

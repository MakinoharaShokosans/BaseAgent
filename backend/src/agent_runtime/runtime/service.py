"""任务服务：API 之下的运行时门面。设计文档：docs/09_api/01_endpoints.md §3、§4。

把任务运行器、记忆、检查点、事件中心组合成 API 需要的动作：提交、快照、审批、续跑、取消、
删除、时间线、产物。API 层只调用这里，不碰运行时内部。领域错误不带 HTTP 概念。
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from loguru import logger

from agent_runtime.approval import bind_decision
from agent_runtime.checkpoint import CheckpointStore
from agent_runtime.core import AgentState, ApprovalDecision, CoreError
from agent_runtime.memory import MemoryStore, TaskRecord
from agent_runtime.observability import EventHub, Redactor, TrajectoryRecorder
from agent_runtime.runtime.errors import (
    TaskAlreadyRunningError,
    TaskNotFoundError,
    TaskNotResumableError,
)
from agent_runtime.runtime.events import (
    LifecycleEvents,
    approval_payload,
    delivery_of,
    turns_of,
)
from agent_runtime.runtime.memory_flow import MemoryFlow
from agent_runtime.runtime.run import TaskRequest
from agent_runtime.runtime.runner import TaskRunner, TaskSnapshot

_TERMINAL: Final = frozenset({"succeeded", "failed", "cancelled", "terminated"})
_RESUMABLE: Final = frozenset({"failed"})


class TaskGoalError(CoreError, ValueError):
    """任务目标为空或过长。"""


class TaskStateConflictError(CoreError):
    """任务当前状态不允许该操作（对外映射为 409）。"""


class ArtifactNotFoundError(CoreError):
    pass


@dataclass(frozen=True, slots=True)
class TaskView:
    """对外的任务快照：``AgentState`` 的投影，不是它本身。"""

    task_id: str
    session_id: str
    workspace_id: str
    task_goal: str
    status: str
    permission_level: str
    approval_request: dict[str, object] | None
    plan: list[dict[str, object]]
    step_count: int
    total_tokens: int
    consecutive_errors: int
    termination_reason: str
    created_at: float
    started_at: float | None
    finished_at: float | None
    artifact_count: int


@dataclass(frozen=True, slots=True)
class ArtifactInfo:
    artifact_id: str
    size: int


class TaskService:
    def __init__(
        self,
        *,
        runner: TaskRunner,
        memory: MemoryStore,
        checkpoints: CheckpointStore,
        hub: EventHub,
        redactor: Redactor,
        resolve_level: Callable[[str | None, bool], str],
        goal_max_chars: int = 20000,
        flow: MemoryFlow | None = None,
        artifacts_root: Path | None = None,
        traces_root: Path | None = None,
        trajectory: TrajectoryRecorder | None = None,
        clock: Callable[[], float] = time.time,
        usage: Callable[[str], int] = lambda task_id: 0,
    ) -> None:
        self.runner = runner
        self._memory = memory
        self._checkpoints = checkpoints
        self._hub = hub
        self._redactor = redactor
        self._resolve_level = resolve_level
        self._goal_max = goal_max_chars
        self._flow = flow
        self._artifacts_root = artifacts_root
        self._traces_root = traces_root
        self._trajectory = trajectory
        self._clock = clock
        self._usage = usage
        self._decided: dict[str, tuple[str, bool]] = {}

    def observer(self, drain: Callable[[], Awaitable[None]] | None = None) -> TaskObserverImpl:
        """组合“事件 + 元数据 + 记忆收尾”的任务生命周期观察者，交给 ``TaskRunner``。

        ``drain``：收尾发 ``task.finished`` 之前先等待的旁路（如还在路上的 ``llm.call``）。"""
        return TaskObserverImpl(self, drain)

    # ------------------------------------------------------------------ 提交

    async def submit(
        self,
        session_id: str,
        goal: str,
        *,
        parent_task_id: str | None = None,
        permission_level: str | None = None,
        confirm_full_permissions: bool = False,
    ) -> TaskView:
        text = goal.strip()
        if not text:
            raise TaskGoalError("task_goal 不能为空")
        if len(text) > self._goal_max:
            raise TaskGoalError(f"task_goal 超过上限 {self._goal_max} 字符")
        session = await self._memory.get_session(session_id)
        workspace = await self._memory.get_workspace(session.workspace_id)
        level = self._resolve_level(permission_level, confirm_full_permissions)
        context = await self._flow.prepare(session_id) if self._flow is not None else None
        request = TaskRequest(
            workspace_id=workspace.workspace_id,
            workspace_path=workspace.root_path,
            session_id=session_id,
            goal=text,
            permission_level=level,
            history=context.history if context else (),
        )
        task_id = self.runner.new_task_id()
        # 元数据先于运行写入：任务一开始跑，观察者就要更新这一行
        await self._memory.record_task(
            TaskRecord(
                task_id,
                session_id,
                workspace.workspace_id,
                text,
                "running",
                level,
                self._clock(),
                parent_task_id=parent_task_id,
            )
        )
        try:
            await self.runner.submit(request, task_id)
        except BaseException:
            await self._memory.delete_task(task_id)
            raise
        return await self.get(task_id)

    # ------------------------------------------------------------------ 查询

    async def get(self, task_id: str) -> TaskView:
        record = await self._memory.get_task(task_id)
        if record is None:
            raise TaskNotFoundError(f"没有任务 {task_id}")
        return await self._view(record)

    async def list(
        self, session_id: str, limit: int = 50, before: float | None = None
    ) -> list[TaskView]:
        records = await self._memory.list_tasks(session_id, limit, before)
        return [await self._view(r) for r in records]

    async def _view(self, record: TaskRecord) -> TaskView:
        status = record.status
        snapshot = self._snapshot_of(record.task_id)
        if snapshot is not None:
            status = snapshot.status
        state = await self._checkpoints.load(record.task_id)
        pending = state.pending_approval if state and status == "waiting_for_approval" else None
        return TaskView(
            task_id=record.task_id,
            session_id=record.session_id,
            workspace_id=record.workspace_id,
            task_goal=self._redactor.text(record.task_goal),
            status=status,
            permission_level=record.permission_level,
            approval_request=approval_payload(pending, self._redactor) if pending else None,
            plan=_plan(state),
            step_count=turns_of(state) if state else record.step_count,
            total_tokens=self._usage(record.task_id) or record.total_tokens,
            consecutive_errors=state.consecutive_errors if state else 0,
            termination_reason=self._redactor.text(
                snapshot.reason if snapshot and snapshot.reason else record.termination_reason
            ),
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            artifact_count=len(state.artifacts) if state else record.artifact_count,
        )

    def _snapshot_of(self, task_id: str) -> TaskSnapshot | None:
        try:
            return self.runner.snapshot(task_id)
        except TaskNotFoundError:
            return None

    # ------------------------------------------------------------------ 审批

    async def approve(
        self, task_id: str, approval_id: str, scope: Literal["once", "always"]
    ) -> TaskView:
        return await self._decide(
            task_id, ApprovalDecision(approval_id=approval_id, approved=True, scope=scope)
        )

    async def reject(self, task_id: str, approval_id: str, reason: str = "") -> TaskView:
        return await self._decide(
            task_id, ApprovalDecision(approval_id=approval_id, approved=False, reason=reason)
        )

    async def _decide(self, task_id: str, decision: ApprovalDecision) -> TaskView:
        record = await self._need(task_id)
        snapshot = self._snapshot_of(task_id)
        status = snapshot.status if snapshot else record.status
        if status != "waiting_for_approval":
            if self._decided.get(task_id) == (decision.approval_id, decision.approved):
                return await self.get(task_id)  # 重复提交同一决定：幂等，返回当前状态
            raise TaskStateConflictError(f"任务当前是 {status}，不在等待审批")
        state = await self._checkpoints.load(task_id)
        if state is None:
            raise TaskNotFoundError(f"没有任务 {task_id}")
        bind_decision(state, decision)  # 预检：错配时在发事件之前就报错
        name = "task.approved" if decision.approved else "task.rejected"
        await self._hub.emit(
            task_id,
            name,
            {"approval_id": decision.approval_id, "scope": decision.scope}
            if decision.approved
            else {
                "approval_id": decision.approval_id,
                "reason": self._redactor.text(decision.reason),
            },
        )
        await self.runner.resume(task_id, decision)
        self._decided[task_id] = (decision.approval_id, decision.approved)
        await self._memory.update_task(task_id, status="running")
        return await self.get(task_id)

    # ------------------------------------------------------------------ 续跑与取消

    async def resume(self, task_id: str) -> TaskView:
        record = await self._need(task_id)
        snapshot = self._snapshot_of(task_id)
        status = snapshot.status if snapshot else record.status
        if status == "waiting_for_approval":
            raise TaskStateConflictError("任务正在等待审批，请使用 approve 或 reject")
        if status == "running":
            raise TaskAlreadyRunningError(f"任务 {task_id} 正在运行")
        if status not in _RESUMABLE:
            raise TaskNotResumableError(f"任务状态为 {status}，不可续跑")
        await self.runner.resume(task_id)
        await self._memory.update_task(task_id, status="running", finished_at=None)
        return await self.get(task_id)

    async def cancel(self, task_id: str) -> TaskView:
        record = await self._need(task_id)
        snapshot = self._snapshot_of(task_id)
        status = snapshot.status if snapshot else record.status
        if status not in _TERMINAL:
            if snapshot is None:  # 上个进程留下、本进程还没接管的任务
                await self._checkpoints.load(task_id)
            await self.runner.cancel(task_id)
        return await self.get(task_id)

    # ------------------------------------------------------------------ 删除

    async def delete(self, task_id: str) -> None:
        record = await self._need(task_id)
        snapshot = self._snapshot_of(task_id)
        status = snapshot.status if snapshot else record.status
        if status == "running" or not self.runner.discard(task_id):
            raise TaskStateConflictError("任务正在运行，先取消再删除")
        await self._checkpoints.delete(task_id)
        await self._hub.forget(task_id)
        self._drop_trajectory(task_id)
        await self._memory.delete_task(record.task_id)
        self._decided.pop(task_id, None)
        for root in (self._artifacts_root, self._traces_root):
            if root is not None:
                shutil.rmtree(root / _safe_name(task_id), ignore_errors=True)

    def _drop_trajectory(self, task_id: str) -> None:
        if self._trajectory is not None:
            self._trajectory.delete(task_id)

    def trajectory_file(self, task_id: str) -> Path | None:
        """轨迹文件路径（存在才返回）；任务 id 先过安全字符集校验。"""
        if self._trajectory is None or not self._trajectory.exists(task_id):
            return None
        return self._trajectory.path_of(task_id)

    async def forget_task(self, task_id: str) -> None:
        """会话/工作区被级联删除后，清掉它们名下任务的事件、检查点和产物（元数据已级联）。"""
        self.runner.discard(task_id)
        await self._checkpoints.delete(task_id)
        await self._hub.forget(task_id)
        self._drop_trajectory(task_id)
        self._decided.pop(task_id, None)
        for root in (self._artifacts_root, self._traces_root):
            if root is not None:
                shutil.rmtree(root / _safe_name(task_id), ignore_errors=True)

    # ------------------------------------------------------------------ 投影

    async def timeline(self, task_id: str, limit: int = 200) -> list[dict[str, object]]:
        """消息的投影：只给 UI 看的字段，已对金丝雀与密钥样内容脱敏。"""
        await self._need(task_id)
        state = await self._checkpoints.load(task_id)
        if state is None:
            return []
        redactor = self._redactor.with_secrets([state.canary_token])
        out: list[dict[str, object]] = []
        for index, message in enumerate(state.messages):
            item: dict[str, object] = {
                "index": index,
                "role": message.role,
                "content": redactor.text(message.content),
            }
            if message.tool_calls:
                item["tool_calls"] = [
                    {"id": c.id, "name": c.name, "args": redactor.args(c.args)[0]}
                    for c in message.tool_calls
                ]
            if message.role == "tool":
                item["tool_call_id"] = message.tool_call_id
                item["name"] = message.name
            if message.reasoning:
                item["reasoning"] = redactor.text(message.reasoning)[:2000]
            out.append(item)
        return out[-limit:]

    async def artifacts(self, task_id: str) -> list[ArtifactInfo]:
        await self._need(task_id)
        state = await self._checkpoints.load(task_id)
        if state is None:
            return []
        out: list[ArtifactInfo] = []
        for artifact_id, raw in sorted(state.artifacts.items()):
            path = self._artifact_path(task_id, raw)
            out.append(ArtifactInfo(artifact_id, path.stat().st_size if path else 0))
        return out

    async def artifact_file(self, task_id: str, artifact_id: str) -> Path:
        await self._need(task_id)
        state = await self._checkpoints.load(task_id)
        raw = state.artifacts.get(artifact_id) if state else None
        path = self._artifact_path(task_id, raw) if raw else None
        if path is None:
            raise ArtifactNotFoundError(f"任务 {task_id} 没有产物 {artifact_id}")
        return path

    def _artifact_path(self, task_id: str, raw: str) -> Path | None:
        """产物路径只来自检查点，仍要校验落在产物根目录内。"""
        if self._artifacts_root is None:
            return None
        root = (self._artifacts_root / _safe_name(task_id)).resolve()
        path = Path(raw).resolve()
        if not path.is_file() or not path.is_relative_to(root):
            return None
        return path

    async def _need(self, task_id: str) -> TaskRecord:
        record = await self._memory.get_task(task_id)
        if record is None:
            raise TaskNotFoundError(f"没有任务 {task_id}")
        return record

    # ------------------------------------------------------------------ 供观察者使用

    async def _on_started(self, task_id: str, state: AgentState) -> None:
        await self._memory.update_task(task_id, status="running", started_at=self._clock())

    async def _on_finished(self, snapshot: TaskSnapshot) -> None:
        state = snapshot.state or await self._checkpoints.load(snapshot.task_id)
        await self._memory.update_task(
            snapshot.task_id,
            status=snapshot.status,
            termination_reason=snapshot.reason,
            finished_at=self._clock(),
            step_count=turns_of(state),
            total_tokens=self._usage(snapshot.task_id),
            artifact_count=len(state.artifacts) if state else 0,
        )
        if (
            self._flow is not None
            and state is not None
            and snapshot.status
            in (
                "succeeded",
                "terminated",
            )
        ):
            report = await self._flow.finish(
                state.session_id,
                state,
                delivery=delivery_of(state, snapshot.status, snapshot.reason),
                status=snapshot.status,
                reason=snapshot.reason,
            )
            if report.error:
                logger.warning("任务 {} 的记忆收尾有问题：{}", snapshot.task_id, report.error)

    async def _on_waiting(self, task_id: str) -> None:
        await self._memory.update_task(task_id, status="waiting_for_approval")


class TaskObserverImpl:
    """``TaskObserver``：事件、元数据、记忆收尾，三者互不影响（运行器已吞掉它的异常）。"""

    def __init__(
        self, service: TaskService, drain: Callable[[], Awaitable[None]] | None = None
    ) -> None:
        self._service = service
        self._life = LifecycleEvents(
            service._hub,  # pyright: ignore[reportPrivateUsage]
            service._redactor,  # pyright: ignore[reportPrivateUsage]
            service._checkpoints.load,  # pyright: ignore[reportPrivateUsage]
            drain=drain,
            usage=service._usage,  # pyright: ignore[reportPrivateUsage]
        )

    async def task_started(self, task_id: str, state: AgentState) -> None:
        await _each(
            self._service._on_started(task_id, state),  # pyright: ignore[reportPrivateUsage]
            self._life.task_started(task_id, state),
        )

    async def task_waiting(self, task_id: str, state: AgentState) -> None:
        await _each(self._service._on_waiting(task_id))  # pyright: ignore[reportPrivateUsage]

    async def task_finished(self, snapshot: TaskSnapshot) -> None:
        await _each(
            self._service._on_finished(snapshot),  # pyright: ignore[reportPrivateUsage]
            self._life.task_finished(snapshot),
        )


async def _each(*steps: Awaitable[None]) -> None:
    for step in steps:
        try:
            await step
        except Exception:
            logger.exception("任务生命周期的一步失败；其余步骤继续")


def _plan(state: AgentState | None) -> list[dict[str, object]]:
    return [{"id": i.id, "title": i.title, "status": i.status} for i in state.plan] if state else []


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


__all__ = [
    "ArtifactInfo",
    "ArtifactNotFoundError",
    "TaskGoalError",
    "TaskObserverImpl",
    "TaskService",
    "TaskStateConflictError",
    "TaskView",
]

"""任务运行器：后台运行、状态映射、并发名额、取消与启动接管。

设计文档：docs/01_architecture/02_task_lifecycle.md、docs/02_runtime/02_main_loop.md §4。
状态目前只存在进程内存里；持久化的任务元数据表属于 API 层的工作（见路线图）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from loguru import logger

from agent_runtime.checkpoint import RecoveryReport, mark_cancelled
from agent_runtime.core import AgentState, ApprovalDecision, current_task_id
from agent_runtime.loop import TaskOutcome, drive
from agent_runtime.runtime.active import ActiveTasks
from agent_runtime.runtime.errors import (
    TaskAlreadyExistsError,
    TaskAlreadyRunningError,
    TaskNotFoundError,
    TaskQueueFullError,
)
from agent_runtime.runtime.resume import ResumeDeps, prepare_resume
from agent_runtime.runtime.run import RunnerDeps, TaskRequest, create_task

TaskStatus = Literal[
    "running", "waiting_for_approval", "succeeded", "terminated", "failed", "cancelled"
]
TERMINAL: frozenset[TaskStatus] = frozenset({"succeeded", "terminated", "failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    task_id: str
    status: TaskStatus
    #: 终止/失败/取消的原因；其余为空
    reason: str = ""
    #: 最近一次已知的状态（运行中为 None）
    state: AgentState | None = None


class TaskObserver(Protocol):
    """任务生命周期通知。``task_finished`` 即“收尾”：它出错不会改变任务结果。"""

    async def task_started(self, task_id: str, state: AgentState) -> None: ...

    async def task_finished(self, snapshot: TaskSnapshot) -> None: ...


class _Record:
    def __init__(self, status: TaskStatus, reason: str = "", state: AgentState | None = None):
        self.status: TaskStatus = status
        self.reason = reason
        self.state = state
        self.task: asyncio.Task[None] | None = None
        self.shutting_down = False

    def snapshot(self, task_id: str) -> TaskSnapshot:
        return TaskSnapshot(task_id, self.status, self.reason, self.state)


def _new_task_id() -> str:
    return "task_" + uuid.uuid4().hex[:12]


class TaskRunner:
    """同一事件循环线程内使用。``max_concurrent`` 只统计 ``running`` 的任务：
    挂起等待审批的任务释放执行资源，不占名额。"""

    def __init__(
        self,
        deps: RunnerDeps,
        *,
        max_concurrent: int = 1,
        observer: TaskObserver | None = None,
        new_task_id: Callable[[], str] = _new_task_id,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent 必须至少为 1")
        self._deps = deps
        self._max = max_concurrent
        self._observer = observer
        self._new_id = new_task_id
        self._active = ActiveTasks()
        self._resume_deps = ResumeDeps(
            checkpoints=deps.checkpoints,
            build_runtime=deps.build_runtime,
            is_idempotent=deps.is_idempotent,
            active=self._active,
        )
        self._records: dict[str, _Record] = {}

    def attach_observer(self, observer: TaskObserver) -> None:
        """生命周期观察者依赖运行器本身（任务服务），所以允许装配后再接上，且只能接一次。"""
        if self._observer is not None:
            raise RuntimeError("观察者已经接上")
        self._observer = observer

    # ------------------------------------------------------------------ 查询

    def snapshot(self, task_id: str) -> TaskSnapshot:
        return self._record(task_id).snapshot(task_id)

    def list_tasks(self) -> list[TaskSnapshot]:
        return [r.snapshot(tid) for tid, r in self._records.items()]

    def _record(self, task_id: str) -> _Record:
        record = self._records.get(task_id)
        if record is None:
            raise TaskNotFoundError(f"没有任务 {task_id}")
        return record

    def _running(self) -> int:
        return sum(1 for r in self._records.values() if r.status == "running")

    async def wait(self, task_id: str) -> TaskSnapshot:
        """等到该任务不再运行（终态或挂起）。"""
        task = self._record(task_id).task
        if task is not None:
            await asyncio.wait([task])
        return self.snapshot(task_id)

    async def wait_all(self) -> None:
        while pending := [r.task for r in self._records.values() if r.task and not r.task.done()]:
            await asyncio.wait(pending)

    # ------------------------------------------------------------------ 提交与续跑

    def new_task_id(self) -> str:
        return self._new_id()

    async def submit(self, request: TaskRequest, task_id: str | None = None) -> str:
        """登记并在后台运行，立即返回 task_id。名额满则 :class:`TaskQueueFullError`，不留痕迹。

        ``task_id`` 由调用方预先分配时（要先写元数据再运行），重复使用已登记的 id 会被拒绝。
        """
        if self._running() >= self._max:
            raise TaskQueueFullError(f"并发任务已满（上限 {self._max}）")
        task_id = task_id or self._new_id()
        if task_id in self._records:
            raise TaskAlreadyExistsError(f"任务 {task_id} 已存在，不会覆盖")
        record = _Record("running")
        self._records[task_id] = record  # 与上面的检查之间没有 await，名额不会被并发提交抢占
        try:
            state = await create_task(self._deps, request, task_id)
        except BaseException:
            del self._records[task_id]
            raise
        self._active.try_acquire(task_id)
        self._spawn(task_id, record, state)
        return task_id

    async def resume(
        self,
        task_id: str,
        decision: ApprovalDecision | Mapping[str, object] | None = None,
    ) -> None:
        """续跑。校验错误同步抛给调用方，任务保持原状；通过后转入后台运行。"""
        record = await self._record_for_resume(task_id)
        if not self._active.try_acquire(task_id):
            raise TaskAlreadyRunningError(f"任务 {task_id} 已在运行")
        try:
            if self._running() >= self._max:
                raise TaskQueueFullError(f"并发任务已满（上限 {self._max}）")
            state = await prepare_resume(self._resume_deps, task_id, decision)
        except BaseException:
            self._active.release(task_id)
            raise
        record.status, record.reason = "running", ""
        self._spawn(task_id, record, state)

    async def _record_for_resume(self, task_id: str) -> _Record:
        if task_id in self._records:
            return self._records[task_id]
        stored = await self._deps.checkpoints.load(task_id)
        if stored is None:
            raise TaskNotFoundError(f"没有任务 {task_id}")
        record = _Record(_status_of_stored(stored), state=stored)
        self._records[task_id] = record
        return record

    def _spawn(self, task_id: str, record: _Record, state: AgentState) -> None:
        record.status, record.state = "running", None
        record.task = asyncio.create_task(self._run(task_id, record, state), name=task_id)

    # ------------------------------------------------------------------ 后台运行

    async def _run(self, task_id: str, record: _Record, state: AgentState) -> None:
        current_task_id.set(task_id)  # 网关的调用记录等旁路据此知道属于哪个任务
        try:
            if self._observer is not None:
                await _swallow(self._observer.task_started(task_id, state))
            runtime = await self._deps.build_runtime(state)
            outcome = await drive(runtime, state)
        except asyncio.CancelledError:
            if record.shutting_down:
                record.status, record.reason = "failed", "服务关闭"
            raise  # 用户取消由 cancel() 收尾
        except Exception as exc:
            logger.exception("任务 {} 失败", task_id)
            await self._finish(task_id, record, "failed", f"{type(exc).__name__}: {exc}", None)
        else:
            await self._settle(task_id, record, outcome)
        finally:
            self._active.release(task_id)

    async def _settle(self, task_id: str, record: _Record, outcome: TaskOutcome) -> None:
        match outcome.status:
            case "waiting_for_approval":
                record.status, record.reason, record.state = (
                    "waiting_for_approval",
                    "",
                    outcome.state,
                )
                hook = getattr(self._observer, "task_waiting", None)
                if hook is not None:  # 可选通知：只有关心挂起的观察者实现它
                    await _swallow(hook(task_id, outcome.state))
            case "terminated":
                await self._finish(
                    task_id, record, "terminated", outcome.state.termination_reason, outcome.state
                )
            case "succeeded":
                await self._finish(task_id, record, "succeeded", "", outcome.state)

    async def _finish(
        self,
        task_id: str,
        record: _Record,
        status: TaskStatus,
        reason: str,
        state: AgentState | None,
    ) -> None:
        record.status, record.reason, record.state = status, reason, state
        if self._observer is not None:
            await _swallow(self._observer.task_finished(record.snapshot(task_id)))

    # ------------------------------------------------------------------ 取消、关停、接管

    async def cancel(self, task_id: str) -> bool:
        """用户取消。运行中的任务被中断，挂起的任务作废其请求；两者都把“用户取消”的终态
        落盘（补齐未配对的工具调用），重启后不会被当成中断或等待审批。已结束的任务返回 False。"""
        record = self._record(task_id)
        if record.status in TERMINAL:
            return False
        if record.task is not None and not record.task.done():
            record.task.cancel()
            await asyncio.wait([record.task])
        stored = await self._deps.checkpoints.load(task_id)
        final = stored
        if stored is not None:
            final = mark_cancelled(stored)
            if final != stored:
                await self._deps.checkpoints.commit(final)
        await self._finish(task_id, record, "cancelled", "用户取消", final)
        return True

    def discard(self, task_id: str) -> bool:
        """忘掉一个已结束或挂起的任务（删除任务时用）。运行中的任务不能忘，返回 False。"""
        record = self._records.get(task_id)
        if record is None:
            return True
        if record.status == "running":
            return False
        del self._records[task_id]
        return True

    async def shutdown(self) -> None:
        """停止所有运行中的任务但不写终态：状态停在上一次提交点，下次启动可续跑。"""
        tasks = []
        for record in self._records.values():
            if record.task is not None and not record.task.done():
                record.shutting_down = True
                record.task.cancel()
                tasks.append(record.task)
        if tasks:
            await asyncio.wait(tasks)

    async def adopt(self, report: RecoveryReport) -> None:
        """启动时接管上次进程留下的任务：中断的记为失败（等用户显式续跑，不自动续跑），
        挂起的恢复为等待审批。"""
        for task_id in report.interrupted:
            self._records.setdefault(task_id, _Record("failed", "进程中断"))
        for task_id in report.waiting_for_approval:
            self._records.setdefault(task_id, _Record("waiting_for_approval"))


def _status_of_stored(state: AgentState) -> TaskStatus:
    if state.pending_approval is not None:
        return "waiting_for_approval"
    if state.phase == "done":
        return "terminated" if state.should_terminate else "succeeded"
    return "failed"


async def _swallow(awaitable: Awaitable[None]) -> None:
    try:
        await awaitable
    except Exception:
        logger.exception("任务生命周期通知失败；不影响任务结果")

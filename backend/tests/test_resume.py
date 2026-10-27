"""resume_task：从检查点续跑。出处：docs/02_runtime/03_checkpoint_and_resume.md §4-7。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.approval import (
    ApprovalMismatchError,
    NotWaitingForApprovalError,
    compute_approval_id,
)
from agent_runtime.checkpoint import CheckpointStore
from agent_runtime.core import (
    DONE,
    AgentState,
    ApprovalDecision,
    Message,
    Milestone,
    PendingApproval,
    ToolCall,
    apply_update,
)
from agent_runtime.loop import Stage, TaskRuntime
from agent_runtime.runtime import (
    ActiveTasks,
    ApprovalRequiredFirstError,
    ResumeDeps,
    TaskAlreadyRunningError,
    TaskNotFoundError,
    TaskNotResumableError,
    resume_task,
)

MakeState = Callable[..., AgentState]

CALL = ToolCall(id="c1", name="bash", args={"command": "curl x"})
ASSISTANT = Message(role="assistant", content="", tool_calls=(CALL,))
TOOL = Message(role="tool", content="ok", tool_call_id="c1", name="bash")
DONE_MS = Milestone(id=0, title="m", status="completed")


def pending(task_id: str = "t1", step: int = 1) -> PendingApproval:
    return PendingApproval(
        approval_id=compute_approval_id(task_id, step, ("c1",)),
        call_ids=("c1",),
        required_level="full_permissions",
        current_level="workspace_write",
        action_type="network",
        command="curl x",
        reason="r",
        escalation_count=1,
    )


class NullSink:
    async def stage_committed(
        self, phase: str, update: Mapping[str, object], state: AgentState
    ) -> None:
        pass

    async def approval_requested(self, request: PendingApproval, state: AgentState) -> None:
        pass


class Harness:
    """记录阶段调用，并按阶段名给出行为。"""

    def __init__(self, store: CheckpointStore, **behaviours: Any) -> None:
        self.store = store
        self.behaviours = behaviours
        self.visited: list[tuple[str, AgentState]] = []
        self.built_with: list[AgentState] = []
        self.idempotent: set[str] = {"view_file"}
        self.active = ActiveTasks()

    def stage(self, name: str) -> Stage:
        async def run(state: AgentState) -> Mapping[str, object]:
            self.visited.append((name, state))
            behaviour = self.behaviours.get(name, {})
            if isinstance(behaviour, BaseException):
                raise behaviour
            if callable(behaviour):
                return await behaviour(state)  # type: ignore[no-any-return]
            return behaviour  # type: ignore[no-any-return]

        return run

    async def build_runtime(self, state: AgentState) -> TaskRuntime:
        self.built_with.append(state)
        names = ("planner", "budget_guard", "executor", "tool_runner", "evaluator")
        return TaskRuntime(
            stages={n: self.stage(n) for n in names},  # type: ignore[misc]
            checkpoints=self.store,
            events=NullSink(),  # type: ignore[arg-type]
        )

    @property
    def deps(self) -> ResumeDeps:
        return ResumeDeps(
            checkpoints=self.store,
            build_runtime=self.build_runtime,
            is_idempotent=lambda name: name in self.idempotent,
            active=self.active,
        )

    def names(self) -> list[str]:
        return [n for n, _ in self.visited]


@pytest.fixture
async def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    async with CheckpointStore(tmp_path / "state.db") as opened:
        yield opened


# ==============================================================================
# 能不能续跑
# ==============================================================================


@pytest.mark.unit
async def test_unknown_task(store: CheckpointStore) -> None:
    with pytest.raises(TaskNotFoundError, match="nope"):
        await resume_task(Harness(store).deps, "nope")


@pytest.mark.unit
async def test_a_succeeded_task_cannot_be_resumed(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await store.commit(apply_update(make_state(), {"phase": DONE, "milestones": [DONE_MS]}))
    harness = Harness(store)
    with pytest.raises(TaskNotResumableError, match="成功"):
        await resume_task(harness.deps, "t1")
    assert harness.visited == []


@pytest.mark.unit
async def test_a_terminated_task_restarts_at_the_planner_with_budget_counters_kept(
    store: CheckpointStore, make_state: MakeState
) -> None:
    fused = apply_update(
        make_state(),
        {
            "phase": DONE,
            "should_terminate": True,
            "termination_reason": "步数用尽",
            "step_count": 25,
            "total_tokens": 1234,
            "milestones": [Milestone(id=0, title="m")],
        },
    )
    await store.commit(fused)
    harness = Harness(store, planner={"milestones": [DONE_MS]})
    outcome = await resume_task(harness.deps, "t1")

    first_name, first_state = harness.visited[0]
    assert first_name == "planner"
    assert first_state.should_terminate is False
    assert first_state.termination_reason == ""
    assert (first_state.step_count, first_state.total_tokens) == (25, 1234)  # 沿用，不清零
    assert outcome.status == "succeeded"


@pytest.mark.unit
async def test_an_interrupted_task_continues_at_its_stored_phase(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await store.commit(
        apply_update(make_state(), {"phase": "evaluator", "milestones": [DONE_MS], "step_count": 2})
    )
    harness = Harness(store)
    outcome = await resume_task(harness.deps, "t1")
    assert harness.names() == ["evaluator"]
    assert outcome.status == "succeeded"
    assert await store.load("t1") == outcome.state


@pytest.mark.unit
async def test_the_runtime_is_built_from_the_state_that_will_run(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await store.commit(
        apply_update(
            make_state(), {"phase": "evaluator", "milestones": [DONE_MS], "total_tokens": 77}
        )
    )
    harness = Harness(store)
    await resume_task(harness.deps, "t1")
    assert harness.built_with[0].total_tokens == 77  # 账本据此与已用量对齐


# ==============================================================================
# 审批
# ==============================================================================


async def suspend(store: CheckpointStore, make_state: MakeState) -> AgentState:
    state = apply_update(
        make_state(),
        {
            "messages": [ASSISTANT],
            "phase": "tool_runner",
            "step_count": 1,
            "pending_approval": pending(),
            "milestones": [Milestone(id=0, title="m")],
        },
    )
    await store.commit(state)
    return state


def approve(approved: bool = True, **extra: Any) -> dict[str, Any]:
    return {"approval_id": pending().approval_id, "approved": approved, **extra}


@pytest.mark.unit
async def test_suspended_task_needs_a_decision(
    store: CheckpointStore, make_state: MakeState
) -> None:
    before = await suspend(store, make_state)
    harness = Harness(store)
    with pytest.raises(ApprovalRequiredFirstError):
        await resume_task(harness.deps, "t1")
    assert harness.visited == []
    assert await store.load("t1") == before


@pytest.mark.unit
async def test_a_valid_decision_is_consumed_by_the_re_entered_stage(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await suspend(store, make_state)

    async def consume(state: AgentState) -> Mapping[str, object]:
        assert state.approval_decision is not None
        assert state.approval_decision.approved is True
        return {
            "messages": [TOOL],
            "pending_approval": None,
            "approval_decision": None,
            "replan_needed": True,
        }

    harness = Harness(store, tool_runner=consume, planner={"milestones": [DONE_MS]})
    outcome = await resume_task(harness.deps, "t1", approve())

    assert harness.names()[0] == "tool_runner"  # 重新进入同一阶段
    assert outcome.status == "succeeded"
    assert outcome.state.pending_approval is None
    assert outcome.state.approval_decision is None


@pytest.mark.unit
async def test_a_rejection_is_delivered_the_same_way(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await suspend(store, make_state)
    seen: list[ApprovalDecision] = []

    async def consume(state: AgentState) -> Mapping[str, object]:
        assert state.approval_decision is not None
        seen.append(state.approval_decision)
        return {
            "messages": [TOOL],
            "pending_approval": None,
            "approval_decision": None,
            "replan_needed": True,
        }

    harness = Harness(store, tool_runner=consume, planner={"milestones": [DONE_MS]})
    await resume_task(harness.deps, "t1", approve(False, reason="不允许联网"))
    assert seen[0].approved is False
    assert seen[0].reason == "不允许联网"


@pytest.mark.unit
async def test_the_decision_is_persisted_before_the_stage_runs(
    store: CheckpointStore, make_state: MakeState
) -> None:
    """决策到达后立刻落盘：之后即使崩溃，用户的批准也不会丢。"""
    await suspend(store, make_state)
    harness = Harness(store, tool_runner=RuntimeError("process died"))
    with pytest.raises(RuntimeError, match="process died"):
        await resume_task(harness.deps, "t1", approve())

    stored = await store.load("t1")
    assert stored is not None
    assert stored.pending_approval is not None
    assert stored.approval_decision is not None

    # 再次续跑不必（也不能）重新给决策
    consumed = {
        "messages": [TOOL],
        "pending_approval": None,
        "approval_decision": None,
        "replan_needed": True,
    }
    again = Harness(store, tool_runner=consumed, planner={"milestones": [DONE_MS]})
    outcome = await resume_task(again.deps, "t1")
    assert outcome.status == "succeeded"


@pytest.mark.unit
async def test_a_decision_for_another_request_leaves_the_task_suspended(
    store: CheckpointStore, make_state: MakeState
) -> None:
    before = await suspend(store, make_state)
    harness = Harness(store)
    with pytest.raises(ApprovalMismatchError):
        await resume_task(harness.deps, "t1", {"approval_id": "appr_other", "approved": True})
    assert harness.visited == []
    assert await store.load("t1") == before


@pytest.mark.unit
async def test_a_decision_for_a_task_that_is_not_waiting_is_refused(
    store: CheckpointStore, make_state: MakeState
) -> None:
    """重复提交：第一次消费后 pending 已清空，第二次被拒绝，不会重复生效。"""
    await store.commit(apply_update(make_state(), {"phase": "evaluator", "milestones": [DONE_MS]}))
    with pytest.raises(NotWaitingForApprovalError):
        await resume_task(Harness(store).deps, "t1", approve())


@pytest.mark.unit
async def test_malformed_decision_is_refused_before_anything_runs(
    store: CheckpointStore, make_state: MakeState
) -> None:
    before = await suspend(store, make_state)
    harness = Harness(store)
    with pytest.raises(ValueError, match="approved"):
        await resume_task(
            harness.deps, "t1", {"approval_id": pending().approval_id, "approved": "yes"}
        )
    assert await store.load("t1") == before


# ==============================================================================
# 派发中断的恢复
# ==============================================================================


async def crash_during_dispatch(store: CheckpointStore, make_state: MakeState, tool: str) -> None:
    call = ToolCall(id="c1", name=tool, args={})
    await store.commit(
        apply_update(
            make_state(),
            {
                "messages": [Message(role="assistant", content="", tool_calls=(call,))],
                "phase": "tool_runner",
                "dispatch_started": ["c1"],
                "step_count": 1,
                "milestones": [Milestone(id=0, title="m")],
            },
        )
    )


@pytest.mark.unit
async def test_unknown_dispatch_result_goes_to_the_planner_without_re_running_the_tool(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await crash_during_dispatch(store, make_state, "bash")
    harness = Harness(store, planner={"milestones": [DONE_MS]})
    await resume_task(harness.deps, "t1")

    assert "tool_runner" not in harness.names()
    first_name, first_state = harness.visited[0]
    assert first_name == "planner"
    assert "执行结果未知" in first_state.messages[-1].content
    assert first_state.dispatch_started == ()


@pytest.mark.unit
async def test_idempotent_dispatch_is_simply_retried(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await crash_during_dispatch(store, make_state, "view_file")
    harness = Harness(
        store,
        tool_runner={
            "messages": [Message(role="tool", content="data", tool_call_id="c1", name="view_file")],
            "replan_needed": True,
        },
        planner={"milestones": [DONE_MS]},
    )
    await resume_task(harness.deps, "t1")
    name, state = harness.visited[0]
    assert name == "tool_runner"
    assert state.dispatch_started == ()


@pytest.mark.unit
async def test_recovery_is_committed_before_the_first_stage_runs(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await crash_during_dispatch(store, make_state, "bash")
    harness = Harness(store, planner=RuntimeError("crash again"))
    with pytest.raises(RuntimeError):
        await resume_task(harness.deps, "t1")
    stored = await store.load("t1")
    assert stored is not None
    assert "执行结果未知" in stored.messages[-1].content
    assert stored.dispatch_started == ()


# ==============================================================================
# 并发
# ==============================================================================


@pytest.mark.unit
async def test_a_second_resume_of_a_running_task_is_rejected(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await store.commit(apply_update(make_state(), {"phase": "evaluator", "milestones": [DONE_MS]}))
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(state: AgentState) -> Mapping[str, object]:
        entered.set()
        await release.wait()
        return {}

    harness = Harness(store, evaluator=slow)
    first = asyncio.create_task(resume_task(harness.deps, "t1"))
    await entered.wait()
    with pytest.raises(TaskAlreadyRunningError):
        await resume_task(harness.deps, "t1")
    release.set()
    await first


@pytest.mark.unit
async def test_the_slot_is_released_after_failure_and_after_cancellation(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await store.commit(apply_update(make_state(), {"phase": "evaluator", "milestones": [DONE_MS]}))
    failing = Harness(store, evaluator=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await resume_task(failing.deps, "t1")
    assert "t1" not in failing.active

    started = asyncio.Event()

    async def hang(state: AgentState) -> Mapping[str, object]:
        started.set()
        await asyncio.Event().wait()
        return {}

    hanging = Harness(store, evaluator=hang)
    task = asyncio.create_task(resume_task(hanging.deps, "t1"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "t1" not in hanging.active


@pytest.mark.unit
async def test_the_slot_is_released_when_resume_is_refused(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await suspend(store, make_state)
    harness = Harness(store)
    with pytest.raises(ApprovalRequiredFirstError):
        await resume_task(harness.deps, "t1")
    assert "t1" not in harness.active


@pytest.mark.unit
def test_active_tasks_registry() -> None:
    active = ActiveTasks()
    assert active.try_acquire("a") is True
    assert active.try_acquire("a") is False
    assert active.try_acquire("b") is True
    active.release("a")
    assert "a" not in active
    assert active.try_acquire("a") is True
    active.release("missing")  # 释放不存在的不报错

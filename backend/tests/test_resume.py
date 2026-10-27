"""resume_task：从检查点续跑。出处：docs/02_runtime/03_checkpoint_and_resume.md §4-7。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.approval import (
    ApprovalMismatchError,
    NotWaitingForApprovalError,
    compute_approval_id,
)
from agent_runtime.checkpoint import UNKNOWN_RESULT, CheckpointStore
from agent_runtime.core import (
    AgentState,
    ApprovalDecision,
    Message,
    PendingApproval,
    ToolCall,
    apply_update,
)
from agent_runtime.loop import TaskRuntime
from agent_runtime.runtime import (
    ActiveTasks,
    ApprovalRequiredFirstError,
    ResumeDeps,
    TaskAlreadyRunningError,
    TaskNotFoundError,
    TaskNotResumableError,
    resume_task,
)
from loop_support import ASSISTANT, FINAL, NullSink

MakeState = Callable[..., AgentState]

CALL = ToolCall(id="c1", name="bash", args={"command": "curl x"})
USER = Message(role="user", content="fix the bug")
TOOL = Message(role="tool", content="ok", tool_call_id="c1", name="bash")


def pending(task_id: str = "t1", position: int = 2) -> PendingApproval:
    return PendingApproval(
        approval_id=compute_approval_id(task_id, position, ("c1",)),
        call_ids=("c1",),
        required_level="full_permissions",
        current_level="workspace_write",
        action_type="network",
        command="curl x",
        reason="r",
        escalation_count=1,
    )


class Harness:
    """记录模型与工具调用时看到的状态。"""

    def __init__(self, store: CheckpointStore, **behaviours: Any) -> None:
        self.store = store
        self.behaviours = behaviours
        self.model_saw: list[AgentState] = []
        self.tools_saw: list[AgentState] = []
        self.built_with: list[AgentState] = []
        self.idempotent: set[str] = {"view_file"}
        self.active = ActiveTasks()

    async def model(self, state: AgentState) -> Message:
        self.model_saw.append(state)
        behaviour = self.behaviours.get("model")
        if isinstance(behaviour, BaseException):
            raise behaviour
        if callable(behaviour):
            return await behaviour(state)  # type: ignore[no-any-return]
        return FINAL

    async def tools(self, state: AgentState, calls: Sequence[ToolCall]) -> Mapping[str, object]:
        self.tools_saw.append(state)
        behaviour = self.behaviours.get("tools")
        if isinstance(behaviour, BaseException):
            raise behaviour
        if callable(behaviour):
            return await behaviour(state)  # type: ignore[no-any-return]
        return {
            "messages": [
                Message(role="tool", content="ok", tool_call_id=c.id, name=c.name) for c in calls
            ],
            "pending_approval": None,
            "approval_decision": None,
        }

    async def build_runtime(self, state: AgentState) -> TaskRuntime:
        self.built_with.append(state)
        return TaskRuntime(
            model=self.model, tools=self.tools, checkpoints=self.store, events=NullSink()
        )  # type: ignore[arg-type]

    @property
    def deps(self) -> ResumeDeps:
        return ResumeDeps(
            checkpoints=self.store,
            build_runtime=self.build_runtime,
            is_idempotent=lambda name: name in self.idempotent,
            active=self.active,
        )


@pytest.fixture
async def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    async with CheckpointStore(tmp_path / "state.db") as opened:
        yield opened


def base(make_state: MakeState, *messages: Message, **fields: Any) -> AgentState:
    return apply_update(make_state(), {"messages": [USER, *messages], **fields})


# ==============================================================================
# 能不能续跑
# ==============================================================================


@pytest.mark.unit
async def test_unknown_task(store: CheckpointStore) -> None:
    with pytest.raises(TaskNotFoundError, match="nope"):
        await resume_task(Harness(store).deps, "nope")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("outcome", "word"), [("succeeded", "成功"), ("cancelled", "取消"), ("terminated", "终止")]
)
async def test_finished_tasks_cannot_be_resumed(
    store: CheckpointStore, make_state: MakeState, outcome: str, word: str
) -> None:
    await store.commit(base(make_state, FINAL), outcome=outcome)  # type: ignore[arg-type]
    harness = Harness(store)
    with pytest.raises(TaskNotResumableError, match=word):
        await resume_task(harness.deps, "t1")
    assert harness.model_saw == []


@pytest.mark.unit
async def test_a_failed_task_just_asks_the_model_again(
    store: CheckpointStore, make_state: MakeState
) -> None:
    """余额不足等模型错误导致 failed：续跑没有额度要对齐，直接再调模型。"""
    await store.commit(base(make_state))
    harness = Harness(store)
    outcome = await resume_task(harness.deps, "t1")
    assert len(harness.model_saw) == 1 and outcome.status == "succeeded"
    assert await store.load("t1") == outcome.state
    meta = await store.load_status("t1")
    assert meta is not None and meta.status == "running"  # 终态由任务运行器写


@pytest.mark.unit
async def test_the_runtime_is_rebuilt_from_the_state_that_will_run(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await store.commit(base(make_state, facts=("f",)))
    harness = Harness(store)
    await resume_task(harness.deps, "t1")
    assert harness.built_with[0].facts == ("f",)


# ==============================================================================
# 审批
# ==============================================================================


async def suspend(store: CheckpointStore, make_state: MakeState) -> AgentState:
    state = base(make_state, ASSISTANT, pending_approval=pending())
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
    assert harness.model_saw == [] and harness.tools_saw == []
    assert await store.load("t1") == before


@pytest.mark.unit
async def test_a_valid_decision_is_consumed_by_the_re_entered_batch(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await suspend(store, make_state)
    harness = Harness(store)
    outcome = await resume_task(harness.deps, "t1", approve())

    assert harness.tools_saw[0].approval_decision is not None
    assert harness.tools_saw[0].approval_decision.approved is True
    assert harness.model_saw[0].messages[-1].role == "tool"  # 先执行本批，再调模型
    assert outcome.status == "succeeded"
    assert outcome.state.pending_approval is None and outcome.state.approval_decision is None


@pytest.mark.unit
async def test_a_rejection_is_delivered_the_same_way(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await suspend(store, make_state)
    harness = Harness(store)
    await resume_task(harness.deps, "t1", approve(False, reason="不允许联网"))
    decision: ApprovalDecision | None = harness.tools_saw[0].approval_decision
    assert decision is not None and decision.approved is False and decision.reason == "不允许联网"


@pytest.mark.unit
async def test_the_decision_is_persisted_before_the_batch_runs(
    store: CheckpointStore, make_state: MakeState
) -> None:
    """决策到达后立刻落盘：之后即使崩溃，用户的批准也不会丢。"""
    await suspend(store, make_state)
    harness = Harness(store, tools=RuntimeError("process died"))
    with pytest.raises(RuntimeError, match="process died"):
        await resume_task(harness.deps, "t1", approve())

    stored = await store.load("t1")
    assert stored is not None
    assert stored.pending_approval is not None and stored.approval_decision is not None

    again = Harness(store)  # 再次续跑不必（也不能）重新给决策
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
    assert harness.tools_saw == []
    assert await store.load("t1") == before


@pytest.mark.unit
async def test_a_decision_for_a_task_that_is_not_waiting_is_refused(
    store: CheckpointStore, make_state: MakeState
) -> None:
    """重复提交：第一次消费后 pending 已清空，第二次被拒绝，不会重复生效。"""
    await store.commit(base(make_state))
    with pytest.raises(NotWaitingForApprovalError):
        await resume_task(Harness(store).deps, "t1", approve())


@pytest.mark.unit
async def test_a_decision_for_a_finished_task_is_refused(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await store.commit(base(make_state, FINAL), outcome="succeeded")
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
# 回合内崩溃的恢复（repair_tail）
# ==============================================================================


async def crash_after_reply(store: CheckpointStore, make_state: MakeState, tool: str) -> None:
    call = ToolCall(id="c1", name=tool, args={})
    await store.commit(base(make_state, Message(role="assistant", content="", tool_calls=(call,))))


@pytest.mark.unit
async def test_unknown_result_goes_to_the_model_without_re_running_the_tool(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await crash_after_reply(store, make_state, "bash")
    harness = Harness(store)
    await resume_task(harness.deps, "t1")

    assert harness.tools_saw == []  # 非幂等：不盲目重放
    assert UNKNOWN_RESULT in harness.model_saw[0].messages[-1].content


@pytest.mark.unit
async def test_an_idempotent_batch_is_dropped_and_the_model_asked_again(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await crash_after_reply(store, make_state, "view_file")
    harness = Harness(store)
    await resume_task(harness.deps, "t1")
    assert harness.tools_saw == []
    assert [m.role for m in harness.model_saw[0].messages] == ["user"]


@pytest.mark.unit
async def test_the_repair_is_committed_before_the_loop_runs(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await crash_after_reply(store, make_state, "bash")
    harness = Harness(store, model=RuntimeError("crash again"))
    with pytest.raises(RuntimeError):
        await resume_task(harness.deps, "t1")
    stored = await store.load("t1")
    assert stored is not None and UNKNOWN_RESULT in stored.messages[-1].content


# ==============================================================================
# 并发
# ==============================================================================


@pytest.mark.unit
async def test_a_second_resume_of_a_running_task_is_rejected(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await store.commit(base(make_state))
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(state: AgentState) -> Message:
        entered.set()
        await release.wait()
        return FINAL

    harness = Harness(store, model=slow)
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
    await store.commit(base(make_state))
    failing = Harness(store, model=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await resume_task(failing.deps, "t1")
    assert "t1" not in failing.active

    started = asyncio.Event()

    async def hang(state: AgentState) -> Message:
        started.set()
        await asyncio.Event().wait()
        return FINAL

    hanging = Harness(store, model=hang)
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

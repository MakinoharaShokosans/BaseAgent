"""drive 循环：假阶段函数驱动。出处：docs/02_runtime/02_main_loop.md §3。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.checkpoint import CheckpointError, CheckpointStore
from agent_runtime.core import (
    DONE,
    AgentState,
    ApprovalDecision,
    ApprovalRequired,
    AtomicPairError,
    InvalidUpdateError,
    Message,
    Milestone,
    PendingApproval,
    Phase,
    ToolCall,
    apply_update,
)
from agent_runtime.loop import Stage, TaskRuntime, drive

MakeState = Callable[..., AgentState]

CALL = ToolCall(id="c1", name="bash", args={"command": "curl x"})
ASSISTANT = Message(role="assistant", content="", tool_calls=(CALL,))
TOOL = Message(role="tool", content="ok", tool_call_id="c1", name="bash")
PENDING = PendingApproval(
    approval_id="appr_1",
    call_ids=("c1",),
    required_level="full_permissions",
    current_level="workspace_write",
    action_type="network",
    command="curl x",
    reason="r",
    escalation_count=1,
)


def done(i: int = 0) -> Milestone:
    return Milestone(id=i, title="m", status="completed")


# ==============================================================================
# 假件
# ==============================================================================


class Log:
    """按时间顺序记录提交与事件。"""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []
        self.committed: list[AgentState] = []
        self.events: list[tuple[str, AgentState]] = []

    def kinds(self) -> list[str]:
        return [f"{kind}:{what}" for kind, what in self.entries]


class FakeStore:
    def __init__(self, log: Log, fail_at: int | None = None) -> None:
        self._log = log
        self._fail_at = fail_at

    async def commit(self, state: AgentState) -> int:
        if self._fail_at is not None and len(self._log.committed) + 1 == self._fail_at:
            raise CheckpointError("disk full")
        self._log.committed.append(state)
        self._log.entries.append(("commit", state.phase))
        return len(self._log.committed)


class FakeSink:
    def __init__(self, log: Log, explode: bool = False) -> None:
        self._log = log
        self._explode = explode

    async def stage_committed(
        self, phase: Phase, update: Mapping[str, object], state: AgentState
    ) -> None:
        self._log.entries.append(("event", phase))
        self._log.events.append((phase, state))
        if self._explode:
            raise RuntimeError("event bus down")

    async def approval_requested(self, request: PendingApproval, state: AgentState) -> None:
        self._log.entries.append(("event", "approval"))
        self._log.events.append(("approval", state))


Step = (
    Mapping[str, object] | BaseException | Callable[[AgentState], Awaitable[Mapping[str, object]]]
)


class Script:
    """按阶段排好的脚本：每次被调用取下一项，并记录收到的状态。"""

    def __init__(self, **steps: list[Step]) -> None:
        self._steps = {k: list(v) for k, v in steps.items()}
        self.calls: list[tuple[str, AgentState]] = []

    def stage(self, name: str) -> Stage:
        async def run(state: AgentState) -> Mapping[str, object]:
            self.calls.append((name, state))
            queue = self._steps.get(name, [])
            step: Step = queue.pop(0) if queue else {}
            if isinstance(step, BaseException):
                raise step
            if callable(step):
                return await step(state)
            return step

        return run

    def runtime(self, log: Log, **kwargs: Any) -> TaskRuntime:
        stages = {
            p: self.stage(p)
            for p in ("planner", "budget_guard", "executor", "tool_runner", "evaluator")
        }
        return TaskRuntime(
            stages=stages,  # type: ignore[arg-type]
            checkpoints=kwargs.pop("checkpoints", FakeStore(log)),
            events=kwargs.pop("events", FakeSink(log)),
        )

    def visited(self) -> list[str]:
        return [name for name, _ in self.calls]


def happy_script() -> Script:
    return Script(
        planner=[{"milestones": [Milestone(id=0, title="m")]}, {"milestones": [done()]}],
        executor=[{"messages": [ASSISTANT], "step_count": 1}],
        tool_runner=[{"messages": [TOOL]}],
    )


# ==============================================================================
# 正常流程
# ==============================================================================


@pytest.mark.unit
async def test_full_run_visits_stages_in_route_order(make_state: MakeState) -> None:
    script, log = happy_script(), Log()
    outcome = await drive(script.runtime(log), make_state())
    assert script.visited() == [
        "planner",
        "budget_guard",
        "executor",
        "tool_runner",
        "planner",
        "evaluator",
    ]
    assert outcome.state.phase == DONE
    assert outcome.status == "succeeded"
    assert outcome.approval_request is None


@pytest.mark.unit
async def test_each_stage_sees_its_own_phase_and_earlier_updates(make_state: MakeState) -> None:
    script = happy_script()
    await drive(script.runtime(Log()), make_state())
    for name, seen in script.calls:
        assert seen.phase == name
    executor_state = dict(script.calls)["executor"]
    assert executor_state.milestones[0].title == "m"
    tool_runner_state = dict(script.calls)["tool_runner"]
    assert tool_runner_state.messages == (ASSISTANT,)
    assert tool_runner_state.step_count == 1


@pytest.mark.unit
async def test_one_checkpoint_per_stage_with_the_next_phase(make_state: MakeState) -> None:
    script, log = happy_script(), Log()
    await drive(script.runtime(log), make_state())
    assert [s.phase for s in log.committed] == [
        "budget_guard",
        "executor",
        "tool_runner",
        "planner",
        "evaluator",
        DONE,
    ]


@pytest.mark.unit
async def test_final_checkpoint_is_the_returned_state(make_state: MakeState) -> None:
    log = Log()
    outcome = await drive(happy_script().runtime(log), make_state())
    assert log.committed[-1] == outcome.state


@pytest.mark.unit
async def test_hard_termination_ends_with_terminated_status(make_state: MakeState) -> None:
    script = Script(
        budget_guard=[{"should_terminate": True, "termination_reason": "步数用尽"}],
    )
    outcome = await drive(script.runtime(Log()), make_state())
    assert script.visited() == ["planner", "budget_guard"]
    assert outcome.state.phase == DONE
    assert outcome.status == "terminated"
    assert outcome.state.termination_reason == "步数用尽"


@pytest.mark.unit
async def test_planner_direct_answer_finishes_immediately(make_state: MakeState) -> None:
    script = Script(planner=[{"milestones": [done()]}])
    outcome = await drive(script.runtime(Log()), make_state())
    assert script.visited() == ["planner"]
    assert outcome.status == "succeeded"


@pytest.mark.unit
async def test_driving_a_finished_state_does_nothing(make_state: MakeState) -> None:
    script, log = Script(), Log()
    finished = apply_update(make_state(), {"phase": DONE})
    outcome = await drive(script.runtime(log), finished)
    assert script.calls == [] and log.committed == []
    assert outcome.state == finished


@pytest.mark.unit
async def test_drive_starts_from_the_phase_in_the_state(make_state: MakeState) -> None:
    """续跑：从检查点里的阶段继续，而不是从头。"""
    script = Script(evaluator=[{"milestones": [done()]}])
    state = apply_update(make_state(), {"phase": "evaluator", "milestones": [done()]})
    await drive(script.runtime(Log()), state)
    assert script.visited() == ["evaluator"]


# ==============================================================================
# 先提交，后发事件
# ==============================================================================


@pytest.mark.unit
async def test_every_event_follows_its_own_commit(make_state: MakeState) -> None:
    log = Log()
    await drive(happy_script().runtime(log), make_state())
    kinds = [k for k, _ in log.entries]
    assert kinds == ["commit", "event"] * 6


@pytest.mark.unit
async def test_events_carry_the_committed_state(make_state: MakeState) -> None:
    log = Log()
    await drive(happy_script().runtime(log), make_state())
    for committed, (_, seen) in zip(log.committed, log.events, strict=True):
        assert seen == committed


@pytest.mark.unit
async def test_event_reports_the_stage_that_just_ran(make_state: MakeState) -> None:
    log = Log()
    await drive(happy_script().runtime(log), make_state())
    assert [phase for phase, _ in log.events] == [
        "planner",
        "budget_guard",
        "executor",
        "tool_runner",
        "planner",
        "evaluator",
    ]


@pytest.mark.unit
async def test_failed_commit_means_no_event_and_the_error_propagates(make_state: MakeState) -> None:
    script, log = happy_script(), Log()
    runtime = script.runtime(log, checkpoints=FakeStore(log, fail_at=2))
    with pytest.raises(CheckpointError):
        await drive(runtime, make_state())
    assert log.kinds() == ["commit:budget_guard", "event:planner"]
    assert script.visited() == ["planner", "budget_guard"]  # 不带着未持久化的进度继续跑


@pytest.mark.unit
async def test_a_broken_event_sink_does_not_stop_the_task(make_state: MakeState) -> None:
    log = Log()
    runtime = happy_script().runtime(log, events=FakeSink(log, explode=True))
    outcome = await drive(runtime, make_state())
    assert outcome.status == "succeeded"
    assert len(log.committed) == 6


# ==============================================================================
# ApprovalRequired：挂起
# ==============================================================================


def approval_script() -> Script:
    return Script(
        planner=[{"milestones": [Milestone(id=0, title="m")]}],
        executor=[{"messages": [ASSISTANT], "step_count": 1}],
        tool_runner=[ApprovalRequired(PENDING)],
    )


@pytest.mark.unit
async def test_approval_required_suspends_with_a_checkpoint(make_state: MakeState) -> None:
    script, log = approval_script(), Log()
    outcome = await drive(script.runtime(log), make_state())

    assert outcome.status == "waiting_for_approval"
    assert outcome.approval_request == PENDING
    assert outcome.state.pending_approval == PENDING
    assert outcome.state.phase == "tool_runner"  # 停在要重做的阶段
    assert log.committed[-1] == outcome.state
    assert script.visited() == ["planner", "budget_guard", "executor", "tool_runner"]


@pytest.mark.unit
async def test_suspension_event_comes_after_the_commit(make_state: MakeState) -> None:
    log = Log()
    await drive(approval_script().runtime(log), make_state())
    assert log.kinds()[-2:] == ["commit:tool_runner", "event:approval"]


@pytest.mark.unit
async def test_suspension_keeps_the_unpaired_tail_and_earlier_progress(
    make_state: MakeState,
) -> None:
    outcome = await drive(approval_script().runtime(Log()), make_state())
    assert outcome.state.messages == (ASSISTANT,)
    assert outcome.state.step_count == 1


@pytest.mark.unit
async def test_nothing_from_the_suspending_stage_is_applied(make_state: MakeState) -> None:
    async def half_done(state: AgentState) -> Mapping[str, object]:
        raise ApprovalRequired(PENDING)

    script = Script(
        planner=[{"milestones": [Milestone(id=0, title="m")]}],
        executor=[{"messages": [ASSISTANT], "step_count": 1}],
        tool_runner=[half_done],
    )
    outcome = await drive(script.runtime(Log()), make_state())
    assert outcome.state.consecutive_errors == 0
    assert outcome.state.total_tokens == 0
    assert outcome.state.fingerprint_history == ()


@pytest.mark.unit
async def test_resume_after_approval_re_enters_the_same_stage_and_continues(
    make_state: MakeState,
) -> None:
    script = approval_script()
    log = Log()
    runtime = script.runtime(log)
    suspended = (await drive(runtime, make_state())).state

    decided = apply_update(
        suspended, {"approval_decision": ApprovalDecision(approval_id="appr_1", approved=True)}
    )

    async def consume(state: AgentState) -> Mapping[str, object]:
        assert state.approval_decision is not None  # 重新进入时决策已在状态里
        return {"messages": [TOOL], "pending_approval": None, "approval_decision": None}

    script._steps["tool_runner"] = [consume]
    script._steps["planner"] = [{"milestones": [done()]}]
    outcome = await drive(runtime, decided)

    assert outcome.status == "succeeded"
    assert outcome.state.pending_approval is None
    assert outcome.state.approval_decision is None
    assert script.visited()[-3:] == ["tool_runner", "planner", "evaluator"]


@pytest.mark.unit
async def test_suspending_twice_in_a_row_is_fine(make_state: MakeState) -> None:
    script = approval_script()
    runtime = script.runtime(Log())
    first = (await drive(runtime, make_state())).state
    script._steps["tool_runner"] = [ApprovalRequired(PENDING)]
    second = await drive(runtime, first)
    assert second.status == "waiting_for_approval"


# ==============================================================================
# 异常、取消、不变量
# ==============================================================================


@pytest.mark.unit
async def test_other_exceptions_propagate_and_state_stays_at_the_last_commit(
    make_state: MakeState,
) -> None:
    script = Script(
        planner=[{"milestones": [Milestone(id=0, title="m")]}],
        executor=[RuntimeError("boom")],
    )
    log = Log()
    with pytest.raises(RuntimeError, match="boom"):
        await drive(script.runtime(log), make_state())
    assert [s.phase for s in log.committed] == ["budget_guard", "executor"]
    assert log.kinds()[-1] == "event:budget_guard"


@pytest.mark.unit
async def test_cancellation_propagates_and_leaves_the_last_commit(make_state: MakeState) -> None:
    started = asyncio.Event()

    async def hang(state: AgentState) -> Mapping[str, object]:
        started.set()
        await asyncio.Event().wait()
        return {}

    script = Script(executor=[hang])
    log = Log()
    task = asyncio.create_task(drive(script.runtime(log), make_state()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [s.phase for s in log.committed] == ["budget_guard", "executor"]


@pytest.mark.unit
async def test_unpaired_tool_calls_are_never_committed(make_state: MakeState) -> None:
    """tool_runner 之后（回到 planner）必须配好对；漏配即实现缺陷，不写检查点。"""
    script = Script(
        planner=[{"milestones": [Milestone(id=0, title="m")]}],
        executor=[{"messages": [ASSISTANT], "step_count": 1}],
        tool_runner=[{}],  # 忘了产出 tool 消息
    )
    log = Log()
    with pytest.raises(AtomicPairError):
        await drive(script.runtime(log), make_state())
    assert [s.phase for s in log.committed] == ["budget_guard", "executor", "tool_runner"]


@pytest.mark.unit
async def test_executor_checkpoint_may_hold_the_calls_awaiting_tool_runner(
    make_state: MakeState,
) -> None:
    log = Log()
    await drive(happy_script().runtime(log), make_state())
    after_executor = next(s for s in log.committed if s.phase == "tool_runner")
    assert after_executor.messages == (ASSISTANT,)


@pytest.mark.unit
async def test_stages_may_not_set_the_phase(make_state: MakeState) -> None:
    script = Script(planner=[{"phase": "evaluator"}])
    with pytest.raises(InvalidUpdateError, match="phase"):
        await drive(script.runtime(Log()), make_state())


@pytest.mark.unit
async def test_invalid_updates_are_rejected_before_any_commit(make_state: MakeState) -> None:
    script, log = Script(planner=[{"nonsense": 1}]), Log()
    with pytest.raises(InvalidUpdateError):
        await drive(script.runtime(log), make_state())
    assert log.committed == []


@pytest.mark.unit
def test_runtime_requires_all_five_stages() -> None:
    log = Log()
    with pytest.raises(ValueError, match="evaluator"):
        TaskRuntime(
            stages={"planner": Script().stage("planner")},  # type: ignore[arg-type]
            checkpoints=FakeStore(log),
            events=FakeSink(log),
        )


# ==============================================================================
# 与真实检查点库联用
# ==============================================================================


@pytest.mark.integration
async def test_crash_mid_run_then_continue_from_the_stored_checkpoint(
    tmp_path: Path, make_state: MakeState
) -> None:
    db = tmp_path / "state.db"
    script = Script(
        planner=[{"milestones": [Milestone(id=0, title="m")]}, {"milestones": [done()]}],
        executor=[{"messages": [ASSISTANT], "step_count": 1}],
        tool_runner=[RuntimeError("process died")],
    )
    async with CheckpointStore(db) as store:
        runtime = script.runtime(Log(), checkpoints=store)
        with pytest.raises(RuntimeError, match="process died"):
            await drive(runtime, make_state())

    # “重启”：新连接，从库里取回状态，换一套（不再崩溃的）阶段函数继续
    async with CheckpointStore(db) as store:
        restored = await store.load("t1")
        assert restored is not None
        assert restored.phase == "tool_runner"
        resumed = Script(tool_runner=[{"messages": [TOOL]}], planner=[{"milestones": [done()]}])
        outcome = await drive(resumed.runtime(Log(), checkpoints=store), restored)
        assert outcome.status == "succeeded"
        assert await store.load("t1") == outcome.state

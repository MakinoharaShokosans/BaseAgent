"""budget_guard 阶段：把守卫的判定变成状态增量，并保证循环必然终止。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.checkpoint import CheckpointStore
from agent_runtime.core import DONE, AgentState, Message, Milestone, PendingApproval, apply_update
from agent_runtime.guardrails import BudgetLimits, PhysicalBudgetGuard
from agent_runtime.loop import Stage, TaskRuntime, drive
from agent_runtime.loop.stages import make_budget_guard_stage
from agent_runtime.runtime import ActiveTasks, ResumeDeps, resume_task

MakeState = Callable[..., AgentState]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def limits(**kw: Any) -> BudgetLimits:
    base: dict[str, Any] = {
        "max_steps": 10,
        "max_total_tokens": 1000,
        "max_wall_time_sec": 100,
        "warning_ratio": 0.9,
    }
    return BudgetLimits(**{**base, **kw})


class NullSink:
    async def stage_committed(
        self, phase: str, update: Mapping[str, object], state: AgentState
    ) -> None:
        pass

    async def approval_requested(self, request: PendingApproval, state: AgentState) -> None:
        pass


class MemoryStore:
    def __init__(self) -> None:
        self.commits: list[AgentState] = []

    async def commit(self, state: AgentState) -> int:
        self.commits.append(state)
        return len(self.commits)


# ==============================================================================
# 阶段本身
# ==============================================================================


@pytest.mark.unit
async def test_within_budget_the_update_is_empty(make_state: MakeState) -> None:
    stage = make_budget_guard_stage(PhysicalBudgetGuard(limits(), clock=FakeClock()))
    state = apply_update(make_state(), {"step_count": 2, "total_tokens": 100})
    assert await stage(state) == {}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("update", "needle"),
    [
        ({"step_count": 10}, "步数"),
        ({"total_tokens": 1000}, "Token"),
    ],
)
async def test_exhaustion_sets_the_hard_stop_with_a_reason(
    make_state: MakeState, update: dict[str, Any], needle: str
) -> None:
    stage = make_budget_guard_stage(PhysicalBudgetGuard(limits(), clock=FakeClock()))
    result = await stage(apply_update(make_state(), update))
    assert result["should_terminate"] is True
    assert needle in str(result["termination_reason"])
    assert "messages" not in result  # 熔断时不再追加告警


@pytest.mark.unit
async def test_wall_time_exhaustion(make_state: MakeState) -> None:
    clock = FakeClock()
    stage = make_budget_guard_stage(PhysicalBudgetGuard(limits(), clock=clock))
    clock.now = 100
    result = await stage(make_state())
    assert result["should_terminate"] is True
    assert "挂钟" in str(result["termination_reason"])


@pytest.mark.unit
async def test_soft_warning_is_a_single_system_message(make_state: MakeState) -> None:
    stage = make_budget_guard_stage(PhysicalBudgetGuard(limits(), clock=FakeClock()))
    result = await stage(apply_update(make_state(), {"step_count": 9, "total_tokens": 950}))

    assert "should_terminate" not in result
    messages = result["messages"]
    assert isinstance(messages, list) and len(messages) == 1
    message = messages[0]
    assert isinstance(message, Message)
    assert message.role == "user"
    assert "9/10" in message.content  # 告诉模型还剩多少
    assert "950/1000" in message.content
    assert "收敛" in message.content


@pytest.mark.unit
async def test_warning_is_not_repeated_on_the_next_round(make_state: MakeState) -> None:
    """每次警告都会往追加式上下文里加一条，反复提醒既无用又破坏缓存。"""
    stage = make_budget_guard_stage(PhysicalBudgetGuard(limits(), clock=FakeClock()))
    state = apply_update(make_state(), {"step_count": 9})
    assert "messages" in await stage(state)
    assert await stage(apply_update(state, {"step_count": 9})) == {}


@pytest.mark.unit
async def test_the_stage_does_not_mutate_the_state(make_state: MakeState) -> None:
    stage = make_budget_guard_stage(PhysicalBudgetGuard(limits(), clock=FakeClock()))
    state = apply_update(make_state(), {"step_count": 10})
    await stage(state)
    assert state.should_terminate is False


# ==============================================================================
# 循环必然终止
# ==============================================================================


def looping_runtime(
    guard: PhysicalBudgetGuard, per_step_tokens: int = 0
) -> tuple[TaskRuntime, dict[str, int]]:
    """永远“没做完”的任务：planner 不完成里程碑，executor 每次加一步。"""
    counts = {"planner": 0, "executor": 0, "evaluator": 0, "tool_runner": 0}

    def counting(name: str, update: Callable[[AgentState], Mapping[str, object]]) -> Stage:
        async def run(state: AgentState) -> Mapping[str, object]:
            counts[name] += 1
            return update(state)

        return run

    stages: dict[str, Stage] = {
        "planner": counting("planner", lambda s: {"milestones": [Milestone(id=0, title="m")]}),
        "budget_guard": make_budget_guard_stage(guard),
        "executor": counting(
            "executor",
            lambda s: {
                "step_count": s.step_count + 1,
                "total_tokens": s.total_tokens + per_step_tokens,
                "messages": [Message(role="assistant", content="still thinking")],
            },
        ),
        "tool_runner": counting("tool_runner", lambda s: {}),
        "evaluator": counting("evaluator", lambda s: {}),
    }
    runtime = TaskRuntime(stages=stages, checkpoints=MemoryStore(), events=NullSink())  # type: ignore[arg-type]
    return runtime, counts


@pytest.mark.unit
async def test_a_task_that_never_converges_stops_at_the_step_limit(make_state: MakeState) -> None:
    runtime, counts = looping_runtime(PhysicalBudgetGuard(limits(max_steps=4), clock=FakeClock()))
    outcome = await drive(runtime, make_state())

    assert outcome.status == "terminated"
    assert outcome.state.phase == DONE
    assert counts["executor"] == 4
    assert outcome.state.step_count == 4
    assert "步数" in outcome.state.termination_reason


@pytest.mark.unit
async def test_a_token_hungry_task_stops_at_the_token_limit(make_state: MakeState) -> None:
    guard = PhysicalBudgetGuard(limits(max_steps=1000, max_total_tokens=250), clock=FakeClock())
    runtime, counts = looping_runtime(guard, per_step_tokens=100)
    outcome = await drive(runtime, make_state())
    assert counts["executor"] == 3  # 100、200、300：第三步之后越线
    assert "Token" in outcome.state.termination_reason


@pytest.mark.unit
async def test_the_model_sees_the_warning_before_the_fuse(make_state: MakeState) -> None:
    runtime, _ = looping_runtime(PhysicalBudgetGuard(limits(max_steps=10), clock=FakeClock()))
    outcome = await drive(runtime, make_state())
    warnings = [m for m in outcome.state.messages if m.role == "user" and "预算" in m.content]
    assert len(warnings) == 1
    index = outcome.state.messages.index(warnings[0])
    assert index < len(outcome.state.messages) - 1  # 告警之后模型还有机会再走几步


# ==============================================================================
# 续跑：挂钟重置，步数与 Token 沿用
# ==============================================================================


@pytest.mark.integration
async def test_resume_resets_the_clock_but_keeps_steps_and_tokens(
    tmp_path: Path, make_state: MakeState
) -> None:
    async with CheckpointStore(tmp_path / "state.db") as store:
        # 第一次运行：挂钟用尽
        clock = FakeClock()
        guard = PhysicalBudgetGuard(limits(), clock=clock)
        runtime, _ = looping_runtime(guard)
        state = apply_update(make_state(), {"step_count": 3, "total_tokens": 400})
        clock.now = 100  # 一开始就到点
        fused = await drive(
            TaskRuntime(stages=runtime.stages, checkpoints=store, events=NullSink()),  # type: ignore[arg-type]
            state,
        )
        assert "挂钟" in fused.state.termination_reason

        # 续跑：新的守卫、新的挂钟
        seen: list[AgentState] = []

        async def build(restored: AgentState) -> TaskRuntime:
            seen.append(restored)
            fresh = PhysicalBudgetGuard(limits(max_steps=6), clock=FakeClock())
            rt, _ = looping_runtime(fresh)
            return TaskRuntime(stages=rt.stages, checkpoints=store, events=NullSink())  # type: ignore[arg-type]

        deps = ResumeDeps(store, build, lambda name: False, ActiveTasks())
        outcome = await resume_task(deps, "t1")

        assert seen[0].step_count == 3 and seen[0].total_tokens == 400  # 沿用
        assert outcome.state.step_count == 6  # 从 3 走到新的步数上限
        assert "步数" in outcome.state.termination_reason  # 不再是挂钟


@pytest.mark.integration
async def test_resume_can_fuse_again_immediately_when_the_limit_is_unchanged(
    tmp_path: Path, make_state: MakeState
) -> None:
    """文档里的已知行为：步数沿用，所以上限不变时续跑可能立刻再熔断。"""
    async with CheckpointStore(tmp_path / "state.db") as store:
        guard = PhysicalBudgetGuard(limits(max_steps=3), clock=FakeClock())
        rt, _ = looping_runtime(guard)
        first = await drive(
            TaskRuntime(stages=rt.stages, checkpoints=store, events=NullSink()), make_state()
        )  # type: ignore[arg-type]
        assert first.state.step_count == 3

        async def build(restored: AgentState) -> TaskRuntime:
            fresh, _ = looping_runtime(PhysicalBudgetGuard(limits(max_steps=3), clock=FakeClock()))
            return TaskRuntime(stages=fresh.stages, checkpoints=store, events=NullSink())  # type: ignore[arg-type]

        outcome = await resume_task(ResumeDeps(store, build, lambda n: False, ActiveTasks()), "t1")
        assert outcome.state.step_count == 3  # 一步都没多走
        assert outcome.status == "terminated"

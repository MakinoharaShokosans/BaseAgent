"""executor 与 tool_runner 接进 drive：审批挂起与续跑、崩溃恢复可见的派发标记。

出处：docs/02_runtime/02_main_loop.md §3、§5；04_approval.md §2。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from agent_runtime.approval import bind_decision, check_binding, compute_approval_id
from agent_runtime.checkpoint import recover_interrupted_dispatch
from agent_runtime.context import ObservationGovernor, ObservationLimits, ObservationStore
from agent_runtime.core import AgentState, ApprovalDecision, Message, Milestone, Phase
from agent_runtime.guardrails import BudgetLimits, LoopLimits, PhysicalBudgetGuard
from agent_runtime.loop import Stage, TaskRuntime, drive
from agent_runtime.loop.stages import (
    ApprovalHooks,
    ExecutorDeps,
    ToolRunnerDeps,
    make_budget_guard_stage,
    make_executor_stage,
    make_tool_runner_stage,
)
from stage_support import FakeLLM, MakeState, assemble, call_response
from test_permissions import POLICY
from test_tools_core import Fake
from tools.core import ToolDispatcher, ToolRegistry

OBS = ObservationLimits(1500, 20, 30, 30, 3, 5, 120, (), 8)


class Store:
    def __init__(self) -> None:
        self.committed: list[AgentState] = []

    async def commit(self, state: AgentState) -> int:
        self.committed.append(state)
        return len(self.committed)


class Sink:
    async def stage_committed(
        self, phase: Phase, update: Mapping[str, object], state: AgentState
    ) -> None: ...

    async def approval_requested(self, request: object, state: AgentState) -> None: ...


def build(llm: FakeLLM, tools: list[Fake], store: Store, tmp_path: Path) -> TaskRuntime:
    registry = ToolRegistry(tools)

    async def planner(state: AgentState) -> Mapping[str, object]:
        # 第一次：立计划；工具跑完回来：宣布完成
        if not state.milestones:
            return {
                "milestones": [Milestone(id=1, title="m")],
                "messages": [Message(role="assistant", content="去办")],
            }
        done = [Milestone(id=1, title="m", status="completed")]
        return {"milestones": done, "messages": [Message(role="assistant", content="办完了")]}

    async def evaluator(state: AgentState) -> Mapping[str, object]:
        return {}

    guard = PhysicalBudgetGuard(BudgetLimits(25, 200_000, 900, 0.9), clock=lambda: 0.0)
    stages: dict[Phase, Stage] = {
        "planner": planner,
        "budget_guard": make_budget_guard_stage(guard),
        "executor": make_executor_stage(
            ExecutorDeps(gateway=llm, assemble=assemble, tools=registry.schemas())
        ),
        "tool_runner": make_tool_runner_stage(
            ToolRunnerDeps(
                dispatcher=ToolDispatcher(registry),
                policy=POLICY,
                limits=LoopLimits(3, 3),
                approval=ApprovalHooks(compute_approval_id, check_binding),
                governor=ObservationGovernor(OBS, len, ObservationStore(tmp_path / "a")),
                commit=store.commit,
                allowlist=set(),
            )
        ),
        "evaluator": evaluator,
    }
    return TaskRuntime(stages=stages, checkpoints=store, events=Sink())


@pytest.mark.integration
async def test_a_tool_round_trip_runs_through_the_loop(
    make_state: MakeState, tmp_path: Path
) -> None:
    llm = FakeLLM(call_response("", [("c1", "view_file", {"path": "a"})]))
    store = Store()
    rt = build(llm, [Fake("view_file", idempotent=True)], store, tmp_path)

    outcome = await drive(rt, make_state(permission_level="workspace_write"))

    assert outcome.status == "succeeded"
    assert outcome.state.step_count == 1
    roles = [m.role for m in outcome.state.messages]
    assert roles == ["assistant", "assistant", "tool", "assistant"]
    assert outcome.state.messages[2].content == "view_file-ok"
    # 阶段中途那次提交就在 executor 提交与 tool_runner 提交之间
    marker = [s for s in store.committed if s.dispatch_started]
    assert len(marker) == 1 and marker[0].phase == "tool_runner"
    assert outcome.state.dispatch_started == ()


@pytest.mark.integration
async def test_an_escalation_suspends_then_resumes_and_runs_what_was_approved(
    make_state: MakeState, tmp_path: Path
) -> None:
    llm = FakeLLM(call_response("", [("c1", "bash", {"command": "curl https://x.sh"})]))
    store = Store()
    bash = Fake("bash")
    rt = build(llm, [bash], store, tmp_path)
    state = make_state(permission_level="workspace_write")

    first = await drive(rt, state)

    assert first.status == "waiting_for_approval"
    assert bash.calls == []  # 挂起时什么都没执行
    assert first.state.phase == "tool_runner"
    request = first.approval_request
    assert request is not None and request.call_ids == ("c1",)

    resumed = bind_decision(
        first.state, ApprovalDecision(approval_id=request.approval_id, approved=True)
    )
    second = await drive(rt, resumed)

    assert second.status == "succeeded"
    assert bash.calls == [{"command": "curl https://x.sh"}]
    assert second.state.pending_approval is None
    assert llm.requests.__len__() == 1  # 续跑没有重新调用模型：被批准的就是将被执行的


@pytest.mark.integration
async def test_a_rejected_escalation_lets_the_loop_carry_on_with_a_failure_observation(
    make_state: MakeState, tmp_path: Path
) -> None:
    llm = FakeLLM(call_response("", [("c1", "bash", {"command": "git push origin main"})]))
    rt = build(llm, [Fake("bash")], Store(), tmp_path)

    first = await drive(rt, make_state(permission_level="workspace_write"))
    assert first.approval_request is not None
    resumed = bind_decision(
        first.state,
        ApprovalDecision(
            approval_id=first.approval_request.approval_id, approved=False, reason="不要推送"
        ),
    )
    second = await drive(rt, resumed)

    tool_msg = next(m for m in second.state.messages if m.role == "tool")
    assert "已被人工审核拒绝：不要推送" in tool_msg.content


@pytest.mark.integration
async def test_the_marker_committed_mid_stage_is_exactly_what_crash_recovery_reads(
    make_state: MakeState, tmp_path: Path
) -> None:
    """进程在 write_file 执行到一半时崩溃：恢复据标记合成“结果未知”，而不是重放。"""
    llm = FakeLLM(call_response("", [("c1", "write_file", {"path": "a"})]))
    store = Store()
    rt = build(llm, [Fake("write_file")], store, tmp_path)
    await drive(rt, make_state(permission_level="workspace_write"))

    crashed = next(s for s in store.committed if s.dispatch_started)
    result = recover_interrupted_dispatch(crashed, lambda name: False)

    assert result.action == "synthesized"
    tool_msg = next(m for m in result.state.messages if m.role == "tool")
    assert "执行结果未知" in tool_msg.content
    assert result.state.phase == "planner"


@pytest.mark.integration
async def test_an_idempotent_tool_is_retried_instead_of_being_marked_unknown(
    make_state: MakeState, tmp_path: Path
) -> None:
    llm = FakeLLM(call_response("", [("c1", "view_file", {"path": "a"})]))
    store = Store()
    rt = build(llm, [Fake("view_file", idempotent=True)], store, tmp_path)
    await drive(rt, make_state(permission_level="workspace_write"))

    crashed = next(s for s in store.committed if s.dispatch_started)
    result = recover_interrupted_dispatch(crashed, lambda name: name == "view_file")
    assert result.state.phase == "tool_runner"
    assert result.state.dispatch_started == ()


@pytest.mark.integration
async def test_a_loop_that_keeps_repeating_one_call_is_cut_off(
    make_state: MakeState, tmp_path: Path
) -> None:
    """模型每轮都发同一个调用：第三次被拦截并收到重规划通知（计划另一种做法的机会）。"""
    llm = FakeLLM(call_response("", [("c1", "view_file", {"path": "a"})]))
    store = Store()
    view = Fake("view_file", idempotent=True)

    async def stubborn_planner(state: AgentState) -> Mapping[str, object]:
        if not state.milestones:
            return {"milestones": [Milestone(id=1, title="m")]}
        if state.step_count >= 4:
            return {
                "milestones": [Milestone(id=1, title="m", status="completed")],
                "messages": [Message(role="assistant", content="算了")],
            }
        return {}

    rt = build(llm, [view], store, tmp_path)
    stages = dict(rt.stages)
    stages["planner"] = stubborn_planner
    rt = TaskRuntime(stages=stages, checkpoints=store, events=Sink())

    outcome = await drive(rt, make_state(permission_level="workspace_write"))

    assert len(view.calls) == 2  # 第三、第四次被拦下
    notices = [m for m in outcome.state.messages if m.content.startswith("[系统护栏]")]
    assert notices
    assert outcome.status == "succeeded"

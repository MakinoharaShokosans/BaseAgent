"""循环与生命周期到事件的翻译。出处：docs/09_api/02_events_contract.md、08_observability/01_events.md。"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from agent_runtime.core import (
    AgentState,
    ApprovalRequired,
    Message,
    Milestone,
    PendingApproval,
    ToolCall,
    apply_update,
)
from agent_runtime.loop import Stage, TaskRuntime, drive
from agent_runtime.observability import Event, EventHub, Redactor
from agent_runtime.runtime import (
    LifecycleEvents,
    TaskEvents,
    TaskSnapshot,
    approval_payload,
    delivery_of,
)

MakeState = Callable[..., AgentState]
REDACTOR = Redactor(["key", "token", "secret", "password"], [r"sk-[A-Za-z0-9]{16,}"])
CALL = ToolCall(id="c1", name="bash", args={"command": "ls", "api_key": "hunter2"})


@pytest.fixture
def make_state(make_state: MakeState) -> MakeState:
    """在基础状态上覆盖任意 AgentState 字段（包括阶段、计数）。"""

    def build(**fields: Any) -> AgentState:
        return dataclasses.replace(make_state(), **fields)

    return build


def names(events: list[Event]) -> list[str]:
    return [e.name for e in events]


async def history(hub: EventHub, task: str = "t1") -> list[Event]:
    return await hub.history(task)


def events_for(hub: EventHub) -> TaskEvents:
    return TaskEvents(hub, "t1", REDACTOR, canary="canary-abc")


@pytest.mark.unit
async def test_planner_commit_emits_plan_then_node_finished(make_state: MakeState) -> None:
    hub = EventHub()
    state = make_state(
        milestones=(Milestone(id=0, title="读代码", status="in_progress"),),
        phase="budget_guard",
        messages=(Message(role="user", content="g"), Message(role="assistant", content="先读代码")),
    )
    await events_for(hub).stage_committed(
        "planner", {"messages": [Message(role="assistant", content="先读代码")]}, state
    )
    got = await history(hub)
    assert names(got) == ["plan", "node.finished"]
    plan, node = got
    assert plan.data["directive"] == "先读代码" and plan.data["milestones"][0]["title"] == "读代码"  # type: ignore[index]
    assert node.data["node"] == "planner" and node.data["decision"] == "先读代码"
    assert node.data["stage"] == 1 and node.data["milestones"] == plan.data["milestones"]


@pytest.mark.unit
async def test_stage_ordinals_are_monotonic_and_can_continue_after_a_resume(
    make_state: MakeState,
) -> None:
    hub = EventHub()
    ev = TaskEvents(hub, "t1", REDACTOR, stage_offset=7)
    state = make_state(phase="executor")
    await ev.stage_committed("budget_guard", {}, state)
    await ev.stage_committed("budget_guard", {}, state)
    assert [e.data["stage"] for e in await history(hub)] == [8, 9]


@pytest.mark.unit
async def test_direct_answer_by_planner_is_a_reply_with_content(make_state: MakeState) -> None:
    hub = EventHub()
    state = make_state(phase="done", messages=(Message(role="assistant", content="答案是 4"),))
    await events_for(hub).stage_committed(
        "planner", {"messages": [Message(role="assistant", content="答案是 4")]}, state
    )
    node = (await history(hub))[-1]
    assert node.data["decision"] == "reply" and node.data["content"] == "答案是 4"


@pytest.mark.unit
async def test_executor_emits_one_tool_call_per_call_with_redacted_args(
    make_state: MakeState,
) -> None:
    hub = EventHub()
    msg = Message(
        role="assistant",
        content="",
        tool_calls=(CALL, ToolCall(id="c2", name="view_file", args={"path": "a.py"})),
    )
    state = make_state(step_count=3, phase="tool_runner", messages=(msg,))
    await events_for(hub).stage_committed("executor", {"messages": [msg]}, state)
    got = await history(hub)
    assert names(got) == ["tool.call", "tool.call", "node.finished"]
    first = got[0].data
    assert first["tool"] == "bash" and first["step"] == 3
    assert first["args"] == {"command": "ls", "api_key": "[REDACTED]"}
    assert got[-1].data["decision"] == "call_tools"


@pytest.mark.unit
async def test_executor_without_calls_is_a_reply_and_emits_no_tool_call(
    make_state: MakeState,
) -> None:
    hub = EventHub()
    msg = Message(role="assistant", content="已完成")
    await events_for(hub).stage_committed(
        "executor", {"messages": [msg]}, make_state(phase="evaluator", messages=(msg,))
    )
    got = await history(hub)
    assert names(got) == ["node.finished"] and got[0].data["decision"] == "reply"


@pytest.mark.unit
async def test_tool_result_carries_ok_artifact_and_clipped_redacted_text(
    make_state: MakeState,
) -> None:
    hub = EventHub()
    ev = events_for(hub)
    await ev.emit("tool.observed", {"call_id": "c1", "ok": False, "artifact_id": "obs_1"})
    body = "boom canary-abc sk-abcdefghijklmnop1234 " + "x" * 800
    tool = Message(role="tool", content=body, tool_call_id="c1", name="bash")
    ok_tool = Message(role="tool", content="fine", tool_call_id="c2", name="view_file")
    await ev.stage_committed(
        "tool_runner", {"messages": [tool, ok_tool]}, make_state(step_count=2, phase="planner")
    )
    got = await history(hub)
    assert names(got) == ["tool.result", "tool.result", "node.finished"]
    bad, good = got[0].data, got[1].data
    assert bad["ok"] is False and bad["artifact_id"] == "obs_1" and bad["call_id"] == "c1"
    assert len(bad["result"]) <= 500 and "canary-abc" not in bad["result"]  # type: ignore[operator]
    assert "sk-abcdefghijklmnop1234" not in bad["result"]  # type: ignore[operator]
    assert good["ok"] is True and good["artifact_id"] == ""


@pytest.mark.unit
async def test_observed_marker_is_not_published_as_an_event(make_state: MakeState) -> None:
    hub = EventHub()
    await events_for(hub).emit("tool.observed", {"call_id": "c1", "ok": True})
    assert await history(hub) == []


@pytest.mark.unit
async def test_other_stage_emits_pass_through_scrubbed(make_state: MakeState) -> None:
    hub = EventHub()
    await events_for(hub).emit("guard.replan_notice", {"consecutive_errors": 3, "token": "zzz"})
    (ev,) = await history(hub)
    assert ev.name == "guard.replan_notice" and ev.data["token"] == "[REDACTED]"


@pytest.mark.unit
async def test_evaluator_decision_accepted_or_continue(make_state: MakeState) -> None:
    hub = EventHub()
    ev = events_for(hub)
    done = make_state(phase="done")
    more = make_state(phase="planner")
    await ev.stage_committed("evaluator", {"milestones": ()}, done)
    await ev.stage_committed("evaluator", {"milestones": ()}, more)
    assert [e.data["decision"] for e in await history(hub) if e.name == "node.finished"] == [
        "accepted",
        "continue",
    ]


@pytest.mark.unit
async def test_budget_guard_decision(make_state: MakeState) -> None:
    hub = EventHub()
    ev = events_for(hub)
    await ev.stage_committed("budget_guard", {}, make_state(phase="executor"))
    await ev.stage_committed(
        "budget_guard",
        {},
        make_state(phase="done", should_terminate=True, termination_reason="步数上限"),
    )
    got = [e for e in await history(hub) if e.name == "node.finished"]
    assert [e.data["decision"] for e in got] == ["pass", "terminate"]
    assert got[1].data["should_terminate"] is True


# ------------------------------------------------------------------ 审批


PENDING = PendingApproval(
    approval_id="appr_1",
    call_ids=("c1",),
    required_level="full_permissions",
    current_level="workspace_write",
    action_type="network_egress",
    command="curl -H 'Authorization: Bearer abc.def' https://x " + "y" * 400,
    reason="命中网络外联；" + "z" * 400,
    escalation_count=2,
    related_actions=("pip install requests",),
)


@pytest.mark.unit
async def test_approval_event_has_the_contract_shape_and_the_full_command(
    make_state: MakeState,
) -> None:
    hub = EventHub()
    await events_for(hub).approval_requested(PENDING, make_state())
    (ev,) = await history(hub)
    assert ev.name == "task.waiting_for_approval"
    assert set(ev.data) == {
        "approval_id",
        "call_ids",
        "required_level",
        "current_level",
        "action_type",
        "command",
        "reason",
        "escalation_count",
        "related_actions",
    }
    assert "abc.def" not in ev.data["command"]  # type: ignore[operator]
    assert str(ev.data["command"]).endswith("y" * 400)  # 命令不截断
    assert len(str(ev.data["reason"])) <= 300  # 摘要类字段由总线限长


@pytest.mark.unit
def test_approval_payload_is_the_same_shape_for_task_out() -> None:
    assert approval_payload(PENDING, REDACTOR)["call_ids"] == ["c1"]


# ------------------------------------------------------------------ 生命周期


def finished(state: AgentState | None, status: str = "succeeded", reason: str = "") -> TaskSnapshot:
    return TaskSnapshot("t1", status, reason, state)  # type: ignore[arg-type]


async def no_state(_: str) -> AgentState | None:
    return None


@pytest.mark.unit
async def test_started_and_finished_carry_status_counts_and_delivery(
    make_state: MakeState,
) -> None:
    hub = EventHub()
    life = LifecycleEvents(hub, REDACTOR, no_state)
    state = make_state(
        step_count=4,
        total_tokens=900,
        messages=(
            Message(role="assistant", content="调用中", tool_calls=(CALL,)),
            Message(role="assistant", content="结论：canary-abc 不应出现"),
        ),
    )
    await life.task_started("t1", state)
    await life.task_finished(finished(state))
    started, done = await history(hub)
    assert started.name == "task.started" and started.data["task_goal"] == "fix the bug"
    assert done.name == "task.finished"
    assert done.data["status"] == "succeeded" and done.data["step_count"] == 4
    assert done.data["total_tokens"] == 900
    assert done.data["delivery"] == "结论：[REDACTED] 不应出现"


@pytest.mark.unit
async def test_failed_task_emits_error_before_the_final_event(make_state: MakeState) -> None:
    hub = EventHub()

    async def load(_: str) -> AgentState | None:
        return make_state(step_count=2)

    await LifecycleEvents(hub, REDACTOR, load).task_finished(
        finished(None, "failed", "RuntimeError: boom")
    )
    error, done = await history(hub)
    assert (error.name, error.data["code"]) == ("task.error", "TASK_FAILED")
    assert done.name == "task.finished" and done.data["step_count"] == 2
    assert done.seq > error.seq


@pytest.mark.unit
async def test_delivery_is_the_last_plain_assistant_message() -> None:
    assert delivery_of(None) == ""


@pytest.mark.unit
async def test_a_finished_task_delivers_its_last_plain_assistant_message(
    make_state: MakeState,
) -> None:
    state = make_state(
        messages=(
            Message(role="user", content="做"),
            Message(role="assistant", content="结论一"),
            Message(role="assistant", content="", tool_calls=(CALL,)),
        )
    )
    assert delivery_of(state) == "结论一"


@pytest.mark.unit
async def test_a_terminated_task_delivers_what_was_achieved_not_an_internal_directive(
    make_state: MakeState,
) -> None:
    from agent_runtime.core import Milestone

    edit = ToolCall(id="c2", name="edit_file", args={"path": "src/app.py"})
    state = make_state(
        should_terminate=True,
        termination_reason="验收累计 4 次未通过，已超过上限 3",
        milestones=(
            Milestone(id=1, title="定位问题", status="completed"),
            Milestone(id=2, title="修复并测试", status="in_progress"),
        ),
        confirmed_facts=("bug 在 parse()",),
        rolling_summary="已改 parse，测试仍失败",
        messages=(
            Message(role="user", content="修 bug"),
            Message(role="assistant", content="", tool_calls=(edit,)),
            Message(role="tool", content="ok", tool_call_id="c2", name="edit_file"),
            Message(role="assistant", content="继续推进当前里程碑。"),
        ),
    )
    text = delivery_of(state)
    assert "任务没有完成：验收累计 4 次未通过" in text
    assert "- 定位问题" in text and "未完成：\n- 修复并测试" in text
    assert "已修改的文件：src/app.py" in text and "bug 在 parse()" in text
    assert "已改 parse，测试仍失败" in text
    assert "继续推进当前里程碑" not in text


@pytest.mark.unit
async def test_files_whose_edit_was_rejected_or_blocked_are_not_listed_as_modified(
    make_state: MakeState,
) -> None:
    def edit(i: str, path: str) -> Message:
        return Message(
            role="assistant",
            content="",
            tool_calls=(ToolCall(id=i, name="write_file", args={"path": path}),),
        )

    def seen(i: str, text: str) -> Message:
        return Message(role="tool", content=text, tool_call_id=i, name="write_file")

    state = make_state(
        should_terminate=True,
        termination_reason="x",
        messages=(
            Message(role="user", content="写"),
            edit("c1", "done.py"),
            seen("c1", "已写入 done.py（3 字符）"),
            edit("c2", "refused.py"),
            seen("c2", "已被人工审核拒绝：不允许。请改用合规方案"),
            edit("c3", "blocked.py"),
            seen("c3", "已拦截：相同工具与参数连续重复，请更换策略后重试。"),
            edit("c4", "bad_args.py"),
            seen("c4", "未执行：该调用的参数无法使用"),
        ),
    )
    text = delivery_of(state)
    assert "已修改的文件：done.py" in text
    assert "refused.py" not in text and "blocked.py" not in text and "bad_args.py" not in text


@pytest.mark.unit
@pytest.mark.security
async def test_a_security_termination_delivers_no_content(make_state: MakeState) -> None:
    state = make_state(
        should_terminate=True,
        termination_reason="[SECURITY] Prompt leak detected via canary token",
        confirmed_facts=("秘密事实",),
        rolling_summary="含有敏感的摘要",
    )
    text = delivery_of(state)
    assert "安全" in text and "秘密事实" not in text and "敏感" not in text


# ------------------------------------------------------------------ 与 drive 联用


class CommitLog:
    def __init__(self) -> None:
        self.states: list[AgentState] = []

    async def commit(self, state: AgentState) -> int:
        self.states.append(state)
        return len(self.states)


@pytest.mark.unit
async def test_events_follow_commits_and_a_suspend_is_not_a_terminal(
    make_state: MakeState,
) -> None:
    hub = EventHub()
    seen_at_emit: list[tuple[str, int]] = []
    log = CommitLog()

    async def planner(state: AgentState) -> Mapping[str, object]:
        seen_at_emit.append(("planner-ran", len(log.states)))
        return {
            "milestones": (Milestone(id=0, title="m"),),
            "messages": [Message(role="assistant", content="干活")],
        }

    async def guard(state: AgentState) -> Mapping[str, object]:
        return {}

    asked = {"n": 0}

    async def executor(state: AgentState) -> Mapping[str, object]:
        asked["n"] += 1
        if asked["n"] == 1:
            raise ApprovalRequired(PENDING)
        return {"messages": [Message(role="assistant", content="好了")]}

    async def evaluator(state: AgentState) -> Mapping[str, object]:
        return {"milestones": (Milestone(id=0, title="m", status="completed"),)}

    async def never(state: AgentState) -> Mapping[str, object]:
        raise AssertionError

    stages: dict[str, Stage] = {
        "planner": planner,
        "budget_guard": guard,
        "executor": executor,
        "tool_runner": never,
        "evaluator": evaluator,
    }
    task_events = events_for(hub)

    class Sink:
        async def stage_committed(self, phase: Any, update: Any, state: AgentState) -> None:
            seen_at_emit.append((phase, len(log.states)))
            await task_events.stage_committed(phase, update, state)

        async def approval_requested(self, request: PendingApproval, state: AgentState) -> None:
            await task_events.approval_requested(request, state)

    rt = TaskRuntime(stages=stages, checkpoints=log, events=Sink())  # type: ignore[arg-type]
    first = await drive(rt, make_state())
    assert first.status == "waiting_for_approval"
    got = await history(hub)
    assert names(got)[-1] == "task.waiting_for_approval"
    assert ("planner", 1) in seen_at_emit  # 事件在对应提交之后
    resumed = apply_update(first.state, {"pending_approval": None})
    await drive(rt, resumed)
    final = await history(hub)
    assert [e.seq for e in final] == list(range(1, len(final) + 1))  # 续跑后序号接着走


# ------------------------------------------------------------------ llm.call


def _record(**kw: Any):  # type: ignore[no-untyped-def]
    from agent_runtime.llm import CallRecord, TokenUsage

    base: dict[str, Any] = {
        "purpose": "planner",
        "tier": "reasoning",
        "endpoint": "deepseek-pro",
        "model": "m",
        "attempt": 1,
        "outcome": "ok",
        "duration_ms": 42,
        "usage": TokenUsage(prompt_tokens=90, completion_tokens=10, total_tokens=100),
        "finish_reason": "stop",
    }
    return CallRecord(**{**base, **kw})


@pytest.mark.unit
async def test_gateway_records_become_llm_call_events_for_the_current_task() -> None:
    from agent_runtime.core import current_task_id
    from agent_runtime.runtime import LlmCallEvents

    hub = EventHub()
    calls = LlmCallEvents(hub)
    current_task_id.set("t1")
    calls(_record())
    calls(
        _record(attempt=2, outcome="error", usage=None, finish_reason=None, error_class="Timeout")
    )
    await calls.drain()
    first, second = await history(hub)
    assert first.name == "llm.call" and first.data["node"] == "planner"
    assert (first.data["tier"], first.data["tokens"], first.data["duration_ms"]) == (
        "reasoning",
        100,
        42,
    )
    assert second.data["attempts"] == 2 and second.data["error"] == "Timeout"
    assert second.data["tokens"] == 0 and "messages" not in first.data  # 不含消息正文


@pytest.mark.unit
async def test_llm_call_events_carry_the_usage_split_when_the_provider_gives_it() -> None:
    from agent_runtime.core import current_task_id
    from agent_runtime.llm import TokenUsage
    from agent_runtime.runtime import LlmCallEvents

    hub = EventHub()
    calls = LlmCallEvents(hub)
    current_task_id.set("t1")
    usage = TokenUsage(
        prompt_tokens=900,
        completion_tokens=100,
        total_tokens=1000,
        reasoning_tokens=40,
        cache_hit_tokens=800,
        cache_miss_tokens=100,
    )
    calls(_record(usage=usage))
    calls(_record())  # 服务商没给缓存字段：不写，而不是写 0
    await calls.drain()
    full, bare = await history(hub)
    assert (full.data["prompt_tokens"], full.data["completion_tokens"]) == (900, 100)
    assert (full.data["cache_hit_tokens"], full.data["cache_miss_tokens"]) == (800, 100)
    assert full.data["reasoning_tokens"] == 40
    assert "cache_hit_tokens" not in bare.data and "cache_miss_tokens" not in bare.data


@pytest.mark.unit
async def test_records_outside_a_task_are_dropped_and_drain_is_idempotent() -> None:
    from agent_runtime.core import current_task_id
    from agent_runtime.runtime import LlmCallEvents

    hub = EventHub()
    calls = LlmCallEvents(hub)
    current_task_id.set(None)
    calls(_record())
    await calls.drain()
    await calls.drain()
    assert await hub.history("t1") == []


@pytest.mark.unit
async def test_task_finished_is_still_last_when_a_call_record_is_in_flight(
    make_state: MakeState,
) -> None:
    from agent_runtime.core import current_task_id
    from agent_runtime.runtime import LlmCallEvents

    hub = EventHub()
    calls = LlmCallEvents(hub)
    life = LifecycleEvents(hub, REDACTOR, no_state, drain=calls.drain)
    current_task_id.set("t1")
    calls(_record(purpose="evaluator"))  # 回调是同步的，事件还没发出
    await life.task_finished(finished(make_state(), "succeeded"))
    assert names(await history(hub)) == ["llm.call", "task.finished"]


@pytest.mark.unit
async def test_the_runner_sets_the_current_task_for_everything_it_runs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from agent_runtime.checkpoint import CheckpointStore
    from agent_runtime.core import current_task_id
    from agent_runtime.runtime import RunnerDeps, TaskRequest, TaskRunner
    from api_support import World

    seen: list[str | None] = []
    async with CheckpointStore(tmp_path / "s.db") as store:
        hub = EventHub()
        world = World(store, hub)
        original = world.stage

        def spying(name: str, events):  # type: ignore[no-untyped-def]
            stage = original(name, events)

            async def wrapped(state: AgentState):  # type: ignore[no-untyped-def]
                seen.append(current_task_id.get())
                return await stage(state)

            return wrapped

        world.stage = spying  # type: ignore[method-assign]
        deps = RunnerDeps(store, world.build, lambda n: False, lambda s, t: "canary-abcdef")
        runner = TaskRunner(deps, new_task_id=lambda: "task-9")
        task_id = await runner.submit(TaskRequest("w", "/w", "s", "goal", "workspace_write"))
        await runner.wait(task_id)
    assert seen and set(seen) == {"task-9"}

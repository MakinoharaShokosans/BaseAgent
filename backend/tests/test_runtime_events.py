"""循环与生命周期到事件的翻译。出处：docs/09_api/02_events_contract.md、08_observability/01_events.md。"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import pytest

from agent_runtime.core import (
    AgentState,
    Message,
    PendingApproval,
    PlanItem,
    ToolCall,
)
from agent_runtime.loop import TaskRuntime, run
from agent_runtime.observability import Event, EventHub, Redactor
from agent_runtime.runtime import (
    LifecycleEvents,
    TaskEvents,
    TaskSnapshot,
    approval_payload,
    delivery_of,
)
from loop_support import ASSISTANT, FINAL, ScriptedWorld

MakeState = Callable[..., AgentState]
REDACTOR = Redactor(["key", "token", "secret", "password"], [r"sk-[A-Za-z0-9]{16,}"])
CALL = ToolCall(id="c1", name="bash", args={"command": "ls", "api_key": "hunter2"})


@pytest.fixture
def make_state(make_state: MakeState) -> MakeState:
    """在基础状态上覆盖任意 AgentState 字段。"""

    def build(**fields: Any) -> AgentState:
        return dataclasses.replace(make_state(), **fields)

    return build


def names(events: list[Event]) -> list[str]:
    return [e.name for e in events]


async def history(hub: EventHub, task: str = "t1") -> list[Event]:
    return await hub.history(task)


def events_for(hub: EventHub, **kw: Any) -> TaskEvents:
    return TaskEvents(hub, "t1", REDACTOR, canary="canary-abc", **kw)


# ------------------------------------------------------------------ 回合事件


@pytest.mark.unit
async def test_a_model_reply_with_tool_calls_emits_the_reply_and_one_tool_call_each(
    make_state: MakeState,
) -> None:
    hub = EventHub()
    msg = Message(
        role="assistant",
        content="先看看",
        tool_calls=(CALL, ToolCall(id="c2", name="view_file", args={"path": "a.py"})),
        reasoning="想一想" * 400,
    )
    await events_for(hub).model_replied(msg, make_state(messages=(msg,)))
    got = await history(hub)
    assert names(got) == ["model.reply", "tool.call", "tool.call"]
    reply = got[0].data
    assert reply["turn"] == 1 and reply["content"] == "先看看"
    assert reply["tool_calls"] == [{"id": "c1", "name": "bash"}, {"id": "c2", "name": "view_file"}]
    assert len(str(reply["reasoning"])) <= 500  # 思考内容只发截断后的摘要
    first = got[1].data
    assert first["tool"] == "bash" and first["step"] == 1
    assert first["args"] == {"command": "ls", "api_key": "[REDACTED]"}


@pytest.mark.unit
async def test_a_final_reply_also_emits_the_legacy_node_finished_with_content(
    make_state: MakeState,
) -> None:
    hub = EventHub()
    msg = Message(role="assistant", content="答案是 4")
    await events_for(hub).model_replied(msg, make_state(messages=(msg,)))
    got = await history(hub)
    assert names(got) == ["model.reply", "node.finished"]
    node = got[1].data
    assert node["decision"] == "reply" and node["content"] == "答案是 4"
    assert node["should_terminate"] is False


@pytest.mark.unit
async def test_turn_finished_emits_results_then_turn_and_legacy_node(make_state: MakeState) -> None:
    hub = EventHub()
    ev = events_for(hub)
    await ev.emit("tool.observed", {"call_id": "c1", "ok": False, "artifact_id": "obs_1"})
    body = "boom canary-abc sk-abcdefghijklmnop1234 " + "x" * 800
    tool = Message(role="tool", content=body, tool_call_id="c1", name="bash")
    ok_tool = Message(role="tool", content="fine", tool_call_id="c2", name="view_file")
    await ev.turn_finished({"messages": [tool, ok_tool]}, make_state())
    got = await history(hub)
    assert names(got) == ["tool.result", "tool.result", "turn.finished", "node.finished"]
    bad, good = got[0].data, got[1].data
    assert bad["ok"] is False and bad["artifact_id"] == "obs_1" and bad["call_id"] == "c1"
    assert len(bad["result"]) <= 500 and "canary-abc" not in bad["result"]  # type: ignore[operator]
    assert "sk-abcdefghijklmnop1234" not in bad["result"]  # type: ignore[operator]
    assert good["ok"] is True and good["artifact_id"] == ""
    turn = got[2].data
    assert (turn["turn"], turn["tool_count"], turn["failed_count"]) == (1, 2, 1)
    assert got[3].data["node"] == "tool_runner" and got[3].data["decision"] == "executed"


@pytest.mark.unit
async def test_turn_ordinals_are_monotonic_and_continue_after_a_resume(
    make_state: MakeState,
) -> None:
    hub = EventHub()
    ev = events_for(hub, turn_offset=7)
    await ev.turn_finished({"messages": []}, make_state())
    await ev.turn_finished({"messages": []}, make_state())
    assert [e.data["turn"] for e in await history(hub) if e.name == "turn.finished"] == [8, 9]


@pytest.mark.unit
async def test_a_plan_update_emits_plan_updated_and_the_legacy_plan(make_state: MakeState) -> None:
    hub = EventHub()
    plan = (PlanItem(id=1, title="读代码", status="in_progress"),)
    await events_for(hub).turn_finished({"messages": [], "plan": plan}, make_state(plan=plan))
    got = {e.name: e for e in await history(hub)}
    assert got["plan.updated"].data["plan"] == [
        {"id": 1, "title": "读代码", "status": "in_progress"}
    ]
    legacy = got["plan"].data["milestones"][0]  # type: ignore[index]
    assert legacy["title"] == "读代码" and legacy["status"] == "in_progress"


@pytest.mark.unit
async def test_observed_marker_is_not_published_as_an_event() -> None:
    hub = EventHub()
    await events_for(hub).emit("tool.observed", {"call_id": "c1", "ok": True})
    assert await history(hub) == []


@pytest.mark.unit
async def test_other_wrapper_emits_pass_through_scrubbed() -> None:
    hub = EventHub()
    await events_for(hub).emit("guard.error_notice", {"consecutive_errors": 3, "token": "zzz"})
    (ev,) = await history(hub)
    assert ev.name == "guard.error_notice" and ev.data["token"] == "[REDACTED]"


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
async def test_started_and_finished_carry_status_turns_usage_and_delivery(
    make_state: MakeState,
) -> None:
    hub = EventHub()
    life = LifecycleEvents(hub, REDACTOR, no_state, usage=lambda task_id: 900)
    state = make_state(
        messages=(
            Message(role="assistant", content="调用中", tool_calls=(CALL,)),
            Message(role="tool", content="ok", tool_call_id="c1", name="bash"),
            Message(role="assistant", content="结论：canary-abc 不应出现"),
        ),
    )
    await life.task_started("t1", state)
    await life.task_finished(finished(state))
    started, done = await history(hub)
    assert started.name == "task.started" and started.data["task_goal"] == "fix the bug"
    assert done.name == "task.finished"
    assert done.data["status"] == "succeeded" and done.data["turns"] == 2
    assert done.data["total_tokens"] == 900  # 统计值
    assert done.data["delivery"] == "结论：[REDACTED] 不应出现"


@pytest.mark.unit
async def test_failed_task_emits_error_before_the_final_event(make_state: MakeState) -> None:
    hub = EventHub()

    async def load(_: str) -> AgentState | None:
        return make_state(messages=(Message(role="assistant", content="x"),))

    await LifecycleEvents(hub, REDACTOR, load).task_finished(
        finished(None, "failed", "RuntimeError: boom")
    )
    error, done = await history(hub)
    assert (error.name, error.data["code"]) == ("task.error", "TASK_FAILED")
    assert done.name == "task.finished" and done.data["turns"] == 1
    assert done.seq > error.seq


@pytest.mark.unit
async def test_delivery_without_state_is_empty() -> None:
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
@pytest.mark.parametrize("status", ["terminated", "cancelled"])
async def test_an_unfinished_task_delivers_what_was_achieved_not_an_internal_directive(
    make_state: MakeState, status: str
) -> None:
    edit = ToolCall(id="c2", name="edit_file", args={"path": "src/app.py"})
    state = make_state(
        plan=(
            PlanItem(id=1, title="定位问题", status="completed"),
            PlanItem(id=2, title="修复并测试", status="in_progress"),
        ),
        facts=("bug 在 parse()",),
        messages=(
            Message(role="user", content="修 bug"),
            Message(role="assistant", content="", tool_calls=(edit,)),
            Message(role="tool", content="ok", tool_call_id="c2", name="edit_file"),
            Message(role="assistant", content="继续推进当前里程碑。"),
        ),
    )
    text = delivery_of(state, status, "用户取消")
    assert "任务没有完成：用户取消" in text
    assert "- 定位问题" in text and "未完成：\n- 修复并测试" in text
    assert "已修改的文件：src/app.py" in text and "bug 在 parse()" in text
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
        messages=(
            Message(role="user", content="写"),
            edit("c1", "done.py"),
            seen("c1", "已写入 done.py（3 字符）"),
            edit("c2", "refused.py"),
            seen("c2", "已被人工审核拒绝：不允许。改用合规方案"),
            edit("c3", "blocked.py"),
            seen("c3", "该调用与最近的调用重复，已拦截。请换一种做法"),
            edit("c4", "bad_args.py"),
            seen("c4", "未执行：该调用的参数无法使用"),
        ),
    )
    text = delivery_of(state, "cancelled", "用户取消")
    assert "已修改的文件：done.py" in text
    assert "refused.py" not in text and "blocked.py" not in text and "bad_args.py" not in text


@pytest.mark.unit
@pytest.mark.security
async def test_a_security_termination_delivers_no_content(make_state: MakeState) -> None:
    state = make_state(facts=("秘密事实",))
    text = delivery_of(state, "terminated", "[SECURITY] Prompt leak detected via canary token")
    assert "安全" in text and "秘密事实" not in text


# ------------------------------------------------------------------ 与循环联用


@pytest.mark.unit
async def test_events_follow_commits_and_a_suspend_is_not_a_terminal(
    tmp_path: Any, make_state: MakeState
) -> None:
    from agent_runtime.checkpoint import CheckpointStore
    from agent_runtime.core import apply_update

    hub = EventHub()
    async with CheckpointStore(tmp_path / "s.db") as store:
        world = ScriptedWorld(store)
        world.needs_approval = True
        world.events = events_for(hub)
        state = apply_update(make_state(), {"messages": [Message(role="user", content="g")]})
        first = await run(await world.build(state), state)
        assert first.status == "waiting_for_approval"
        got = await history(hub)
        assert names(got)[-1] == "task.waiting_for_approval"
        assert names(got)[:2] == ["model.reply", "tool.call"]

        world.needs_approval = False
        resumed = apply_update(first.state, {"pending_approval": None})
        await run(await world.build(resumed), resumed)
        final = await history(hub)
        assert [e.seq for e in final] == list(range(1, len(final) + 1))  # 续跑后序号接着走
    _ = (ASSISTANT, FINAL, TaskRuntime)


# ------------------------------------------------------------------ llm.call


def _record(**kw: Any):  # type: ignore[no-untyped-def]
    from agent_runtime.llm import CallRecord, TokenUsage

    base: dict[str, Any] = {
        "purpose": "agent",
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
    assert first.name == "llm.call" and first.data["node"] == "agent"
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
    calls(_record(purpose="agent"))  # 回调是同步的，事件还没发出
    await life.task_finished(finished(make_state(), "succeeded"))
    assert names(await history(hub)) == ["llm.call", "task.finished"]


@pytest.mark.unit
async def test_the_llm_call_tally_counts_tokens_per_task() -> None:
    from agent_runtime.core import current_task_id
    from agent_runtime.runtime import LlmCallEvents

    calls = LlmCallEvents(EventHub())
    current_task_id.set("t1")
    calls(_record())
    calls(_record())
    await calls.drain()
    assert calls.tokens_of("t1") == 200 and calls.tokens_of("other") == 0


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
        original = world.model

        async def spying(state: AgentState) -> Message:
            seen.append(current_task_id.get())
            return await original(state)

        world.model = spying  # type: ignore[method-assign]
        deps = RunnerDeps(store, world.build, lambda n: False, lambda s, t: "canary-abcdef")
        runner = TaskRunner(deps, new_task_id=lambda: "task-9")
        task_id = await runner.submit(TaskRequest("w", "/w", "s", "goal", "workspace_write"))
        await runner.wait(task_id)
    assert seen and set(seen) == {"task-9"}

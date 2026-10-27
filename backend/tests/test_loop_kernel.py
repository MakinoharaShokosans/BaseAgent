"""循环内核：调模型、执行工具、追加结果；没有工具调用即结束。出处：docs/02_runtime/02_main_loop.md。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.checkpoint import CheckpointStore
from agent_runtime.core import (
    AgentState,
    Message,
    ToolCall,
    apply_update,
    check_invariants,
)
from agent_runtime.loop import (
    LoopAborted,
    TaskOutcome,
    TaskRuntime,
    run,
)
from loop_support import ASSISTANT, FINAL, NullSink, ScriptedWorld

MakeState = Callable[..., AgentState]


class Recorder:
    """记下事件与提交的先后，验证“先提交后发事件”。"""

    def __init__(self, store: CheckpointStore) -> None:
        self.log: list[str] = []
        self._store = store

    async def commit(self, state: AgentState) -> int:
        self.log.append(f"commit:{len(state.messages)}")
        return await self._store.commit(state)

    async def model_replied(self, message: Message, state: AgentState) -> None:
        self.log.append(f"event:reply:{len(state.messages)}")

    async def turn_finished(self, update: Mapping[str, object], state: AgentState) -> None:
        self.log.append(f"event:turn:{len(state.messages)}")

    async def approval_requested(self, request: Any, state: AgentState) -> None:
        self.log.append("event:approval")


@pytest.fixture
async def world(tmp_path: Path):  # type: ignore[no-untyped-def]
    async with CheckpointStore(tmp_path / "state.db") as store:
        yield ScriptedWorld(store)


def start(make_state: MakeState) -> AgentState:
    return apply_update(make_state(), {"messages": [Message(role="user", content="fix the bug")]})


@pytest.mark.unit
async def test_a_reply_without_tool_calls_ends_the_task(
    world: ScriptedWorld, make_state: MakeState
) -> None:
    world.uses_tools = False
    outcome = await run(await world.build(start(make_state)), start(make_state))
    assert outcome.status == "succeeded"
    assert outcome.state.messages[-1] == FINAL


@pytest.mark.unit
async def test_tool_calls_run_and_the_loop_continues(
    world: ScriptedWorld, make_state: MakeState
) -> None:
    outcome = await run(await world.build(start(make_state)), start(make_state))
    assert [m.role for m in outcome.state.messages] == ["user", "assistant", "tool", "assistant"]
    check_invariants(outcome.state)


@pytest.mark.unit
async def test_commit_happens_before_each_event(
    world: ScriptedWorld, make_state: MakeState
) -> None:
    rec = Recorder(world.store)
    runtime = TaskRuntime(model=world.model, tools=world.tools, checkpoints=rec, events=rec)  # type: ignore[arg-type]
    await run(runtime, start(make_state))
    assert rec.log == [
        "commit:2",
        "event:reply:2",  # 模型回复带工具调用：先提交
        "commit:3",
        "event:turn:3",  # 工具结果追加后：再提交
        "commit:4",
        "event:reply:4",  # 终回复
    ]


@pytest.mark.unit
async def test_there_is_no_step_cap(world: ScriptedWorld, make_state: MakeState) -> None:
    """没有预算，也没有步数上限：循环只在模型不再调用工具时结束。"""
    turns = 0

    async def model(state: AgentState) -> Message:
        nonlocal turns
        turns += 1
        return (
            Message(
                role="assistant",
                content="",
                tool_calls=(ToolCall(id=f"c{turns}", name="view_file", args={"n": turns}),),
            )
            if turns <= 60
            else FINAL
        )

    runtime = TaskRuntime(
        model=model, tools=world.tools, checkpoints=world.store, events=NullSink()
    )  # type: ignore[arg-type]
    outcome = await run(runtime, start(make_state))
    assert turns == 61 and outcome.status == "succeeded"


@pytest.mark.unit
async def test_a_canary_leak_terminates_without_touching_the_state(
    world: ScriptedWorld, make_state: MakeState
) -> None:
    world.leak = True
    state = start(make_state)
    outcome = await run(await world.build(state), state)
    assert outcome.status == "terminated" and "Prompt leak" in outcome.termination_reason
    assert outcome.state == state  # 带金丝雀的回复不得进入状态


@pytest.mark.unit
async def test_model_errors_propagate_and_leave_the_last_checkpoint(
    world: ScriptedWorld, make_state: MakeState
) -> None:
    """余额不足与其他模型错误同一路径：异常传出，状态停在上一次提交点。"""
    world.fail_model = RuntimeError("402 insufficient balance")
    state = start(make_state)
    await world.store.commit(state)
    with pytest.raises(RuntimeError, match="insufficient"):
        await run(await world.build(state), state)
    assert await world.store.load("t1") == state


@pytest.mark.unit
async def test_approval_suspends_with_the_batch_unanswered(
    world: ScriptedWorld, make_state: MakeState
) -> None:
    world.needs_approval = True
    outcome = await run(await world.build(start(make_state)), start(make_state))
    assert outcome.status == "waiting_for_approval"
    assert outcome.state.pending_approval is not None
    assert outcome.state.messages[-1] == ASSISTANT  # 本批没有任何结果被应用
    stored = await world.store.load("t1")
    assert stored == outcome.state


@pytest.mark.unit
async def test_resume_executes_the_unanswered_batch_before_asking_the_model(
    world: ScriptedWorld, make_state: MakeState
) -> None:
    asked_with: list[int] = []
    inner = world.model

    async def model(state: AgentState) -> Message:
        asked_with.append(len(state.messages))
        return await inner(state)

    state = apply_update(start(make_state), {"messages": [ASSISTANT]})
    runtime = TaskRuntime(
        model=model, tools=world.tools, checkpoints=world.store, events=NullSink()
    )  # type: ignore[arg-type]
    outcome = await run(runtime, state)
    assert asked_with == [3]  # user, assistant, tool 之后才调模型
    assert outcome.state.messages[2].role == "tool"


@pytest.mark.unit
async def test_repeated_blocks_abort_the_task(world: ScriptedWorld, make_state: MakeState) -> None:
    async def tools(state: AgentState, calls: Sequence[ToolCall]) -> Mapping[str, object]:
        return {
            "messages": [
                Message(role="tool", content="已拦截", tool_call_id=c.id, name=c.name)
                for c in calls
            ],
            "consecutive_blocks": state.consecutive_blocks + 1,
        }

    async def model(state: AgentState) -> Message:
        return Message(
            role="assistant",
            content="",
            tool_calls=(ToolCall(id=f"c{len(state.messages)}", name="bash", args={}),),
        )

    runtime = TaskRuntime(
        model=model,
        tools=tools,
        checkpoints=world.store,
        events=NullSink(),
        max_consecutive_blocks=3,  # type: ignore[arg-type]
    )
    with pytest.raises(LoopAborted):
        await run(runtime, start(make_state))
    stored = await world.store.load("t1")
    assert stored is not None and stored.consecutive_blocks == 3  # 回合已提交，可续跑


@pytest.mark.unit
async def test_cancellation_propagates(world: ScriptedWorld, make_state: MakeState) -> None:
    world.gate = asyncio.Event()
    task = asyncio.create_task(run(await world.build(start(make_state)), start(make_state)))
    await world.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.unit
async def test_event_failures_do_not_fail_the_task(
    world: ScriptedWorld, make_state: MakeState
) -> None:
    class Exploding(NullSink):
        async def model_replied(self, message: Message, state: AgentState) -> None:
            raise RuntimeError("sink down")

    runtime = TaskRuntime(
        model=world.model, tools=world.tools, checkpoints=world.store, events=Exploding()
    )  # type: ignore[arg-type]
    outcome = await run(runtime, start(make_state))
    assert isinstance(outcome, TaskOutcome) and outcome.status == "succeeded"

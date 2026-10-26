"""派发前标记的崩溃恢复。出处：docs/02_runtime/03_checkpoint_and_resume.md §5。"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_runtime.checkpoint import recover_interrupted_dispatch
from agent_runtime.core import (
    AgentState,
    Message,
    PendingApproval,
    ToolCall,
    apply_update,
    check_invariants,
    new_state,
)

MakeState = Callable[..., AgentState]

IDEMPOTENT = {"view_file", "grep"}


def is_idempotent(name: str) -> bool:
    return name in IDEMPOTENT


def calls(*specs: tuple[str, str]) -> Message:
    return Message(
        role="assistant",
        content="",
        tool_calls=tuple(ToolCall(id=i, name=n, args={}) for i, n in specs),
    )


def crashed(make_state: MakeState, assistant: Message, started: list[str]) -> AgentState:
    return apply_update(
        make_state(),
        {
            "messages": [Message(role="user", content="go"), assistant],
            "phase": "tool_runner",
            "dispatch_started": started,
            "step_count": 1,
        },
    )


def tool_messages(state: AgentState) -> dict[str, Message]:
    return {m.tool_call_id: m for m in state.messages if m.role == "tool" and m.tool_call_id}


# ==============================================================================
# 不需要处理的情形
# ==============================================================================


@pytest.mark.unit
def test_nothing_to_do_without_a_marker(make_state: MakeState) -> None:
    state = apply_update(make_state(), {"phase": "tool_runner"})
    result = recover_interrupted_dispatch(state, is_idempotent)
    assert result.action == "none"
    assert result.state is state


@pytest.mark.unit
def test_marker_outside_tool_runner_is_ignored(make_state: MakeState) -> None:
    state = apply_update(
        make_state(),
        {"messages": [calls(("c1", "bash"))], "phase": "planner", "dispatch_started": ["c1"]},
    )
    assert recover_interrupted_dispatch(state, is_idempotent).action == "none"


@pytest.mark.unit
def test_suspended_approval_is_not_a_dispatch_crash(make_state: MakeState) -> None:
    pending = PendingApproval(
        approval_id="appr_1",
        call_ids=("c1",),
        required_level="full_permissions",
        current_level="read_only",
        action_type="network",
        command="curl x",
        reason="r",
        escalation_count=1,
    )
    state = apply_update(
        make_state(),
        {
            "messages": [calls(("c1", "bash"))],
            "phase": "tool_runner",
            "dispatch_started": ["c1"],
            "pending_approval": pending,
        },
    )
    assert recover_interrupted_dispatch(state, is_idempotent).action == "none"


# ==============================================================================
# 合成“执行结果未知”
# ==============================================================================


@pytest.mark.unit
def test_non_idempotent_call_gets_an_unknown_result(make_state: MakeState) -> None:
    state = crashed(make_state, calls(("c1", "bash")), ["c1"])
    result = recover_interrupted_dispatch(state, is_idempotent)

    assert result.action == "synthesized"
    message = tool_messages(result.state)["c1"]
    assert message.name == "bash"
    assert "执行结果未知" in message.content
    assert "核验" in message.content
    assert result.state.phase == "planner"
    assert result.state.dispatch_started == ()
    assert result.synthesized_ids == ("c1",)


@pytest.mark.unit
def test_recovered_state_satisfies_the_invariants(make_state: MakeState) -> None:
    result = recover_interrupted_dispatch(
        crashed(make_state, calls(("c1", "bash")), ["c1"]), is_idempotent
    )
    check_invariants(result.state)


@pytest.mark.unit
def test_every_call_of_the_batch_is_answered_in_order(make_state: MakeState) -> None:
    assistant = calls(("a", "bash"), ("b", "view_file"), ("c", "write_file"))
    result = recover_interrupted_dispatch(crashed(make_state, assistant, ["a", "b"]), is_idempotent)

    tail = result.state.messages[-3:]
    assert [m.tool_call_id for m in tail] == ["a", "b", "c"]
    assert "执行结果未知" in tail[0].content  # 派发过、非幂等
    assert "执行结果未知" not in tail[1].content  # 派发过但幂等：没有副作用风险
    assert "执行结果未知" not in tail[2].content  # 根本没派发
    assert "未执行" in tail[1].content
    assert "未执行" in tail[2].content
    check_invariants(result.state)


@pytest.mark.unit
def test_earlier_messages_are_untouched(make_state: MakeState) -> None:
    assistant = Message(
        role="assistant",
        content="plan",
        tool_calls=(ToolCall(id="c1", name="bash", args={"command": "ls"}),),
        reasoning="think",
        reasoning_state={"sig": "AB+/=="},
        produced_by="ep",
    )
    state = crashed(make_state, assistant, ["c1"])
    result = recover_interrupted_dispatch(state, is_idempotent)
    assert result.state.messages[: len(state.messages)] == state.messages
    assert result.state.messages[-2].reasoning_state == {"sig": "AB+/=="}


# ==============================================================================
# 幂等工具直接重试
# ==============================================================================


@pytest.mark.unit
def test_only_idempotent_calls_started_means_retry(make_state: MakeState) -> None:
    assistant = calls(("a", "view_file"), ("b", "grep"))
    state = crashed(make_state, assistant, ["a", "b"])
    result = recover_interrupted_dispatch(state, is_idempotent)

    assert result.action == "retry"
    assert result.state.phase == "tool_runner"  # 原样重做该阶段
    assert result.state.dispatch_started == ()
    assert result.state.messages == state.messages  # 不合成任何消息
    assert result.synthesized_ids == ()


@pytest.mark.unit
def test_unknown_tools_are_treated_as_non_idempotent(make_state: MakeState) -> None:
    state = crashed(make_state, calls(("c1", "mystery_mcp_tool")), ["c1"])
    assert recover_interrupted_dispatch(state, is_idempotent).action == "synthesized"


@pytest.mark.unit
def test_recovery_is_idempotent_itself(make_state: MakeState) -> None:
    once = recover_interrupted_dispatch(
        crashed(make_state, calls(("c1", "bash")), ["c1"]), is_idempotent
    )
    twice = recover_interrupted_dispatch(once.state, is_idempotent)
    assert twice.action == "none"
    assert twice.state == once.state


@pytest.mark.unit
def test_the_input_state_is_not_modified(make_state: MakeState) -> None:
    state = crashed(make_state, calls(("c1", "bash")), ["c1"])
    before = state.messages
    recover_interrupted_dispatch(state, is_idempotent)
    assert state.messages == before
    assert state.dispatch_started == ("c1",)


# ==============================================================================
# 性质
# ==============================================================================


@st.composite
def batches(draw: st.DrawFn) -> tuple[list[tuple[str, str]], list[str], set[str]]:
    count = draw(st.integers(1, 5))
    names = draw(
        st.lists(
            st.sampled_from(["bash", "view_file", "grep", "write_file"]),
            min_size=count,
            max_size=count,
        )
    )
    specs = [(f"c{i}", n) for i, n in enumerate(names)]
    started = draw(st.lists(st.sampled_from([s[0] for s in specs]), min_size=1, unique=True))
    idempotent = draw(st.sets(st.sampled_from(["bash", "view_file", "grep", "write_file"])))
    return specs, started, idempotent


@pytest.mark.unit
@given(batches())
def test_any_recovered_state_is_consistent(
    batch: tuple[list[tuple[str, str]], list[str], set[str]],
) -> None:
    specs, started, idempotent = batch
    base = new_state(
        workspace_id="w", workspace_path="/p", session_id="s", task_id="t",
        task_goal="g", permission_level="read_only", canary_token="c",
    )  # fmt: skip
    state = apply_update(
        base,
        {
            "messages": [calls(*specs)],
            "phase": "tool_runner",
            "dispatch_started": started,
        },
    )
    result = recover_interrupted_dispatch(state, lambda n: n in idempotent)
    check_invariants(result.state)
    assert result.state.dispatch_started == ()
    if result.action == "synthesized":
        answered = [m.tool_call_id for m in result.state.messages if m.role == "tool"]
        assert answered == [c for c, _ in specs]
        unknown = {c for c, n in specs if c in started and n not in idempotent}
        assert set(result.synthesized_ids) == {c for c, _ in specs}
        assert unknown  # 只有存在非幂等的已派发调用才会合成
    else:
        assert result.action == "retry"
        assert all(n in idempotent for c, n in specs if c in started)


# ==============================================================================
# 取消的终态
# ==============================================================================


@pytest.mark.unit
def test_cancelled_state_is_terminal_and_clears_every_pending_thing(make_state: MakeState) -> None:
    from agent_runtime.checkpoint import CANCELLED_REASON, mark_cancelled

    state = apply_update(
        crashed(make_state, calls(("c1", "bash")), ["c1"]),
        {"approval_decision": None},
    )
    cancelled = mark_cancelled(state)
    assert cancelled.phase == "done"
    assert cancelled.should_terminate is True
    assert cancelled.termination_reason == CANCELLED_REASON
    assert cancelled.dispatch_started == ()
    assert cancelled.pending_approval is None
    check_invariants(cancelled)
    assert "未执行" in tool_messages(cancelled)["c1"].content


@pytest.mark.unit
def test_cancel_only_fills_in_the_missing_results(make_state: MakeState) -> None:
    from agent_runtime.checkpoint import mark_cancelled

    state = apply_update(
        make_state(),
        {
            "messages": [
                calls(("a", "bash"), ("b", "bash")),
                Message(role="tool", content="real result", tool_call_id="a", name="bash"),
            ],
            "phase": "tool_runner",
        },
    )
    cancelled = mark_cancelled(state)
    results = tool_messages(cancelled)
    assert results["a"].content == "real result"  # 已有的结果不被覆盖
    assert "取消" in results["b"].content
    check_invariants(cancelled)


@pytest.mark.unit
def test_cancelling_a_finished_state_changes_nothing(make_state: MakeState) -> None:
    from agent_runtime.checkpoint import mark_cancelled

    finished = apply_update(make_state(), {"phase": "done"})
    assert mark_cancelled(finished) is finished


@pytest.mark.unit
def test_cancel_with_no_tool_calls_just_closes_the_task(make_state: MakeState) -> None:
    from agent_runtime.checkpoint import mark_cancelled

    state = apply_update(make_state(), {"messages": [Message(role="user", content="go")]})
    cancelled = mark_cancelled(state)
    assert cancelled.messages == state.messages
    assert cancelled.phase == "done"

"""消息类型：字段约束、原子对不变量。出处：docs/02_runtime/01_state_model.md §1。"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from agent_runtime.core import (
    AtomicPairError,
    InvalidMessageError,
    Message,
    ToolCall,
    assert_atomic_pairs,
)

CALL_A = ToolCall(id="a", name="view_file", args={"path": "x"})
CALL_B = ToolCall(id="b", name="grep", args={"pattern": "y"})


def _assistant(*calls: ToolCall) -> Message:
    return Message(role="assistant", content="", tool_calls=calls)


def _tool(call_id: str, name: str = "t", content: str = "ok") -> Message:
    return Message(role="tool", content=content, tool_call_id=call_id, name=name)


# ==============================================================================
# 字段约束
# ==============================================================================


@pytest.mark.unit
def test_messages_are_immutable() -> None:
    message = Message(role="user", content="hi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        message.content = "changed"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        CALL_A.name = "x"  # type: ignore[misc]


@pytest.mark.unit
def test_plain_messages_construct() -> None:
    assert Message(role="system", content="s").tool_calls == ()
    assert Message(role="user", content="u").reasoning is None
    assert _assistant(CALL_A).tool_calls == (CALL_A,)
    assert _tool("a").tool_call_id == "a"


@pytest.mark.unit
def test_unknown_role_is_rejected() -> None:
    with pytest.raises(InvalidMessageError, match="role"):
        Message(role="function", content="x")  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize("role", ["system", "user", "tool"])
def test_only_assistant_may_carry_tool_calls_or_reasoning(role: str) -> None:
    extra: dict[str, Any] = {"tool_call_id": "a", "name": "t"} if role == "tool" else {}
    for field, value in [
        ("tool_calls", (CALL_A,)),
        ("reasoning", "thoughts"),
        ("reasoning_state", {"sig": "x"}),
        ("produced_by", "deepseek-pro"),
    ]:
        with pytest.raises(InvalidMessageError, match=field):
            Message(role=role, content="x", **{**extra, field: value})  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize("role", ["system", "user", "assistant"])
def test_only_tool_messages_carry_tool_call_id_and_name(role: str) -> None:
    with pytest.raises(InvalidMessageError, match="tool_call_id"):
        Message(role=role, content="x", tool_call_id="a")  # type: ignore[arg-type]
    with pytest.raises(InvalidMessageError, match="name"):
        Message(role=role, content="x", name="t")  # type: ignore[arg-type]


@pytest.mark.unit
def test_tool_message_requires_a_call_id() -> None:
    with pytest.raises(InvalidMessageError, match="tool_call_id"):
        Message(role="tool", content="x")
    with pytest.raises(InvalidMessageError, match="tool_call_id"):
        Message(role="tool", content="x", tool_call_id="")


@pytest.mark.unit
def test_content_must_be_a_string() -> None:
    with pytest.raises(InvalidMessageError, match="content"):
        Message(role="user", content=None)  # type: ignore[arg-type]


@pytest.mark.unit
def test_tool_call_validation() -> None:
    with pytest.raises(InvalidMessageError, match="id"):
        ToolCall(id="", name="t", args={})
    with pytest.raises(InvalidMessageError, match="name"):
        ToolCall(id="a", name="", args={})
    with pytest.raises(InvalidMessageError, match="args"):
        ToolCall(id="a", name="t", args=[1])  # type: ignore[arg-type]


@pytest.mark.unit
def test_tool_call_ids_must_be_unique_within_a_message() -> None:
    with pytest.raises(InvalidMessageError, match="重复"):
        _assistant(CALL_A, ToolCall(id="a", name="other", args={}))


@pytest.mark.unit
def test_tool_calls_are_stored_as_a_tuple() -> None:
    message = Message(role="assistant", content="", tool_calls=[CALL_A])  # type: ignore[arg-type]
    assert isinstance(message.tool_calls, tuple)


@pytest.mark.unit
def test_args_are_copied_so_later_mutation_cannot_leak_in() -> None:
    source: dict[str, Any] = {"path": "x"}
    call = ToolCall(id="a", name="t", args=source)
    source["path"] = "changed"
    assert call.args["path"] == "x"


# ==============================================================================
# 原子对不变量
# ==============================================================================


@pytest.mark.unit
def test_complete_pairs_pass() -> None:
    assert_atomic_pairs(
        [
            Message(role="user", content="go"),
            _assistant(CALL_A, CALL_B),
            _tool("b"),  # 顺序不要求一致
            _tool("a"),
            Message(role="assistant", content="done"),
        ]
    )


@pytest.mark.unit
def test_empty_and_plain_conversations_pass() -> None:
    assert_atomic_pairs([])
    assert_atomic_pairs([Message(role="user", content="x"), Message(role="assistant", content="y")])


@pytest.mark.unit
def test_missing_tool_message_is_rejected() -> None:
    with pytest.raises(AtomicPairError, match="b"):
        assert_atomic_pairs(
            [_assistant(CALL_A, CALL_B), _tool("a"), Message(role="user", content="x")]
        )


@pytest.mark.unit
def test_extra_or_foreign_tool_message_is_rejected() -> None:
    with pytest.raises(AtomicPairError):
        assert_atomic_pairs([_assistant(CALL_A), _tool("a"), _tool("zzz")])
    with pytest.raises(AtomicPairError):
        assert_atomic_pairs([_assistant(CALL_A), _tool("zzz")])


@pytest.mark.unit
def test_duplicate_tool_message_is_rejected() -> None:
    with pytest.raises(AtomicPairError):
        assert_atomic_pairs([_assistant(CALL_A, CALL_B), _tool("a"), _tool("a")])


@pytest.mark.unit
def test_orphan_tool_message_is_rejected() -> None:
    with pytest.raises(AtomicPairError):
        assert_atomic_pairs([Message(role="user", content="x"), _tool("a")])


@pytest.mark.unit
def test_tool_messages_must_directly_follow_their_assistant() -> None:
    with pytest.raises(AtomicPairError):
        assert_atomic_pairs(
            [_assistant(CALL_A), Message(role="user", content="interject"), _tool("a")]
        )


@pytest.mark.unit
def test_pending_tail_is_tolerated_only_when_allowed() -> None:
    history = [Message(role="user", content="x"), _assistant(CALL_A, CALL_B)]
    with pytest.raises(AtomicPairError):
        assert_atomic_pairs(history)
    assert_atomic_pairs(history, allow_pending_tail=True)


@pytest.mark.unit
def test_pending_tail_allowance_does_not_cover_earlier_groups() -> None:
    broken_middle = [_assistant(CALL_A), Message(role="user", content="x"), _assistant(CALL_B)]
    with pytest.raises(AtomicPairError):
        assert_atomic_pairs(broken_middle, allow_pending_tail=True)


@pytest.mark.unit
def test_pending_tail_still_rejects_foreign_results() -> None:
    with pytest.raises(AtomicPairError):
        assert_atomic_pairs([_assistant(CALL_A), _tool("zzz")], allow_pending_tail=True)

"""状态编解码：严格、无损、可迁移。出处：docs/02_runtime/01_state_model.md §5。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_runtime.core import (
    CURRENT_SCHEMA_VERSION,
    AgentState,
    ApprovalDecision,
    FailedAttempt,
    Message,
    Milestone,
    PendingApproval,
    Role,
    StateDecodeError,
    ToolCall,
    apply_update,
    dumps_state,
    loads_state,
    new_state,
    state_from_dict,
    state_to_dict,
)

# ==============================================================================
# 生成器
# ==============================================================================

json_scalars = st.none() | st.booleans() | st.integers(-(2**53), 2**53) | st.text(max_size=20)
json_values = st.recursive(
    json_scalars,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=12,
)
json_objects = st.dictionaries(st.text(max_size=8), json_values, max_size=4)
text = st.text(max_size=40)
ident = st.text(min_size=1, max_size=12)


@st.composite
def tool_calls(draw: st.DrawFn) -> tuple[ToolCall, ...]:
    ids = draw(st.lists(ident, max_size=3, unique=True))
    return tuple(ToolCall(id=i, name=draw(ident), args=draw(json_objects)) for i in ids)


@st.composite
def messages(draw: st.DrawFn) -> Message:
    role: Role = draw(st.sampled_from(["system", "user", "assistant", "tool"]))
    if role == "assistant":
        return Message(
            role="assistant",
            content=draw(text),
            tool_calls=draw(tool_calls()),
            reasoning=draw(st.none() | text),
            reasoning_state=draw(st.none() | json_objects),
            produced_by=draw(st.none() | ident),
        )
    if role == "tool":
        return Message(role="tool", content=draw(text), tool_call_id=draw(ident), name=draw(ident))
    return Message(role=role, content=draw(text))


def base_state(**overrides: Any) -> AgentState:
    return new_state(
        **{
            "workspace_id": "w",
            "workspace_path": "/p",
            "session_id": "s",
            "task_id": "t",
            "task_goal": "g",
            "permission_level": "read_only",
            "canary_token": "c",
            **overrides,
        }
    )


# ==============================================================================
# 往返
# ==============================================================================


@pytest.mark.unit
@given(messages())
def test_message_round_trips(message: Message) -> None:
    assert Message.from_dict(message.to_dict()) == message


@pytest.mark.unit
@given(st.lists(messages(), max_size=6))
def test_state_round_trips_through_json(msgs: list[Message]) -> None:
    state = apply_update(base_state(), {"messages": msgs, "step_count": 4, "total_tokens": 99})
    assert loads_state(dumps_state(state)) == state


@pytest.mark.unit
@given(json_objects)
def test_reasoning_state_survives_byte_for_byte(payload: dict[str, Any]) -> None:
    """思考内容的不透明载荷：编码、解码、再编码，字节完全相同。"""
    message = Message(role="assistant", content="x", reasoning_state=payload)
    state = apply_update(base_state(), {"messages": [message]})
    first = dumps_state(state)
    second = dumps_state(loads_state(first))
    assert first == second
    restored = loads_state(first).messages[0]
    assert json.dumps(restored.reasoning_state) == json.dumps(payload)


@pytest.mark.unit
def test_reasoning_state_key_order_and_unicode_are_preserved() -> None:
    payload = {"z": 1, "a": {"终": "端", "b": [3, 2, 1]}, "sig": "AB+/=="}
    message = Message(role="assistant", content="", reasoning_state=payload)
    restored = loads_state(dumps_state(apply_update(base_state(), {"messages": [message]})))
    assert list(restored.messages[0].reasoning_state or {}) == ["z", "a", "sig"]
    assert restored.messages[0].reasoning_state == payload


@pytest.mark.unit
def test_every_field_round_trips() -> None:
    state = apply_update(
        base_state(rolling_summary="sum", confirmed_facts=["f"]),
        {
            "milestones": [Milestone(id=1, title="m", description="d", status="in_progress")],
            "current_milestone_idx": 0,
            "failed_attempts": [FailedAttempt(action="a", failure_reason="r", conclusion="c")],
            "artifacts": {"a1": "/x"},
            "fingerprint_history": ["h1", "h2"],
            "consecutive_errors": 2,
            "should_terminate": True,
            "termination_reason": "budget",
            "phase": "tool_runner",
            "pending_approval": PendingApproval(
                approval_id="appr_1",
                call_ids=("c1",),
                required_level="full_permissions",
                current_level="read_only",
                action_type="network",
                command="curl x",
                reason="r",
                escalation_count=1,
                related_actions=("pip install y",),
            ),
            "approval_decision": ApprovalDecision(approval_id="appr_1", approved=True),
            "dispatch_started": ["c1"],
        },
    )
    assert loads_state(dumps_state(state)) == state


@pytest.mark.unit
def test_encoding_is_deterministic() -> None:
    state = apply_update(base_state(), {"artifacts": {"b": "2", "a": "1"}})
    assert dumps_state(state) == dumps_state(state)
    assert dumps_state(state) == dumps_state(loads_state(dumps_state(state)))


# ==============================================================================
# 严格解码
# ==============================================================================


def _valid() -> dict[str, Any]:
    return state_to_dict(
        apply_update(base_state(), {"messages": [Message(role="user", content="x")]})
    )


@pytest.mark.unit
def test_schema_version_is_recorded() -> None:
    assert _valid()["schema_version"] == CURRENT_SCHEMA_VERSION == 1


@pytest.mark.unit
def test_unknown_role_is_rejected() -> None:
    data = _valid()
    data["messages"][0]["role"] = "function"
    with pytest.raises(StateDecodeError, match="role"):
        state_from_dict(data)


@pytest.mark.unit
@pytest.mark.parametrize(
    "field", ["task_id", "messages", "phase", "permission_level", "step_count"]
)
def test_missing_required_field_is_rejected(field: str) -> None:
    data = _valid()
    del data[field]
    with pytest.raises(StateDecodeError, match=field):
        state_from_dict(data)


@pytest.mark.unit
def test_unknown_top_level_field_is_rejected() -> None:
    data = _valid()
    data["surprise"] = 1
    with pytest.raises(StateDecodeError, match="surprise"):
        state_from_dict(data)


@pytest.mark.unit
def test_unknown_message_field_is_rejected() -> None:
    data = _valid()
    data["messages"][0]["extra"] = 1
    with pytest.raises(StateDecodeError, match="extra"):
        state_from_dict(data)


@pytest.mark.unit
def test_message_missing_optional_fields_still_loads() -> None:
    """旧检查点没有 reasoning 等字段，仍可加载。"""
    data = _valid()
    data["messages"] = [{"role": "assistant", "content": "hi"}]
    state = state_from_dict(data)
    assert state.messages[0].reasoning is None
    assert state.messages[0].produced_by is None


@pytest.mark.unit
def test_wrong_types_are_rejected() -> None:
    for field, bad in [
        ("step_count", "3"),
        ("messages", "x"),
        ("should_terminate", 1),
        ("artifacts", []),
    ]:
        data = _valid()
        data[field] = bad
        with pytest.raises(StateDecodeError, match=field):
            state_from_dict(data)


@pytest.mark.unit
def test_semantic_violations_surface_as_decode_errors() -> None:
    data = _valid()
    data["messages"] = [
        {"role": "user", "content": "x", "tool_calls": [{"id": "a", "name": "t", "args": {}}]}
    ]
    with pytest.raises(StateDecodeError, match="tool_calls"):
        state_from_dict(data)


@pytest.mark.unit
def test_non_object_and_bad_json_are_rejected() -> None:
    with pytest.raises(StateDecodeError):
        state_from_dict([])  # type: ignore[arg-type]
    with pytest.raises(StateDecodeError, match="JSON"):
        loads_state("{not json")
    with pytest.raises(StateDecodeError):
        loads_state("[1, 2]")


@pytest.mark.security
@pytest.mark.unit
def test_decoding_never_instantiates_types_by_name() -> None:
    """被篡改的检查点最坏只能导致解码失败，不能触发任意类型实例化。"""
    data = _valid()
    data["messages"][0]["__class__"] = "os.system"
    data["messages"][0]["content"] = {"__reduce__": ["os.system", "echo pwned"]}
    with pytest.raises(StateDecodeError):
        state_from_dict(data)


# ==============================================================================
# 版本与迁移
# ==============================================================================


@pytest.mark.unit
def test_newer_schema_version_is_rejected() -> None:
    data = _valid()
    data["schema_version"] = CURRENT_SCHEMA_VERSION + 1
    with pytest.raises(StateDecodeError, match="schema_version"):
        state_from_dict(data)


@pytest.mark.unit
@pytest.mark.parametrize("bad", [0, -1, "1", None, 1.0, True])
def test_invalid_schema_version_is_rejected(bad: object) -> None:
    data = _valid()
    data["schema_version"] = bad
    with pytest.raises(StateDecodeError, match="schema_version"):
        state_from_dict(data)


@pytest.mark.unit
def test_older_versions_are_migrated_step_by_step() -> None:
    """用注入的迁移表验证机制；真实迁移在出现第 2 版时才有。"""
    data = _valid()

    def v0_to_v1(d: dict[str, Any]) -> dict[str, Any]:
        d = dict(d)
        d["rolling_summary"] = d.pop("legacy_summary")
        return d

    data["schema_version"] = 0
    data["legacy_summary"] = "from v0"
    del data["rolling_summary"]
    state = state_from_dict(data, migrations={0: v0_to_v1}, current_version=1, minimum_version=0)
    assert state.rolling_summary == "from v0"
    assert state.schema_version == 1


@pytest.mark.unit
def test_missing_migration_step_is_an_error() -> None:
    data = _valid()
    data["schema_version"] = 0
    with pytest.raises(StateDecodeError, match="迁移"):
        state_from_dict(data, migrations={}, current_version=1, minimum_version=0)

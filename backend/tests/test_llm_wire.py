"""请求构造与响应解析（纯函数）。

出处：docs/07_llm_gateway/01_gateway.md §3-4、§8-10，及 04_deepseek.md。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from openai.types.chat import ChatCompletion

from aegis_config.models import Capabilities, Endpoint
from agent_runtime.core import Message, ToolCall, ToolSchema
from agent_runtime.llm import LLMRequest, estimate_tokens
from agent_runtime.llm.wire import build_request_kwargs, parse_completion


def endpoint(**caps: Any) -> Endpoint:
    capabilities = {
        "tools": True,
        "json_object": True,
        "usage_in_response": True,
        "thinking_control": "deepseek",
        "thinking_with_tools": False,
        "reasoning_field": "reasoning_content",
        "reasoning_passback": "never",
    }
    return Endpoint(
        name="deepseek-pro",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        api_key_env="DEEPSEEK_API_KEY",
        timeout_sec=60,
        context_window=100_000,
        max_output_tokens=4096,
        reasoning_effort=None,
        capabilities=Capabilities(**{**capabilities, **caps}),
    )


SEARCH = ToolSchema(
    name="grep", description="search", parameters={"type": "object", "properties": {}}
)
USER = Message(role="user", content="hi")


def req(*messages: Message, **kw: Any) -> LLMRequest:
    return LLMRequest(tier="fast", messages=messages or (USER,), **kw)


def build(ep: Endpoint | None = None, temperature: float = 0.2, **kw: Any) -> dict[str, Any]:
    messages = kw.pop("messages", (USER,))
    return build_request_kwargs(ep or endpoint(), temperature, req(*messages, **kw))


# ==============================================================================
# 消息转换
# ==============================================================================


@pytest.mark.unit
def test_basic_payload() -> None:
    out = build()
    assert out["model"] == "deepseek-v4-pro"
    assert out["messages"] == [{"role": "user", "content": "hi"}]
    assert out["temperature"] == 0.2
    assert "tools" not in out


@pytest.mark.unit
def test_system_message_must_come_first() -> None:
    system = Message(role="system", content="rules")
    assert build(messages=(system, USER))["messages"][0] == {"role": "system", "content": "rules"}
    with pytest.raises(ValueError, match="system"):
        build(messages=(USER, system))


@pytest.mark.unit
def test_assistant_tool_calls_use_null_content_and_json_arguments() -> None:
    call = ToolCall(id="c1", name="grep", args={"pattern": "终端", "n": 2})
    assistant = Message(role="assistant", content="", tool_calls=(call,))
    tool = Message(role="tool", content="3 hits", tool_call_id="c1", name="grep")
    messages = build(messages=(USER, assistant, tool))["messages"]

    sent = messages[1]
    assert sent["content"] is None
    assert sent["tool_calls"] == [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "grep", "arguments": '{"pattern": "终端", "n": 2}'},
        }
    ]
    assert messages[2] == {"role": "tool", "tool_call_id": "c1", "content": "3 hits"}


@pytest.mark.unit
def test_assistant_text_is_kept_next_to_tool_calls() -> None:
    call = ToolCall(id="c1", name="grep", args={})
    assistant = Message(role="assistant", content="looking", tool_calls=(call,))
    assert build(messages=(USER, assistant))["messages"][1]["content"] == "looking"


@pytest.mark.unit
def test_a_malformed_call_is_sent_back_with_its_original_text() -> None:
    call = ToolCall(id="c1", name="grep", args={"_raw": '{"pattern": "unterminated'})
    assistant = Message(role="assistant", content="", tool_calls=(call,))
    sent = build(messages=(USER, assistant))["messages"][1]
    assert sent["tool_calls"][0]["function"]["arguments"] == '{"pattern": "unterminated'


@pytest.mark.unit
def test_consecutive_user_messages_are_merged_only_when_asked() -> None:
    a, b = Message(role="user", content="first"), Message(role="user", content="second")
    assert len(build(messages=(a, b))["messages"]) == 2
    merged = build(endpoint(merge_consecutive_user=True), messages=(a, b))["messages"]
    assert merged == [{"role": "user", "content": "first\n\nsecond"}]


@pytest.mark.unit
def test_merging_does_not_cross_other_roles() -> None:
    a = Message(role="user", content="a")
    mid = Message(role="assistant", content="m")
    b = Message(role="user", content="b")
    out = build(endpoint(merge_consecutive_user=True), messages=(a, mid, b))["messages"]
    assert [m["role"] for m in out] == ["user", "assistant", "user"]


# ==============================================================================
# 工具、输出上限、温度
# ==============================================================================


@pytest.mark.unit
def test_tools_are_sent_as_function_schemas() -> None:
    out = build(tools=(SEARCH,))
    assert out["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "grep",
                "description": "search",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


@pytest.mark.unit
def test_max_tokens_is_always_explicit_and_capped_by_the_endpoint() -> None:
    assert build()["max_tokens"] == 4096
    assert build(max_output_tokens=500)["max_tokens"] == 500
    assert build(max_output_tokens=99_999)["max_tokens"] == 4096


# ==============================================================================
# 思考开关与强度
# ==============================================================================


@pytest.mark.unit
def test_deepseek_thinking_is_explicit_and_on_without_tools() -> None:
    out = build()
    assert out["extra_body"] == {"thinking": {"type": "enabled"}}


@pytest.mark.unit
def test_tools_turn_thinking_off_when_the_endpoint_cannot_pass_it_back() -> None:
    out = build(tools=(SEARCH,), reasoning_effort="high")
    assert out["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in out  # 关着思考时不发强度


@pytest.mark.unit
def test_tools_keep_thinking_on_when_the_endpoint_allows_it() -> None:
    out = build(endpoint(thinking_with_tools=True), tools=(SEARCH,))
    assert out["extra_body"] == {"thinking": {"type": "enabled"}}


@pytest.mark.unit
def test_no_thinking_field_without_the_deepseek_dialect() -> None:
    out = build(endpoint(thinking_control="none"))
    assert "extra_body" not in out


@pytest.mark.unit
@pytest.mark.parametrize(
    ("asked", "sent"),
    [("low", "low"), ("medium", "high"), ("high", "high"), ("max", "max")],
)
def test_reasoning_effort_maps_onto_deepseek_levels(asked: str, sent: str) -> None:
    assert build(reasoning_effort=asked)["reasoning_effort"] == sent


@pytest.mark.unit
def test_endpoint_default_effort_applies_when_the_request_gives_none() -> None:
    ep = endpoint().model_copy(update={"reasoning_effort": "max"})
    assert build(ep)["reasoning_effort"] == "max"
    assert build(ep, reasoning_effort="low")["reasoning_effort"] == "low"


@pytest.mark.unit
def test_no_effort_is_sent_when_nobody_asked() -> None:
    assert "reasoning_effort" not in build()


# ==============================================================================
# 思考内容回传
# ==============================================================================


def history_with_reasoning(produced_by: str = "deepseek-pro") -> tuple[Message, ...]:
    def assistant(text: str, reasoning: str | None, calls: tuple[ToolCall, ...] = ()) -> Message:
        return Message(
            role="assistant",
            content=text,
            tool_calls=calls,
            reasoning=reasoning,
            produced_by=produced_by if reasoning else None,
        )

    call = ToolCall(id="c1", name="grep", args={})
    return (
        Message(role="user", content="q1"),
        assistant("a1", "old thinking"),
        Message(role="user", content="q2"),
        assistant("", "tail thinking", (call,)),
        Message(role="tool", content="ok", tool_call_id="c1", name="grep"),
    )


def reasoning_fields(out: dict[str, Any]) -> list[str | None]:
    return [m.get("reasoning_content") for m in out["messages"] if m["role"] == "assistant"]


@pytest.mark.unit
def test_never_drops_all_reasoning() -> None:
    out = build(messages=history_with_reasoning(), tools=(SEARCH,))
    assert reasoning_fields(out) == [None, None]


@pytest.mark.unit
def test_tool_turns_passes_back_only_the_tail_round_trip() -> None:
    ep = endpoint(reasoning_passback="tool_turns")
    out = build(ep, messages=history_with_reasoning(), tools=(SEARCH,))
    assert reasoning_fields(out) == [None, "tail thinking"]


@pytest.mark.unit
def test_all_when_tools_passes_everything_but_only_with_tools() -> None:
    ep = endpoint(reasoning_passback="all_when_tools", thinking_with_tools=True)
    with_tools = build(ep, messages=history_with_reasoning(), tools=(SEARCH,))
    assert reasoning_fields(with_tools) == ["old thinking", "tail thinking"]
    without = build(ep, messages=history_with_reasoning())
    assert reasoning_fields(without) == [None, None]


@pytest.mark.unit
def test_reasoning_from_another_endpoint_is_never_passed_back() -> None:
    ep = endpoint(reasoning_passback="tool_turns")
    out = build(ep, messages=history_with_reasoning(produced_by="someone-else"), tools=(SEARCH,))
    assert reasoning_fields(out) == [None, None]


@pytest.mark.unit
def test_passback_needs_a_reasoning_field_name() -> None:
    ep = endpoint(reasoning_passback="tool_turns", reasoning_field=None)
    out = build(ep, messages=history_with_reasoning(), tools=(SEARCH,))
    assert reasoning_fields(out) == [None, None]


@pytest.mark.unit
def test_the_passback_field_name_comes_from_the_endpoint() -> None:
    ep = endpoint(reasoning_passback="tool_turns", reasoning_field="thoughts")
    out = build(ep, messages=history_with_reasoning(), tools=(SEARCH,))
    assert out["messages"][3]["thoughts"] == "tail thinking"


@pytest.mark.unit
def test_opaque_passback_is_not_implemented_yet() -> None:
    ep = endpoint(reasoning_passback="opaque")
    with pytest.raises(NotImplementedError, match="opaque"):
        build(ep, messages=history_with_reasoning())


# ==============================================================================
# 响应解析
# ==============================================================================


def completion(message: dict[str, Any], finish: str = "stop", **extra: Any) -> ChatCompletion:
    body: dict[str, Any] = {
        "id": "x",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-v4-pro",
        "choices": [
            {"index": 0, "finish_reason": finish, "message": {"role": "assistant", **message}}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    body.update(extra)
    return ChatCompletion.model_validate(body)


def call_dict(call_id: str | None, name: str, arguments: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "function", "function": {"name": name, "arguments": arguments}}
    if call_id is not None:
        out["id"] = call_id
    return out


def parse(c: ChatCompletion, ep: Endpoint | None = None, request: LLMRequest | None = None) -> Any:
    return parse_completion(ep or endpoint(), c, request or req(), safety_factor=1.2)


@pytest.mark.unit
def test_plain_answer() -> None:
    parsed = parse(completion({"content": "hello"}))
    assert parsed.message == Message(role="assistant", content="hello", produced_by="deepseek-pro")
    assert parsed.finish_reason == "stop"
    assert parsed.malformed_calls == {}


@pytest.mark.unit
def test_reasoning_content_is_captured_and_stamped_with_the_endpoint() -> None:
    parsed = parse(completion({"content": "42", "reasoning_content": "let me think"}))
    assert parsed.message.reasoning == "let me think"
    assert parsed.message.produced_by == "deepseek-pro"


@pytest.mark.unit
def test_no_reasoning_when_the_endpoint_declares_no_field() -> None:
    ep = endpoint(reasoning_field=None)
    parsed = parse(completion({"content": "42", "reasoning_content": "hidden"}), ep)
    assert parsed.message.reasoning is None


@pytest.mark.unit
def test_tool_calls_are_normalised() -> None:
    parsed = parse(
        completion(
            {"content": None, "tool_calls": [call_dict("c1", "grep", '{"pattern": "x"}')]},
            finish="tool_calls",
        )
    )
    assert parsed.message.tool_calls == (ToolCall(id="c1", name="grep", args={"pattern": "x"}),)
    assert parsed.message.content == ""
    assert parsed.malformed_calls == {}


@pytest.mark.unit
def test_empty_arguments_mean_no_arguments() -> None:
    for empty in ("", "  "):
        parsed = parse(completion({"tool_calls": [call_dict("c1", "grep", empty)]}, "tool_calls"))
        assert parsed.message.tool_calls[0].args == {}
        assert parsed.malformed_calls == {}


@pytest.mark.unit
@pytest.mark.parametrize("bad", ['{"pattern": "unterminated', "[1, 2]", '"text"', "42"])
def test_arguments_that_are_not_a_json_object_are_flagged(bad: str) -> None:
    parsed = parse(completion({"tool_calls": [call_dict("c1", "grep", bad)]}, "tool_calls"))
    assert parsed.message.tool_calls[0].args == {"_raw": bad}
    assert "c1" in parsed.malformed_calls
    assert "JSON" in parsed.malformed_calls["c1"]


@pytest.mark.unit
def test_duplicate_and_empty_ids_get_unique_generated_ones() -> None:
    parsed = parse(
        completion(
            {
                "tool_calls": [
                    call_dict("dup", "b", "{}"),
                    call_dict("dup", "c", "{}"),
                    call_dict("", "d", "{}"),
                ]
            },
            "tool_calls",
        )
    )
    ids = [c.id for c in parsed.message.tool_calls]
    assert len(set(ids)) == 3 and all(ids)
    assert ids[0] == "dup"  # 第一次出现保持原样


@pytest.mark.unit
def test_unknown_tool_names_are_kept_for_the_dispatcher() -> None:
    parsed = parse(
        completion({"tool_calls": [call_dict("c1", "no_such_tool", "{}")]}, "tool_calls"),
        request=req(tools=(SEARCH,)),
    )
    assert parsed.message.tool_calls[0].name == "no_such_tool"


@pytest.mark.unit
def test_truncated_output_with_tool_calls_is_not_executable() -> None:
    parsed = parse(
        completion({"tool_calls": [call_dict("c1", "grep", '{"pattern": "x"}')]}, finish="length")
    )
    assert "c1" in parsed.malformed_calls
    assert "截断" in parsed.malformed_calls["c1"]


@pytest.mark.unit
def test_empty_answer_without_tool_calls_is_an_invalid_response() -> None:
    from agent_runtime.llm import LLMInvalidResponse

    with pytest.raises(LLMInvalidResponse):
        parse(completion({"content": ""}))
    with pytest.raises(LLMInvalidResponse):
        parse(completion({"content": None}))


@pytest.mark.unit
def test_no_choices_is_an_invalid_response() -> None:
    from agent_runtime.llm import LLMInvalidResponse

    with pytest.raises(LLMInvalidResponse):
        parse(completion({"content": "x"}, choices=[]))


# ==============================================================================
# 用量
# ==============================================================================


@pytest.mark.unit
def test_usage_from_the_provider() -> None:
    usage = parse(completion({"content": "x"})).usage
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (10, 5, 15)
    assert usage.estimated is False
    assert usage.cache_hit_tokens is None and usage.cache_miss_tokens is None


@pytest.mark.unit
def test_cache_and_reasoning_token_details() -> None:
    c = completion(
        {"content": "x"},
        usage={
            "prompt_tokens": 100,
            "completion_tokens": 40,
            "total_tokens": 140,
            "prompt_cache_hit_tokens": 80,
            "prompt_cache_miss_tokens": 20,
            "completion_tokens_details": {"reasoning_tokens": 25},
        },
    )
    usage = parse(c).usage
    assert (usage.cache_hit_tokens, usage.cache_miss_tokens) == (80, 20)
    assert usage.reasoning_tokens == 25


@pytest.mark.unit
def test_missing_usage_is_estimated_high_and_marked() -> None:
    c = completion({"content": "你好，世界" * 20}, usage=None)
    request = req(Message(role="user", content="请总结以下内容" * 30))
    usage = parse_completion(endpoint(), c, request, safety_factor=1.2).usage
    assert usage.estimated is True
    assert usage.prompt_tokens >= len("请总结以下内容" * 30)  # CJK 至少一字一 token，宁高勿低
    assert usage.completion_tokens >= 100
    assert usage.total_tokens == usage.prompt_tokens + usage.completion_tokens


@pytest.mark.unit
def test_an_endpoint_that_never_reports_usage_is_always_estimated() -> None:
    ep = endpoint(usage_in_response=False)
    usage = parse(completion({"content": "x"}), ep).usage
    assert usage.estimated is True


@pytest.mark.unit
def test_estimate_tokens_is_conservative() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 400) >= 100  # 英文约 4 字符一 token，向上取
    assert estimate_tokens("汉" * 100) >= 100
    assert estimate_tokens("x") >= 1


@pytest.mark.unit
def test_the_request_is_json_serialisable() -> None:
    call = ToolCall(id="c1", name="grep", args={"a": [1, {"b": None}]})
    out = build(messages=(USER, Message(role="assistant", content="", tool_calls=(call,))))
    json.dumps(out)

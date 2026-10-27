"""网关的结构化输出：路径选择、校验、修复重试、截断、降级与计数。

出处：docs/07_llm_gateway/02_structured_output.md。
"""

from __future__ import annotations

import json
from typing import Any, Literal

import httpx2
import pytest
from pydantic import BaseModel

from agent_runtime.core import Message
from agent_runtime.llm import (
    Degraded,
    GatewayTier,
    LLMGateway,
    LLMRequest,
    Parsed,
    StructuredRecord,
)
from baseagent_config.models import Capabilities, Endpoint, LlmConfig
from test_llm_gateway import OK_BODY, USER, World, status


class Item(BaseModel):
    id: int


class Verdict(BaseModel):
    thought: str
    next_step: str
    status: Literal["ok", "blocked"] = "ok"
    items: list[Item] = []


GOOD = {"thought": "想法", "next_step": "读文件"}


def reply(
    content: str | dict[str, Any] | None = None,
    *,
    finish: str = "stop",
    tool_args: str | None = None,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> httpx2.Response:
    body = json.loads(json.dumps(OK_BODY))
    choice = body["choices"][0]
    choice["finish_reason"] = finish
    text = json.dumps(content, ensure_ascii=False) if isinstance(content, dict) else content
    choice["message"]["content"] = text
    if tool_args is not None:
        choice["message"]["tool_calls"] = [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "emit_result", "arguments": tool_args},
            }
        ]
    body["usage"] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    return httpx2.Response(200, json=body)


def ep(name: str = "a", **caps: Any) -> Endpoint:
    return Endpoint(
        name=name,
        base_url=f"https://{name}.example.com",
        model=f"model-{name}",
        api_key_env="UNUSED",
        timeout_sec=60,
        context_window=100_000,
        max_output_tokens=4096,
        capabilities=Capabilities(usage_in_response=True, **caps),
    )


def request(**kw: Any) -> LLMRequest:
    base: dict[str, Any] = {"tier": "fast", "messages": (USER,), "purpose": "planner"}
    return LLMRequest(**{**base, **kw})


def llm(**structured: Any) -> LlmConfig:
    return LlmConfig.model_validate({"structured": structured})


def setup(*endpoints: Endpoint, config: LlmConfig | None = None) -> tuple[World, Any]:
    world = World()
    return world, world.gateway(fast=endpoints, llm=config)


def system_text(body: dict[str, Any]) -> str:
    return "\n".join(m["content"] for m in body["messages"] if m["role"] == "system")


# ==============================================================================
# 四条请求路径
# ==============================================================================


@pytest.mark.unit
async def test_json_schema_path_sends_a_strict_schema_and_no_prompt_copy() -> None:
    world, gateway = setup(ep(json_schema=True, json_object=True, tools=True))
    world.script["a"] = [reply(GOOD)]

    result = await gateway.invoke_structured(request(), Verdict)

    assert isinstance(result, Parsed)
    assert result.value == Verdict.model_validate(GOOD)
    body = world.body()
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "Verdict"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False
    assert "tools" not in body
    assert "Schema" not in system_text(body)
    assert result.response.structured_path == "json_schema"


@pytest.mark.unit
async def test_json_object_path_puts_the_schema_into_a_system_message() -> None:
    world, gateway = setup(ep(json_object=True, tools=True))
    world.script["a"] = [reply(GOOD)]

    result = await gateway.invoke_structured(request(), Verdict)

    assert isinstance(result, Parsed)
    body = world.body()
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system"
    assert "json" in body["messages"][0]["content"].lower()
    assert "next_step" in body["messages"][0]["content"]
    assert body["messages"][1] == {"role": "user", "content": "hi"}
    assert result.response.structured_path == "json_object"


@pytest.mark.unit
async def test_json_object_path_extends_the_callers_system_message_without_mutating_it() -> None:
    world, gateway = setup(ep(json_object=True))
    world.script["a"] = [reply(GOOD)]
    system = Message(role="system", content="你是规划器。")

    await gateway.invoke_structured(request(messages=(system, USER)), Verdict)

    body = world.body()
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"].startswith("你是规划器。\n\n")
    assert "next_step" in body["messages"][0]["content"]
    assert system.content == "你是规划器。"


@pytest.mark.unit
async def test_tool_path_forces_the_emit_result_tool() -> None:
    world, gateway = setup(ep(tools=True))
    world.script["a"] = [reply(None, tool_args=json.dumps(GOOD))]

    result = await gateway.invoke_structured(request(), Verdict)

    assert isinstance(result, Parsed)
    assert result.value.next_step == "读文件"
    body = world.body()
    assert [t["function"]["name"] for t in body["tools"]] == ["emit_result"]
    assert "next_step" in body["tools"][0]["function"]["parameters"]["properties"]
    assert body["tool_choice"] == {"type": "function", "function": {"name": "emit_result"}}
    assert "response_format" not in body
    assert result.response.structured_path == "tool"


@pytest.mark.unit
async def test_tool_path_falls_back_to_the_text_when_the_model_ignores_the_tool() -> None:
    world, gateway = setup(ep(tools=True))
    world.script["a"] = [reply(GOOD)]
    result = await gateway.invoke_structured(request(), Verdict)
    assert isinstance(result, Parsed)
    assert result.value.thought == "想法"


@pytest.mark.unit
async def test_prompt_path_is_lenient_about_wrappers() -> None:
    world, gateway = setup(ep())
    world.script["a"] = [reply("结果：\n```json\n" + json.dumps(GOOD) + "\n```\n")]

    result = await gateway.invoke_structured(request(), Verdict)

    assert isinstance(result, Parsed)
    body = world.body()
    assert "response_format" not in body
    assert "tools" not in body
    assert "next_step" in system_text(body)
    assert result.response.structured_path == "prompt"


@pytest.mark.unit
async def test_prefer_schema_false_prefers_json_object() -> None:
    world, gateway = setup(ep(json_schema=True, json_object=True), config=llm(prefer_schema=False))
    world.script["a"] = [reply(GOOD)]
    await gateway.invoke_structured(request(), Verdict)
    assert world.body()["response_format"] == {"type": "json_object"}


@pytest.mark.unit
async def test_each_endpoint_gets_the_path_its_own_capabilities_allow() -> None:
    world, gateway = setup(ep("a", json_schema=True), ep("b"))
    world.script["a"] = [status(500)]
    world.script["b"] = [reply(GOOD)]

    result = await gateway.invoke_structured(request(), Verdict)

    assert isinstance(result, Parsed)
    assert world.body(0)["response_format"]["type"] == "json_schema"
    assert "response_format" not in world.body(-1)
    assert result.response.structured_path == "prompt"
    assert result.response.endpoint == "b"


@pytest.mark.unit
async def test_structured_output_and_caller_tools_cannot_be_combined() -> None:
    from test_llm_gateway import SEARCH

    _, gateway = setup(ep(tools=True))
    with pytest.raises(ValueError, match="工具"):
        await gateway.invoke_structured(request(tools=(SEARCH,)), Verdict)


# ==============================================================================
# 校验与修复重试
# ==============================================================================


@pytest.mark.unit
async def test_unknown_fields_are_ignored_and_defaults_apply() -> None:
    world, gateway = setup(ep(json_object=True))
    world.script["a"] = [reply({**GOOD, "surprise": 1})]
    result = await gateway.invoke_structured(request(), Verdict)
    assert isinstance(result, Parsed)
    assert result.value.status == "ok"
    assert result.value.items == []


@pytest.mark.unit
async def test_a_validation_failure_is_repaired_once_with_the_errors_in_the_prompt() -> None:
    world, gateway = setup(ep(json_object=True))
    world.script["a"] = [reply({"thought": "t"}), reply(GOOD, prompt_tokens=30)]

    result = await gateway.invoke_structured(request(), Verdict)

    assert isinstance(result, Parsed)
    assert result.value.next_step == "读文件"
    assert result.rounds == 2
    assert result.repaired is True
    assert len(world.requests) == 2

    second = world.body(1)["messages"]
    assert second[-2] == {"role": "assistant", "content": json.dumps({"thought": "t"})}
    assert second[-1]["role"] == "user"
    assert "`next_step`" in second[-1]["content"]
    assert "只输出" in second[-1]["content"]
    # 上一轮的原消息仍在
    assert {"role": "user", "content": "hi"} in second


@pytest.mark.unit
async def test_usage_is_summed_over_the_repair_rounds() -> None:
    world, gateway = setup(ep(json_object=True))
    world.script["a"] = [
        reply({"thought": "t"}, prompt_tokens=10, completion_tokens=5),
        reply(GOOD, prompt_tokens=30, completion_tokens=7),
    ]
    result = await gateway.invoke_structured(request(), Verdict)
    assert isinstance(result, Parsed)
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (40, 12)
    assert result.usage.total_tokens == 52
    assert result.response.usage.total_tokens == 37  # 最后一次调用本身


@pytest.mark.unit
async def test_the_repair_goes_to_the_endpoint_that_produced_the_bad_output() -> None:
    world, gateway = setup(ep("a", json_object=True), ep("b", json_object=True))
    world.script["a"] = [status(500)]
    world.script["b"] = [reply({"thought": "t"}), reply(GOOD)]

    result = await gateway.invoke_structured(request(), Verdict)

    assert isinstance(result, Parsed)
    assert result.response.endpoint == "b"
    assert len(world.host_requests("a")) >= 1
    first_a = len(world.host_requests("a"))
    assert [r.url.host for r in world.requests[first_a:]] == ["b.example.com", "b.example.com"]


@pytest.mark.unit
async def test_pinning_does_not_forbid_falling_back_when_the_endpoint_fails() -> None:
    world, gateway = setup(ep("a", json_object=True), ep("b", json_object=True))
    # 首轮由 a 产出坏输出；修复轮 a 挂了 -> 允许降级到 b
    world.script["a"] = [reply({"thought": "t"}), status(500)]
    world.script["b"] = [reply(GOOD)]
    result = await gateway.invoke_structured(request(), Verdict)
    assert isinstance(result, Parsed)
    assert result.response.endpoint == "b"


@pytest.mark.unit
async def test_two_failures_give_a_degraded_result_with_the_raw_text() -> None:
    world, gateway = setup(ep(json_object=True))
    world.script["a"] = [reply({"thought": "t"}), reply("完全不是 JSON")]

    result = await gateway.invoke_structured(request(), Verdict)

    assert isinstance(result, Degraded)
    assert result.reason == "invalid"
    assert result.raw_text == "完全不是 JSON"
    assert result.rounds == 2
    assert any("JSON" in p for p in result.problems)
    assert len(world.requests) == 2  # 修复最多一次


@pytest.mark.unit
async def test_repair_retries_is_configurable() -> None:
    world, gateway = setup(ep(json_object=True), config=llm(repair_retries=0))
    world.script["a"] = [reply({"thought": "t"})]
    result = await gateway.invoke_structured(request(), Verdict)
    assert isinstance(result, Degraded)
    assert len(world.requests) == 1

    world, gateway = setup(ep(json_object=True), config=llm(repair_retries=2))
    world.script["a"] = [reply({"thought": "t"}), reply({"thought": "t"}), reply(GOOD)]
    result = await gateway.invoke_structured(request(), Verdict)
    assert isinstance(result, Parsed)
    assert result.rounds == 3


@pytest.mark.unit
async def test_a_value_outside_the_enum_is_rejected_and_never_used() -> None:
    world, gateway = setup(ep(json_object=True), config=llm(repair_retries=0))
    world.script["a"] = [reply({**GOOD, "status": "ok; rm -rf /"})]
    result = await gateway.invoke_structured(request(), Verdict)
    assert isinstance(result, Degraded)
    assert any("status" in p for p in result.problems)
    assert "rm -rf" not in "\n".join(result.problems)


@pytest.mark.unit
async def test_tool_path_arguments_that_are_not_json_are_repaired() -> None:
    world, gateway = setup(ep(tools=True))
    world.script["a"] = [
        reply(None, tool_args='{"thought": '),
        reply(None, tool_args=json.dumps(GOOD)),
    ]
    result = await gateway.invoke_structured(request(), Verdict)
    assert isinstance(result, Parsed)
    assert result.rounds == 2
    # 修复提示里回显的是原始参数文本，而不是重新编码的东西
    assert world.body(1)["messages"][-2]["content"] == '{"thought": '


@pytest.mark.unit
async def test_an_empty_response_is_a_transient_failure_not_a_validation_failure() -> None:
    world, gateway = setup(ep(json_object=True))
    world.script["a"] = [reply(""), reply(GOOD)]
    result = await gateway.invoke_structured(request(), Verdict)
    assert isinstance(result, Parsed)
    assert result.rounds == 1
    assert result.response.attempts == 2


# ==============================================================================
# 截断与内容过滤
# ==============================================================================


@pytest.mark.unit
async def test_a_truncated_output_is_not_parsed_even_if_it_looks_complete() -> None:
    world, gateway = setup(ep(json_object=True))
    world.script["a"] = [reply(GOOD, finish="length"), reply(GOOD)]

    result = await gateway.invoke_structured(request(max_output_tokens=500), Verdict)

    assert isinstance(result, Parsed)
    assert result.rounds == 2
    assert len(world.requests) == 2
    first, second = world.body(0), world.body(1)
    assert first["max_tokens"] == 500
    assert second["max_tokens"] == 1000
    assert "缩短" in second["messages"][-1]["content"]


@pytest.mark.unit
async def test_the_raised_output_limit_is_capped_by_the_endpoint() -> None:
    world, gateway = setup(ep(json_object=True))
    world.script["a"] = [reply("{", finish="length"), reply(GOOD)]
    await gateway.invoke_structured(request(max_output_tokens=3000), Verdict)
    assert world.body(1)["max_tokens"] == 4096


@pytest.mark.unit
async def test_raising_the_limit_can_be_switched_off() -> None:
    world, gateway = setup(ep(json_object=True), config=llm(on_truncate_raise_max_tokens=False))
    world.script["a"] = [reply("{", finish="length"), reply(GOOD)]
    await gateway.invoke_structured(request(max_output_tokens=500), Verdict)
    assert world.body(1)["max_tokens"] == 500


@pytest.mark.unit
async def test_a_repeated_truncation_degrades_with_its_own_reason() -> None:
    world, gateway = setup(ep(json_object=True))
    world.script["a"] = [reply('{"thought": "x', finish="length")]
    result = await gateway.invoke_structured(request(max_output_tokens=500), Verdict)
    assert isinstance(result, Degraded)
    assert result.reason == "truncated"
    assert len(world.requests) == 2


@pytest.mark.unit
async def test_the_echo_of_a_huge_truncated_output_is_capped_in_the_repair_prompt() -> None:
    world, gateway = setup(ep(json_object=True))
    world.script["a"] = [reply("x" * 50_000, finish="length"), reply(GOOD)]
    await gateway.invoke_structured(request(), Verdict)
    echoed = world.body(1)["messages"][-2]["content"]
    assert len(echoed) < 3000


@pytest.mark.unit
async def test_content_filter_is_reported_without_a_retry() -> None:
    world, gateway = setup(ep(json_object=True))
    world.script["a"] = [reply("部分", finish="content_filter")]
    result = await gateway.invoke_structured(request(), Verdict)
    assert isinstance(result, Degraded)
    assert result.reason == "content_filter"
    assert len(world.requests) == 1


# ==============================================================================
# 事件与计数
# ==============================================================================


@pytest.mark.unit
async def test_outcomes_are_counted_per_purpose_and_reported() -> None:
    world = World()
    records: list[StructuredRecord] = []
    endpoint = ep(json_object=True)
    gateway = LLMGateway(
        {"fast": GatewayTier(temperature=0.2, endpoints=(endpoint,))},
        LlmConfig(),
        client_factory=world.factory,
        clock=world.clock,
        sleep=world.sleep,
        structured_observer=records.append,
    )
    world.script["a"] = [
        reply(GOOD),  # planner: ok
        reply({"thought": "t"}),  # planner: repaired
        reply(GOOD),
        reply("坏"),  # evaluator: degraded
        reply("坏"),
    ]

    await gateway.invoke_structured(request(purpose="planner"), Verdict)
    await gateway.invoke_structured(request(purpose="planner"), Verdict)
    await gateway.invoke_structured(request(purpose="evaluator"), Verdict)

    assert gateway.structured_counts == {
        ("planner", "ok"): 1,
        ("planner", "repaired"): 1,
        ("evaluator", "degraded"): 1,
    }
    assert [(r.purpose, r.outcome, r.rounds) for r in records] == [
        ("planner", "ok", 1),
        ("planner", "repaired", 2),
        ("evaluator", "degraded", 2),
    ]
    assert records[2].reason == "invalid"
    assert records[0].path == "json_object"
    assert records[0].reason is None

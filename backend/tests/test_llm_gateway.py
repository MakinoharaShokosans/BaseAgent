"""LLMGateway：重试、降级、熔断、截止时间、并发、用量，全部用 httpx2.MockTransport 离线测试。

出处：docs/07_llm_gateway/01_gateway.md。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI

from agent_runtime.core import Message, ToolCall, ToolSchema
from agent_runtime.llm import (
    CallRecord,
    GatewayTier,
    LLMBadRequest,
    LLMDeadlineExceeded,
    LLMGateway,
    LLMRequest,
    LLMUnavailableError,
)
from baseagent_config.models import Capabilities, Endpoint, LlmConfig

SECRET = "sk-super-secret-key-0123456789"
USER = Message(role="user", content="hi")
SEARCH = ToolSchema(name="grep", description="d", parameters={"type": "object", "properties": {}})

OK_BODY: dict[str, Any] = {
    "id": "x",
    "object": "chat.completion",
    "created": 1,
    "model": "deepseek-v4-pro",
    "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hello"}}
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


def ok(content: str = "hello") -> httpx2.Response:
    body = json.loads(json.dumps(OK_BODY))
    body["choices"][0]["message"]["content"] = content
    return httpx2.Response(200, json=body)


def status(code: int, message: str = "nope", **headers: str) -> httpx2.Response:
    return httpx2.Response(code, json={"error": {"message": message}}, headers=headers)


def endpoint(name: str = "a", host: str | None = None, **kw: Any) -> Endpoint:
    caps = Capabilities(
        tools=kw.pop("tools", True),
        usage_in_response=True,
        thinking_control="deepseek",
        thinking_with_tools=False,
        reasoning_field="reasoning_content",
    )
    return Endpoint(
        **{
            "name": name,
            "base_url": f"https://{host or name}.example.com",
            "model": f"model-{name}",
            "api_key_env": "UNUSED",
            "timeout_sec": 60,
            "context_window": 100_000,
            "max_output_tokens": 4096,
            "capabilities": caps,
            **kw,
        }
    )


class World:
    """假网络 + 假时钟。每个主机一份剧本：按顺序给响应或抛异常，用完后重复最后一项。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.script: dict[str, list[Any]] = {}
        self.requests: list[httpx2.Request] = []
        self.sleeps: list[float] = []
        self.latency = 0.0
        self.clients: list[AsyncOpenAI] = []
        self.records: list[CallRecord] = []
        self.in_flight = 0
        self.peak = 0
        self.hold = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def host_requests(self, host: str) -> list[httpx2.Request]:
        return [r for r in self.requests if r.url.host == f"{host}.example.com"]

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            self.now += self.latency
            if self.hold:
                await asyncio.sleep(self.hold)
            host = request.url.host.removesuffix(".example.com")
            queue = self.script[host]
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, BaseException):
                raise item
            response = item(request) if callable(item) else item
            assert isinstance(response, httpx2.Response)
            return response
        finally:
            self.in_flight -= 1

    def factory(self, ep: Endpoint) -> AsyncOpenAI:
        client = AsyncOpenAI(
            api_key=SECRET,
            base_url=ep.base_url,
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle)),
        )
        self.clients.append(client)
        return client

    def gateway(
        self,
        reasoning: tuple[Endpoint, ...] = (),
        fast: tuple[Endpoint, ...] = (),
        llm: LlmConfig | None = None,
        rng: Callable[[], float] = lambda: 0.0,
    ) -> LLMGateway:
        tiers = {
            "reasoning": GatewayTier(temperature=0.0, endpoints=reasoning or (endpoint("r"),)),
            "fast": GatewayTier(temperature=0.2, endpoints=fast or (endpoint("a"),)),
        }
        for tier in tiers.values():
            for ep in tier.endpoints:
                self.script.setdefault(ep.name, [ok()])
        return LLMGateway(
            tiers,
            llm or LlmConfig(),
            client_factory=self.factory,
            clock=self.clock,
            sleep=self.sleep,
            rng=rng,
            observer=self.records.append,
        )


def request(**kw: Any) -> LLMRequest:
    base: dict[str, Any] = {"tier": "fast", "messages": (USER,), "purpose": "test"}
    return LLMRequest(**{**base, **kw})


def llm(**sections: dict[str, Any]) -> LlmConfig:
    return LlmConfig.model_validate(sections)


# ==============================================================================
# 成功路径
# ==============================================================================


@pytest.mark.unit
async def test_a_plain_call() -> None:
    world = World()
    world.latency = 0.25
    gateway = world.gateway()
    response = await gateway.invoke(request())

    assert response.message.content == "hello"
    assert response.message.produced_by == "a"
    assert (response.endpoint, response.model) == ("a", "model-a")
    assert response.finish_reason == "stop"
    assert response.attempts == 1
    assert response.duration_ms == 250
    assert response.usage.total_tokens == 15
    await gateway.aclose()


@pytest.mark.unit
async def test_the_wire_request() -> None:
    world = World()
    await world.gateway().invoke(request(tools=(SEARCH,), max_output_tokens=300))
    sent = world.requests[0]
    assert str(sent.url) == "https://a.example.com/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {SECRET}"
    body = world.body()
    assert body["model"] == "model-a"
    assert body["max_tokens"] == 300
    assert body["temperature"] == 0.2
    assert body["thinking"] == {"type": "disabled"}  # extra_body 已并入顶层
    assert body["tools"][0]["function"]["name"] == "grep"


@pytest.mark.unit
async def test_tiers_use_their_own_endpoints_and_temperature() -> None:
    world = World()
    gateway = world.gateway(reasoning=(endpoint("big"),), fast=(endpoint("small"),))
    assert (await gateway.invoke(request(tier="reasoning"))).endpoint == "big"
    assert world.body()["temperature"] == 0.0
    assert (await gateway.invoke(request(tier="fast"))).endpoint == "small"
    assert world.body()["temperature"] == 0.2


@pytest.mark.unit
async def test_clients_are_created_once_per_endpoint_and_closed_on_shutdown() -> None:
    world = World()
    gateway = world.gateway(fast=(endpoint("a"), endpoint("b")))
    for _ in range(5):
        await gateway.invoke(request())
    assert len(world.clients) == 3  # r、a、b 各一个，调用多少次都不新建
    await gateway.aclose()
    assert all(client.is_closed() for client in world.clients)


@pytest.mark.unit
async def test_every_attempt_is_reported_to_the_observer() -> None:
    world = World()
    world.script["a"] = [status(500), ok()]
    await world.gateway().invoke(request(purpose="planner"))
    assert [(r.attempt, r.outcome) for r in world.records] == [(1, "retry"), (2, "ok")]
    first, last = world.records
    assert first.error_class == "InternalServerError"
    assert last.purpose == "planner" and last.tier == "fast"
    assert (last.endpoint, last.model, last.finish_reason) == ("a", "model-a", "stop")
    assert last.usage is not None and last.usage.total_tokens == 15


# ==============================================================================
# 重试
# ==============================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    "failure",
    [
        status(429),
        status(500),
        status(503),
        httpx2.ReadTimeout("slow"),
        httpx2.ConnectError("refused"),
    ],
)
async def test_transient_failures_are_retried_on_the_same_endpoint(failure: Any) -> None:
    world = World()
    world.script["a"] = [failure, ok("recovered")]
    response = await world.gateway().invoke(request())
    assert response.message.content == "recovered"
    assert response.attempts == 2
    assert len(world.host_requests("a")) == 2


@pytest.mark.unit
async def test_backoff_is_exponential_and_capped() -> None:
    world = World()
    world.script["a"] = [status(500)] * 3 + [ok()]
    config = llm(retry={"max_retries": 4, "backoff_factor": 2.0, "backoff_max_sec": 5})
    await world.gateway(llm=config).invoke(request())
    assert world.sleeps == [2.0, 4.0, 5.0]


@pytest.mark.unit
async def test_jitter_is_added_to_the_wait() -> None:
    world = World()
    world.script["a"] = [status(500), ok()]
    gateway = world.gateway(llm=llm(retry={"jitter": 0.5}), rng=lambda: 1.0)
    await gateway.invoke(request())
    assert world.sleeps == [3.0]  # 2 × (1 + 0.5 × 1)


@pytest.mark.unit
async def test_retry_after_is_honoured() -> None:
    world = World()
    world.script["a"] = [status(429, **{"retry-after": "9"}), ok()]
    await world.gateway().invoke(request())
    assert world.sleeps == [9.0]


@pytest.mark.unit
async def test_a_retry_after_beyond_the_cap_abandons_the_endpoint() -> None:
    world = World()
    world.script["a"] = [status(429, **{"retry-after": "600"})]
    world.script["b"] = [ok("from b")]
    response = await world.gateway(fast=(endpoint("a"), endpoint("b"))).invoke(request())
    assert response.endpoint == "b"
    assert world.sleeps == []  # 不为一个注定等不到的窗口干等
    assert len(world.host_requests("a")) == 1


@pytest.mark.unit
async def test_the_attempt_limit_is_per_endpoint() -> None:
    world = World()
    world.script["a"] = [status(500)]
    with pytest.raises(LLMUnavailableError):
        await world.gateway(llm=llm(retry={"max_retries": 3})).invoke(request())
    assert len(world.host_requests("a")) == 3


@pytest.mark.unit
@pytest.mark.parametrize("code", [400, 401, 403, 422])
async def test_client_errors_are_not_retried(code: int) -> None:
    world = World()
    world.script["a"] = [status(code)]
    with pytest.raises((LLMBadRequest, LLMUnavailableError)):
        await world.gateway().invoke(request())
    assert len(world.host_requests("a")) == 1


@pytest.mark.unit
async def test_a_malformed_response_is_retried_once() -> None:
    world = World()
    empty = httpx2.Response(200, json={**OK_BODY, "choices": []})
    world.script["a"] = [empty, ok("fine")]
    response = await world.gateway().invoke(request())
    assert response.message.content == "fine"
    assert response.attempts == 2


@pytest.mark.unit
async def test_a_second_malformed_response_exhausts_the_endpoint() -> None:
    world = World()
    world.script["a"] = [httpx2.Response(200, json={**OK_BODY, "choices": []})]
    world.script["b"] = [ok("from b")]
    response = await world.gateway(fast=(endpoint("a"), endpoint("b"))).invoke(request())
    assert response.endpoint == "b"
    assert len(world.host_requests("a")) == 2


# ==============================================================================
# 降级
# ==============================================================================


@pytest.mark.unit
async def test_falls_back_to_the_next_endpoint_with_the_same_request() -> None:
    world = World()
    world.script["a"] = [status(500)]
    world.script["b"] = [ok("from b")]
    gateway = world.gateway(fast=(endpoint("a"), endpoint("b")))
    response = await gateway.invoke(request(tools=(SEARCH,)))

    assert response.endpoint == "b" and response.message.content == "from b"
    a_body, b_body = (json.loads(world.host_requests(h)[-1].content) for h in ("a", "b"))
    assert a_body["messages"] == b_body["messages"]
    assert a_body["tools"] == b_body["tools"]
    assert (a_body["model"], b_body["model"]) == ("model-a", "model-b")
    assert response.attempts == 4  # a 三次 + b 一次


@pytest.mark.unit
async def test_fallback_outcome_is_labelled_in_the_records() -> None:
    world = World()
    world.script["a"] = [status(401)]
    world.script["b"] = [ok()]
    await world.gateway(fast=(endpoint("a"), endpoint("b"))).invoke(request())
    assert [(r.endpoint, r.outcome) for r in world.records] == [("a", "fallback"), ("b", "ok")]


@pytest.mark.unit
async def test_exhausting_everything_reports_each_endpoint() -> None:
    world = World()
    world.script["a"] = [status(500, "a is down")]
    world.script["b"] = [httpx2.ConnectError("refused")]
    gateway = world.gateway(fast=(endpoint("a"), endpoint("b")))
    with pytest.raises(LLMUnavailableError) as info:
        await gateway.invoke(request())

    failures = {f.endpoint: f for f in info.value.failures}
    assert set(failures) == {"a", "b"}
    assert failures["a"].error_class == "InternalServerError"
    assert failures["b"].error_class == "APIConnectionError"
    assert failures["a"].attempts == 3
    assert "a" in str(info.value) and "b" in str(info.value)
    assert world.records[-1].outcome == "error"


@pytest.mark.unit
async def test_a_bad_request_still_tries_the_next_endpoint() -> None:
    world = World()
    world.script["a"] = [status(400, "context length exceeded")]
    world.script["b"] = [ok("bigger window")]
    response = await world.gateway(fast=(endpoint("a"), endpoint("b"))).invoke(request())
    assert response.endpoint == "b"
    assert len(world.host_requests("a")) == 1


@pytest.mark.unit
async def test_bad_requests_everywhere_raise_bad_request_not_unavailable() -> None:
    world = World()
    world.script["a"] = [status(400, "invalid parameter")]
    with pytest.raises(LLMBadRequest) as info:
        await world.gateway().invoke(request())
    assert info.value.context_overflow is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    [
        "This model's maximum context length is 65536 tokens",
        "context_length_exceeded",
        "Input too many tokens",
    ],
)
async def test_context_overflow_is_recognised(message: str) -> None:
    world = World()
    world.script["a"] = [status(400, message)]
    with pytest.raises(LLMBadRequest) as info:
        await world.gateway().invoke(request())
    assert info.value.context_overflow is True


@pytest.mark.unit
async def test_an_auth_failure_blocks_the_endpoint_until_reload() -> None:
    world = World()
    world.script["a"] = [status(401, "bad key")]
    world.script["b"] = [ok("from b")]
    gateway = world.gateway(fast=(endpoint("a"), endpoint("b")))
    await gateway.invoke(request())
    await gateway.invoke(request())
    assert len(world.host_requests("a")) == 1  # 第二次不再先撞一次 401

    world.script["a"] = [ok("from a again")]
    gateway.reload()
    assert (await gateway.invoke(request())).endpoint == "a"


@pytest.mark.unit
async def test_payment_required_is_handled_like_an_auth_failure() -> None:
    world = World()
    world.script["a"] = [status(402, "insufficient balance")]
    world.script["b"] = [ok("from b")]
    gateway = world.gateway(fast=(endpoint("a"), endpoint("b")))
    assert (await gateway.invoke(request())).endpoint == "b"
    await gateway.invoke(request())
    assert len(world.host_requests("a")) == 1


@pytest.mark.unit
async def test_endpoints_without_the_needed_capability_are_skipped() -> None:
    world = World()
    gateway = world.gateway(fast=(endpoint("a", tools=False), endpoint("b")))
    response = await gateway.invoke(request(tools=(SEARCH,)))
    assert response.endpoint == "b"
    assert world.host_requests("a") == []


@pytest.mark.unit
async def test_no_capable_endpoint_is_unavailable() -> None:
    world = World()
    gateway = world.gateway(fast=(endpoint("a", tools=False),))
    with pytest.raises(LLMUnavailableError, match="能力"):
        await gateway.invoke(request(tools=(SEARCH,)))
    assert world.requests == []


# ==============================================================================
# 熔断
# ==============================================================================


def breaker_world() -> tuple[World, LLMGateway]:
    world = World()
    world.script["a"] = [status(500)]
    world.script["b"] = [ok("from b")]
    config = llm(retry={"max_retries": 1}, circuit={"open_after": 2, "open_sec": 60})
    return world, world.gateway(fast=(endpoint("a"), endpoint("b")), llm=config)


@pytest.mark.unit
async def test_a_dead_endpoint_is_skipped_once_its_breaker_opens() -> None:
    world, gateway = breaker_world()
    await gateway.invoke(request())
    await gateway.invoke(request())
    assert len(world.host_requests("a")) == 2  # 熔断前各试一次
    await gateway.invoke(request())
    await gateway.invoke(request())
    assert len(world.host_requests("a")) == 2  # 打开后直接跳过
    assert len(world.host_requests("b")) == 4


@pytest.mark.unit
async def test_a_probe_after_the_window_can_close_the_breaker() -> None:
    world, gateway = breaker_world()
    for _ in range(3):
        await gateway.invoke(request())
    world.now += 61
    world.script["a"] = [ok("a is back")]
    assert (await gateway.invoke(request())).endpoint == "a"
    assert (await gateway.invoke(request())).endpoint == "a"  # 已恢复


@pytest.mark.unit
async def test_a_failed_probe_keeps_it_open() -> None:
    world, gateway = breaker_world()
    for _ in range(3):
        await gateway.invoke(request())
    before = len(world.host_requests("a"))
    world.now += 61
    await gateway.invoke(request())  # 探测失败，降级到 b
    await gateway.invoke(request())  # 又被跳过
    assert len(world.host_requests("a")) == before + 1


@pytest.mark.unit
async def test_when_every_endpoint_is_open_the_latest_opened_is_still_tried() -> None:
    world = World()
    world.script["a"] = [status(500)]
    world.script["b"] = [status(500)]
    world.latency = 1.0  # 让两个端点打开的时刻不同
    config = llm(retry={"max_retries": 1}, circuit={"open_after": 1, "open_sec": 60})
    gateway = world.gateway(fast=(endpoint("a"), endpoint("b")), llm=config)
    with pytest.raises(LLMUnavailableError):
        await gateway.invoke(request())  # 两个都打开：a 先、b 后
    a_before, b_before = len(world.host_requests("a")), len(world.host_requests("b"))

    world.script["b"] = [ok("b recovered")]
    response = await gateway.invoke(request())  # 都在冷却期，仍然试最近打开的 b
    assert response.endpoint == "b"
    assert len(world.host_requests("a")) == a_before
    assert len(world.host_requests("b")) == b_before + 1


@pytest.mark.unit
async def test_bad_requests_do_not_count_against_endpoint_health() -> None:
    world = World()
    world.script["a"] = [status(400, "bad")]
    world.script["b"] = [ok()]
    config = llm(circuit={"open_after": 1, "open_sec": 60})
    gateway = world.gateway(fast=(endpoint("a"), endpoint("b")), llm=config)
    for _ in range(3):
        await gateway.invoke(request())
    assert len(world.host_requests("a")) == 3  # 请求本身有问题不是端点的错，不熔断


# ==============================================================================
# 截止时间
# ==============================================================================


@pytest.mark.unit
async def test_no_attempt_starts_without_enough_time() -> None:
    world = World()
    deadline = world.now + 4.9  # 最小尝试时间默认 5 秒
    with pytest.raises(LLMDeadlineExceeded):
        await world.gateway().invoke(request(deadline=deadline))
    assert world.requests == []


@pytest.mark.unit
async def test_the_per_attempt_timeout_is_cut_to_the_remaining_time() -> None:
    world = World()
    await world.gateway().invoke(request(deadline=world.now + 20))
    assert world.requests[0].extensions["timeout"]["read"] == pytest.approx(20)


@pytest.mark.unit
async def test_without_a_deadline_the_endpoint_timeout_applies() -> None:
    world = World()
    await world.gateway().invoke(request())
    assert world.requests[0].extensions["timeout"]["read"] == 60


@pytest.mark.unit
async def test_a_backoff_that_would_overrun_the_deadline_gives_up_instead_of_sleeping() -> None:
    world = World()
    world.script["a"] = [status(500)]
    with pytest.raises(LLMDeadlineExceeded):
        await world.gateway().invoke(request(deadline=world.now + 6.9))
    assert world.sleeps == []  # 等 2 秒后只剩 4.9 秒，不到一次最小尝试（5 秒）：不等了
    assert len(world.host_requests("a")) == 1


@pytest.mark.unit
async def test_time_spent_on_earlier_attempts_counts() -> None:
    world = World()
    world.latency = 3.0
    world.script["a"] = [status(500)]
    with pytest.raises(LLMDeadlineExceeded):
        await world.gateway().invoke(request(deadline=world.now + 9))
    assert len(world.host_requests("a")) == 1


@pytest.mark.unit
async def test_the_deadline_error_is_distinct_from_unavailability() -> None:
    assert not issubclass(LLMDeadlineExceeded, LLMUnavailableError)
    assert not issubclass(LLMUnavailableError, LLMDeadlineExceeded)


@pytest.mark.unit
async def test_the_deadline_error_carries_what_failed_before_it() -> None:
    world = World()
    world.script["a"] = [status(500)]
    with pytest.raises(LLMDeadlineExceeded) as info:
        await world.gateway().invoke(request(deadline=world.now + 6.9))
    assert [f.endpoint for f in info.value.failures] == ["a"]


# ==============================================================================
# 并发与限流
# ==============================================================================


@pytest.mark.unit
async def test_endpoint_concurrency_is_bounded() -> None:
    world = World()
    world.hold = 0.02
    gateway = world.gateway(llm=llm(concurrency={"per_endpoint": 2}))
    await asyncio.gather(*(gateway.invoke(request()) for _ in range(8)))
    assert world.peak == 2


@pytest.mark.unit
async def test_a_429_lowers_the_endpoint_concurrency() -> None:
    world = World()
    world.script["a"] = [status(429), ok()]
    world.hold = 0.02
    gateway = world.gateway(llm=llm(concurrency={"per_endpoint": 4}))
    await gateway.invoke(request())
    assert gateway.concurrency_limit("a") == 2


# ==============================================================================
# 工具调用与用量
# ==============================================================================


@pytest.mark.unit
async def test_malformed_tool_calls_are_surfaced_on_the_response() -> None:
    world = World()
    body = json.loads(json.dumps(OK_BODY))
    body["choices"][0]["finish_reason"] = "tool_calls"
    body["choices"][0]["message"] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "grep", "arguments": "{oops"}},
            {"id": "c2", "type": "function", "function": {"name": "grep", "arguments": "{}"}},
        ],
    }
    world.script["a"] = [httpx2.Response(200, json=body)]
    response = await world.gateway().invoke(request(tools=(SEARCH,)))
    assert response.message.tool_calls[0] == ToolCall(id="c1", name="grep", args={"_raw": "{oops"})
    assert set(response.malformed_calls) == {"c1"}


@pytest.mark.unit
async def test_a_tool_call_without_an_id_gets_one() -> None:
    world = World()
    body = json.loads(json.dumps(OK_BODY))
    body["choices"][0]["message"] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"type": "function", "function": {"name": "grep", "arguments": "{}"}},
            {"type": "function", "function": {"name": "grep", "arguments": "{}"}},
        ],
    }
    body["choices"][0]["finish_reason"] = "tool_calls"
    world.script["a"] = [httpx2.Response(200, json=body)]
    response = await world.gateway().invoke(request(tools=(SEARCH,)))
    ids = [c.id for c in response.message.tool_calls]
    assert len(set(ids)) == 2 and all(ids)


@pytest.mark.unit
async def test_null_tool_arguments_on_the_wire_mean_no_arguments() -> None:
    world = World()
    body = json.loads(json.dumps(OK_BODY))
    body["choices"][0]["message"] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "grep", "arguments": None}}
        ],
    }
    body["choices"][0]["finish_reason"] = "tool_calls"
    world.script["a"] = [httpx2.Response(200, json=body)]
    response = await world.gateway().invoke(request(tools=(SEARCH,)))
    assert response.message.tool_calls == (ToolCall(id="c1", name="grep", args={}),)
    assert response.malformed_calls == {}


@pytest.mark.unit
async def test_missing_usage_is_estimated_with_the_configured_safety_factor() -> None:
    world = World()
    body = json.loads(json.dumps(OK_BODY))
    del body["usage"]
    world.script["a"] = [httpx2.Response(200, json=body)]
    config = llm(usage={"estimate_safety_factor": 2.0})
    high = await world.gateway(llm=config).invoke(request())
    world2 = World()
    world2.script["a"] = [httpx2.Response(200, json=body)]
    low = await world2.gateway(llm=llm(usage={"estimate_safety_factor": 1.0})).invoke(request())
    assert high.usage.estimated and low.usage.estimated
    assert high.usage.total_tokens > low.usage.total_tokens > 0


@pytest.mark.unit
async def test_estimation_can_be_switched_off() -> None:
    world = World()
    body = json.loads(json.dumps(OK_BODY))
    del body["usage"]
    world.script["a"] = [httpx2.Response(200, json=body)]
    config = llm(usage={"estimate_when_missing": False})
    usage = (await world.gateway(llm=config).invoke(request())).usage
    assert usage.total_tokens == 0 and usage.estimated is False


# ==============================================================================
# 安全
# ==============================================================================


@pytest.mark.security
@pytest.mark.unit
async def test_the_key_never_appears_in_errors_or_records() -> None:
    world = World()
    world.script["a"] = [status(401, f"Incorrect API key provided: {SECRET}")]
    world.script["b"] = [status(500, f"upstream echoed {SECRET}")]
    gateway = world.gateway(fast=(endpoint("a"), endpoint("b")))
    with pytest.raises(LLMUnavailableError) as info:
        await gateway.invoke(request())

    assert SECRET not in str(info.value)
    assert all(SECRET not in f.message for f in info.value.failures)
    assert all(SECRET not in repr(record) for record in world.records)
    assert any("***" in f.message for f in info.value.failures)


@pytest.mark.unit
async def test_construction_rejects_opaque_passback_until_it_is_implemented() -> None:
    world = World()
    bad = endpoint("a").model_copy(
        update={"capabilities": Capabilities(tools=True, reasoning_passback="opaque")}
    )
    with pytest.raises(NotImplementedError, match="opaque"):
        world.gateway(fast=(bad,))


# ==============================================================================
# 从加载好的配置装配
# ==============================================================================


@pytest.fixture
def loaded(tmp_path: Any, config_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from baseagent_config import load_settings

    for name in list(__import__("os").environ):
        if name.startswith("BASEAGENT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("BASEAGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    return load_settings(config_path=config_path, create_home=False)


@pytest.mark.unit
async def test_from_loaded_builds_both_tiers_from_the_reference_config(loaded: Any) -> None:
    gateway = LLMGateway.from_loaded(loaded)
    assert gateway.endpoint_names("reasoning") == ("deepseek-pro",)
    assert gateway.endpoint_names("fast") == ("deepseek-flash",)
    await gateway.aclose()


@pytest.mark.unit
async def test_from_loaded_sends_the_key_from_the_environment(loaded: Any) -> None:
    world = World()

    def factory(ep: Endpoint) -> AsyncOpenAI:
        from agent_runtime.llm.gateway import default_client_factory

        real = default_client_factory({"DEEPSEEK_API_KEY": SECRET})(ep)
        assert real.api_key == SECRET and real.max_retries == 0
        return world.factory(ep)

    gateway = LLMGateway.from_loaded(loaded, client_factory=factory)
    world.script["deepseek-flash"] = [ok()]
    await gateway.aclose()


@pytest.mark.unit
def test_api_key_resolution() -> None:
    from agent_runtime.llm.gateway import api_key_for

    cloud = endpoint("c", host="api")
    local = endpoint("l").model_copy(update={"base_url": "http://127.0.0.1:11434"})
    assert api_key_for(cloud.model_copy(update={"api_key_env": "K"}), {"K": " sk-x "}) == "sk-x"
    assert api_key_for(local, {}) == "EMPTY"
    with pytest.raises(ValueError, match="UNUSED"):
        api_key_for(cloud, {})


@pytest.mark.unit
async def test_streaming_accumulates_content_and_emits_deltas() -> None:
    from types import SimpleNamespace

    from agent_runtime.core import Message
    from agent_runtime.llm import LLMRequest

    chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(content="he", reasoning_content="think", tool_calls=[]),
                )
            ]
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    delta=SimpleNamespace(content="llo", reasoning_content="ing", tool_calls=[]),
                )
            ]
        ),
    ]

    class Stream:
        def __aiter__(self):
            async def walk():
                for chunk in chunks:
                    yield chunk

            return walk()

    class Completions:
        async def create(self, **kwargs: Any):
            assert kwargs["stream"] is True
            return Stream()

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    ep = endpoint()
    ep = ep.model_copy(
        update={"capabilities": ep.capabilities.model_copy(update={"streaming": True})}
    )
    gw = LLMGateway(
        {"fast": GatewayTier(0.0, (ep,))},
        LlmConfig(),
        client_factory=lambda _: client,  # type: ignore[arg-type]
    )
    rt = SimpleNamespace(endpoint=ep, client=client)
    seen: list[tuple[str, str]] = []

    async def on_delta(kind: str, text: str) -> None:
        seen.append((kind, text))

    parsed = await gw._stream_once(
        rt,  # type: ignore[arg-type]
        {},
        LLMRequest(
            tier="fast",
            messages=(Message(role="user", content="hi"),),
            stream=True,
            on_delta=on_delta,
        ),
        call_timeout=1,
    )
    assert parsed.message.content == "hello"
    assert parsed.message.reasoning == "thinking"
    assert seen == [
        ("content", "he"),
        ("reasoning", "think"),
        ("content", "llo"),
        ("reasoning", "ing"),
    ]
    assert parsed.usage.total_tokens > 0

"""技术验证项 1：openai 3.x 的行为是否符合文档里的假设。

假设出处：docs/07_llm_gateway/04_deepseek.md、docs/01_architecture/05_tech_stack.md。

全部离线：用 httpx2.MockTransport 充当服务端，不联网、不需要密钥。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2
import openai
import pytest
from openai import AsyncOpenAI

BASE_URL = "https://api.deepseek.com"

COMPLETION: dict[str, Any] = {
    "id": "c1",
    "object": "chat.completion",
    "created": 1,
    "model": "deepseek-v4-pro",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "hi", "reasoning_content": "thinking..."},
        }
    ],
    "usage": {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
        "prompt_cache_hit_tokens": 6,
        "prompt_cache_miss_tokens": 4,
    },
}


class Recorder:
    def __init__(self, responder: Callable[[httpx2.Request], httpx2.Response]) -> None:
        self.requests: list[httpx2.Request] = []
        self._responder = responder

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self._responder(request)

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def _client(recorder: Recorder, **kwargs: Any) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key="sk-test",
        base_url=BASE_URL,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(recorder)),
        **kwargs,
    )


def _ok(_: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=COMPLETION)


MESSAGES: Any = [{"role": "user", "content": "x"}]


@pytest.mark.unit
def test_installed_major_version_is_the_one_we_verified() -> None:
    assert openai.__version__.startswith("3.")


@pytest.mark.unit
def test_sdk_is_built_on_httpx2_not_httpx() -> None:
    """3.x 的底层是独立的 httpx2 包。后果：respx 只拦截 httpx，拦不住 openai 的请求；
    自带的 http_client 必须是 httpx2.AsyncClient；异常类型也是 httpx2 的。"""
    import httpx

    assert httpx2.AsyncClient is not httpx.AsyncClient
    client = AsyncOpenAI(api_key="k")
    assert isinstance(client._client, httpx2.AsyncClient)


# ------------------------------------------------------------------------------
# 重试
# ------------------------------------------------------------------------------


@pytest.mark.unit
async def test_max_retries_zero_means_exactly_one_attempt() -> None:
    rec = Recorder(lambda _: httpx2.Response(500, json={"error": {"message": "boom"}}))
    with pytest.raises(openai.InternalServerError):
        await _client(rec, max_retries=0).chat.completions.create(model="m", messages=MESSAGES)
    assert len(rec.requests) == 1


@pytest.mark.unit
async def test_default_client_retries_twice_on_its_own() -> None:
    """文档假设：SDK 默认自动重试 2 次，所以网关必须显式关掉，自己掌控重试。"""
    rec = Recorder(lambda _: httpx2.Response(500, json={"error": {"message": "boom"}}))
    with pytest.raises(openai.InternalServerError):
        await _client(rec).chat.completions.create(model="m", messages=MESSAGES)
    assert len(rec.requests) == 3


# ------------------------------------------------------------------------------
# 请求形状
# ------------------------------------------------------------------------------


@pytest.mark.unit
async def test_extra_body_fields_are_merged_into_the_top_level_json() -> None:
    rec = Recorder(_ok)
    await _client(rec, max_retries=0).chat.completions.create(
        model="m", messages=MESSAGES, extra_body={"thinking": {"type": "disabled"}}
    )
    assert rec.body()["thinking"] == {"type": "disabled"}


@pytest.mark.unit
async def test_url_and_auth_header() -> None:
    rec = Recorder(_ok)
    await _client(rec, max_retries=0).chat.completions.create(model="m", messages=MESSAGES)
    request = rec.requests[0]
    assert str(request.url) == "https://api.deepseek.com/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"


@pytest.mark.unit
async def test_json_object_response_format_is_sent() -> None:
    rec = Recorder(_ok)
    await _client(rec, max_retries=0).chat.completions.create(
        model="m", messages=MESSAGES, response_format={"type": "json_object"}
    )
    assert rec.body()["response_format"] == {"type": "json_object"}


@pytest.mark.unit
async def test_per_request_timeout_reaches_httpx() -> None:
    rec = Recorder(_ok)
    await _client(rec, max_retries=0).chat.completions.create(
        model="m", messages=MESSAGES, timeout=12.5
    )
    assert rec.requests[0].extensions["timeout"]["read"] == 12.5


@pytest.mark.unit
async def test_assistant_reasoning_content_is_sent_back_verbatim() -> None:
    """回传策略 tool_turns / all_when_tools 依赖：自定义字段能原样放进 messages。"""
    history: Any = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "prior thinking",
            "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
    ]
    rec = Recorder(_ok)
    await _client(rec, max_retries=0).chat.completions.create(model="m", messages=history)
    assert rec.body()["messages"][1]["reasoning_content"] == "prior thinking"


# ------------------------------------------------------------------------------
# 响应里的非标准字段
# ------------------------------------------------------------------------------


@pytest.mark.unit
async def test_reasoning_content_is_readable_from_the_message() -> None:
    rec = Recorder(_ok)
    response = await _client(rec, max_retries=0).chat.completions.create(
        model="m", messages=MESSAGES
    )
    message = response.choices[0].message
    assert getattr(message, "reasoning_content", None) == "thinking..."
    assert message.model_extra == {"reasoning_content": "thinking..."}


@pytest.mark.unit
async def test_cache_usage_fields_are_readable() -> None:
    rec = Recorder(_ok)
    response = await _client(rec, max_retries=0).chat.completions.create(
        model="m", messages=MESSAGES
    )
    usage = response.usage
    assert usage is not None
    assert usage.prompt_tokens == 10
    assert usage.model_extra == {"prompt_cache_hit_tokens": 6, "prompt_cache_miss_tokens": 4}


@pytest.mark.unit
async def test_missing_optional_fields_do_not_break_parsing() -> None:
    bare = json.loads(json.dumps(COMPLETION))
    del bare["usage"]
    del bare["choices"][0]["message"]["reasoning_content"]
    rec = Recorder(lambda _: httpx2.Response(200, json=bare))
    response = await _client(rec, max_retries=0).chat.completions.create(
        model="m", messages=MESSAGES
    )
    assert response.usage is None
    assert getattr(response.choices[0].message, "reasoning_content", None) is None


@pytest.mark.unit
async def test_tool_call_arguments_arrive_as_a_json_string() -> None:
    body = json.loads(json.dumps(COMPLETION))
    body["choices"][0]["message"] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "f", "arguments": '{"a": 1}'}}
        ],
    }
    body["choices"][0]["finish_reason"] = "tool_calls"
    rec = Recorder(lambda _: httpx2.Response(200, json=body))
    response = await _client(rec, max_retries=0).chat.completions.create(
        model="m", messages=MESSAGES
    )
    call: Any = (response.choices[0].message.tool_calls or [])[0]
    assert call.function.name == "f"
    assert isinstance(call.function.arguments, str)
    assert json.loads(call.function.arguments) == {"a": 1}


# ------------------------------------------------------------------------------
# 流式
# ------------------------------------------------------------------------------


def _sse(chunks: list[dict[str, Any]]) -> httpx2.Response:
    text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx2.Response(
        200, content=text.encode(), headers={"content-type": "text/event-stream"}
    )


def _chunk(delta: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}] if delta else [],
        **extra,
    }


@pytest.mark.unit
async def test_stream_exposes_reasoning_deltas_and_final_usage() -> None:
    rec = Recorder(
        lambda _: _sse(
            [
                _chunk({"role": "assistant", "reasoning_content": "th"}),
                _chunk({"reasoning_content": "ink"}),
                _chunk({"content": "answer"}),
                _chunk(
                    {},
                    usage=COMPLETION["usage"],
                ),
            ]
        )
    )
    stream = await _client(rec, max_retries=0).chat.completions.create(
        model="m", messages=MESSAGES, stream=True, stream_options={"include_usage": True}
    )
    reasoning, content, usage = "", "", None
    async for chunk in stream:
        if chunk.choices:
            delta = chunk.choices[0].delta
            reasoning += getattr(delta, "reasoning_content", None) or ""
            content += delta.content or ""
        if chunk.usage:
            usage = chunk.usage
    assert (reasoning, content) == ("think", "answer")
    assert usage is not None
    assert usage.model_extra is not None
    assert usage.model_extra["prompt_cache_hit_tokens"] == 6
    assert rec.body()["stream_options"] == {"include_usage": True}


# ------------------------------------------------------------------------------
# 错误
# ------------------------------------------------------------------------------


@pytest.mark.unit
async def test_rate_limit_error_exposes_retry_after() -> None:
    rec = Recorder(
        lambda _: httpx2.Response(
            429, json={"error": {"message": "slow down"}}, headers={"retry-after": "7"}
        )
    )
    with pytest.raises(openai.RateLimitError) as info:
        await _client(rec, max_retries=0).chat.completions.create(model="m", messages=MESSAGES)
    assert info.value.status_code == 429
    assert info.value.response.headers["retry-after"] == "7"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, openai.BadRequestError),
        (401, openai.AuthenticationError),
        (402, openai.APIStatusError),  # DeepSeek 余额不足；SDK 没有专门子类
        (422, openai.UnprocessableEntityError),
        (503, openai.InternalServerError),
    ],
)
async def test_status_codes_map_to_exception_classes(status: int, error: type[Exception]) -> None:
    rec = Recorder(lambda _: httpx2.Response(status, json={"error": {"message": "m"}}))
    with pytest.raises(error) as info:
        await _client(rec, max_retries=0).chat.completions.create(model="m", messages=MESSAGES)
    assert info.value.status_code == status  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_connection_failure_is_an_api_connection_error() -> None:
    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(openai.APIConnectionError):
        await _client(Recorder(boom), max_retries=0).chat.completions.create(
            model="m", messages=MESSAGES
        )


@pytest.mark.unit
async def test_timeout_is_an_api_timeout_error() -> None:
    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("too slow", request=request)

    with pytest.raises(openai.APITimeoutError):
        await _client(Recorder(slow), max_retries=0).chat.completions.create(
            model="m", messages=MESSAGES
        )

"""LLMGateway：全工程唯一接触模型 SDK 的地方。

端点内重试 → 跨端点降级 → 熔断 → 截止时间 → 并发保护。设计文档：docs/07_llm_gateway/01_gateway.md。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, NoReturn

import openai
from loguru import logger
from openai import AsyncOpenAI
from pydantic import BaseModel

from agent_runtime.core import Message, ToolCall
from agent_runtime.llm.errors import (
    EndpointFailure,
    FailureKind,
    LLMBadRequest,
    LLMDeadlineExceeded,
    LLMInvalidResponse,
    LLMUnavailableError,
)
from agent_runtime.llm.resilience import (
    AdaptiveLimiter,
    CircuitBreaker,
    backoff_delay,
    parse_retry_after,
)
from agent_runtime.llm.structured import (
    Degraded,
    Parsed,
    Rejected,
    StructuredOutcome,
    StructuredRecord,
    add_usage,
    echo,
    judge,
    path_for,
    repair_prompt,
)
from agent_runtime.llm.types import (
    CallRecord,
    GatewayTier,
    LLMRequest,
    LLMResponse,
    Outcome,
    StructuredSpec,
    TokenUsage,
)
from agent_runtime.llm.wire import ParsedCompletion, build_request_kwargs, parse_completion
from baseagent_config.models import Endpoint, LlmConfig

if TYPE_CHECKING:
    from baseagent_config import LoadedSettings

ClientFactory = Callable[[Endpoint], AsyncOpenAI]
Redactor = Callable[[str], str]

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_CONTEXT_OVERFLOW: Final = re.compile(
    r"context[ _]length|maximum context|too many tokens|context window|上下文.{0,6}(超|长)", re.I
)
_SECRET_LIKE: Final = re.compile(r"sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,}", re.I)


def default_redact(text: str) -> str:
    return _SECRET_LIKE.sub("***", text)


def api_key_for(endpoint: Endpoint, env: Mapping[str, str]) -> str:
    """本地端点缺密钥回填 ``EMPTY``；云端缺密钥是错误（带着空密钥请求只会 401）。"""
    key = env.get(endpoint.api_key_env, "").strip()
    if key:
        return key
    from urllib.parse import urlparse

    if (urlparse(endpoint.base_url).hostname or "") in _LOOPBACK:
        return "EMPTY"
    raise ValueError(f"端点 {endpoint.name} 缺少环境变量 {endpoint.api_key_env}")


def default_client_factory(env: Mapping[str, str]) -> ClientFactory:
    def factory(endpoint: Endpoint) -> AsyncOpenAI:
        # max_retries=0：重试与降级由网关自己掌控，不让 SDK 在底下偷偷再来一轮
        return AsyncOpenAI(
            api_key=api_key_for(endpoint, env),
            base_url=endpoint.base_url,
            timeout=endpoint.timeout_sec,
            max_retries=0,
        )

    return factory


@dataclass(slots=True)
class _Runtime:
    tier: str
    endpoint: Endpoint
    client: AsyncOpenAI
    breaker: CircuitBreaker
    limiter: AdaptiveLimiter
    blocked: bool = False


@dataclass(slots=True)
class _Call:
    """一次 invoke 的可变上下文。"""

    request: LLMRequest
    start: float
    attempts: int = 0
    failures: list[EndpointFailure] = field(default_factory=list[EndpointFailure])


class LLMGateway:
    def __init__(
        self,
        tiers: Mapping[str, GatewayTier],
        llm: LlmConfig,
        *,
        client_factory: ClientFactory,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        observer: Callable[[CallRecord], None] | None = None,
        structured_observer: Callable[[StructuredRecord], None] | None = None,
        redact: Redactor = default_redact,
    ) -> None:
        self._tiers = dict(tiers)
        self._cfg = llm
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._observer = observer
        self._structured_observer = structured_observer
        self._structured_counts: Counter[tuple[str, StructuredOutcome]] = Counter()
        self._redact = redact
        self._runtimes: dict[str, list[_Runtime]] = {}
        for tier_name, tier in self._tiers.items():
            self._runtimes[tier_name] = [
                self._runtime(tier_name, ep, client_factory) for ep in tier.endpoints
            ]

    @classmethod
    def from_loaded(
        cls,
        loaded: LoadedSettings,
        *,
        env: Mapping[str, str] | None = None,
        client_factory: ClientFactory | None = None,
        **kwargs: object,
    ) -> LLMGateway:
        """从加载好的配置装配。只使用缺密钥的云端端点已被剔除后的可用端点。"""
        environment = os.environ if env is None else env
        models = loaded.settings.models
        tiers = {
            tier: GatewayTier(
                temperature=getattr(models, tier).temperature,
                endpoints=loaded.endpoints[tier],
            )
            for tier in ("reasoning", "fast")
        }
        keys = [
            v.strip()
            for k, v in environment.items()
            if k.endswith(("_KEY", "_TOKEN")) and len(v.strip()) >= 8
        ]

        def redact(text: str) -> str:
            for key in keys:
                text = text.replace(key, "***")
            return default_redact(text)

        return cls(
            tiers,
            loaded.settings.llm,
            client_factory=client_factory or default_client_factory(environment),
            redact=redact,
            **kwargs,  # type: ignore[arg-type]
        )

    def _runtime(self, tier: str, ep: Endpoint, factory: ClientFactory) -> _Runtime:
        if ep.capabilities.reasoning_passback == "opaque":
            raise NotImplementedError(f"端点 {ep.name}：reasoning_passback = opaque 尚未实现")
        return _Runtime(
            tier=tier,
            endpoint=ep,
            client=factory(ep),  # 长生命周期：每个端点一个，随网关创建
            breaker=CircuitBreaker(
                open_after=self._cfg.circuit.open_after,
                open_sec=self._cfg.circuit.open_sec,
                clock=self._clock,
            ),
            limiter=AdaptiveLimiter(self._cfg.concurrency.per_endpoint),
        )

    # ------------------------------------------------------------------ 查询与维护

    def endpoint_names(self, tier: str) -> tuple[str, ...]:
        return tuple(rt.endpoint.name for rt in self._runtimes[tier])

    def concurrency_limit(self, endpoint: str) -> int:
        for runtimes in self._runtimes.values():
            for rt in runtimes:
                if rt.endpoint.name == endpoint:
                    return rt.limiter.limit
        raise KeyError(endpoint)

    @property
    def structured_counts(self) -> dict[tuple[str, StructuredOutcome], int]:
        """结构化调用的结果计数：(purpose, outcome) -> 次数。降级率由它统计。"""
        return dict(self._structured_counts)

    def reload(self) -> None:
        """清掉“鉴权失败”屏蔽与熔断状态（换了密钥或余额后调用）。"""
        for runtimes in self._runtimes.values():
            for rt in runtimes:
                rt.blocked = False
                rt.breaker.record_success()

    async def aclose(self) -> None:
        for runtimes in self._runtimes.values():
            for rt in runtimes:
                await rt.client.close()

    # ------------------------------------------------------------------ 调用

    async def invoke(self, request: LLMRequest) -> LLMResponse:
        if request.tier not in self._runtimes:
            raise ValueError(f"未知的档位：{request.tier!r}")
        if request.structured is not None and request.tools:
            raise ValueError("结构化输出不能与调用方的工具同时使用")
        call = _Call(request=request, start=self._clock())

        capable = [rt for rt in self._runtimes[request.tier] if _capable(rt.endpoint, request)]
        if not capable:
            raise LLMUnavailableError("没有满足能力要求（工具调用）的端点")

        order = [rt for rt in capable if not rt.blocked]
        if request.prefer_endpoint is not None:
            order.sort(key=lambda rt: rt.endpoint.name != request.prefer_endpoint)  # 稳定排序
        for rt in capable:
            if rt.blocked:
                call.failures.append(
                    EndpointFailure(
                        rt.endpoint.name, 0, "Blocked", "此前鉴权失败，已屏蔽直到 reload", "blocked"
                    )
                )

        skipped: list[_Runtime] = []
        attempted = False
        for position, rt in enumerate(order):
            if not rt.breaker.allow():
                skipped.append(rt)
                continue
            attempted = True
            response = await self._run_endpoint(rt, call, last=position == len(order) - 1)
            if response is not None:
                return response
        if not attempted and skipped:
            # 全部熔断：仍然试最近打开的那个，不能因为熔断而完全拒绝服务
            latest = max(skipped, key=lambda r: r.breaker.opened_at or 0.0)
            response = await self._run_endpoint(latest, call, last=True)
            if response is not None:
                return response
        self._raise_exhausted(call)

    async def _stream_once(
        self,
        rt: _Runtime,
        kwargs: dict[str, Any],
        request: LLMRequest,
        *,
        call_timeout: float,
    ) -> ParsedCompletion:
        """流式调用并交付增量；返回值与整条响应同构，重试/降级沿用外层逻辑。"""
        from agent_runtime.llm.tokens import estimate_completion_tokens, estimate_prompt_tokens

        stream = await rt.client.chat.completions.create(
            **kwargs, stream=True, timeout=call_timeout
        )
        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        finish = ""
        field = rt.endpoint.capabilities.reasoning_field

        async def delta(kind: str, text: str) -> None:
            if request.on_delta is not None:
                await request.on_delta(kind, text)

        async for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            if getattr(choice, "finish_reason", None):
                finish = str(choice.finish_reason)
            part = getattr(choice, "delta", None)
            if part is None:
                continue
            text = getattr(part, "content", None)
            if isinstance(text, str) and text:
                content.append(text)
                await delta("content", text)
            thought = getattr(part, field, None) if field else None
            if isinstance(thought, str) and thought:
                reasoning.append(thought)
                await delta("reasoning", thought)
            for item in getattr(part, "tool_calls", None) or ():
                index = int(getattr(item, "index", 0) or 0)
                slot = calls.setdefault(index, {"id": "", "name": "", "args": ""})
                call_id = getattr(item, "id", None)
                if call_id:
                    slot["id"] = str(call_id)
                function = getattr(item, "function", None)
                if function is not None:
                    name = getattr(function, "name", None)
                    if name:
                        slot["name"] = str(name)
                    arguments = getattr(function, "arguments", None)
                    if isinstance(arguments, str):
                        slot["args"] += arguments

        parsed_calls: list[ToolCall] = []
        for index in sorted(calls):
            slot = calls[index]
            raw = slot["args"] or "{}"
            try:
                args = json.loads(raw)
            except ValueError as exc:
                raise LLMInvalidResponse(f"流式工具参数不是合法 JSON：{raw[:80]}") from exc
            if not isinstance(args, dict):
                raise LLMInvalidResponse("流式工具参数必须是 JSON 对象")
            parsed_calls.append(
                ToolCall(
                    id=slot["id"] or f"call_{index + 1}",
                    name=slot["name"],
                    args=args,
                )
            )
        text_body = "".join(content)
        reason = "".join(reasoning) or None
        if not text_body and not parsed_calls:
            raise LLMInvalidResponse("流式响应既没有内容也没有工具调用")
        message = Message(
            role="assistant",
            content=text_body,
            tool_calls=tuple(parsed_calls),
            reasoning=reason,
            produced_by=rt.endpoint.name,
        )
        prompt_tokens = estimate_prompt_tokens(request.messages, request.tools)
        completion_tokens = estimate_completion_tokens(message)
        usage = TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            estimated=True,
        )
        return ParsedCompletion(message, usage, finish or "stop", {})

    def _raise_exhausted(self, call: _Call) -> NoReturn:
        failures = tuple(call.failures)
        real = [f for f in failures if f.kind != "blocked"]
        if real and all(f.kind == "bad_request" for f in real):
            raise LLMBadRequest(
                "请求被所有端点拒绝",
                failures,
                context_overflow=any(f.context_overflow for f in real),
            )
        raise LLMUnavailableError("所有端点均不可用", failures)

    def _remaining(self, request: LLMRequest) -> float | None:
        return None if request.deadline is None else request.deadline - self._clock()

    def _emit(
        self,
        call: _Call,
        rt: _Runtime,
        attempt: int,
        outcome: Outcome,
        began: float,
        **extra: object,
    ) -> None:
        if self._observer is None:
            return
        self._observer(
            CallRecord(
                purpose=call.request.purpose,
                tier=call.request.tier,
                endpoint=rt.endpoint.name,
                model=rt.endpoint.model,
                attempt=attempt,
                outcome=outcome,
                duration_ms=round((self._clock() - began) * 1000),
                **extra,  # type: ignore[arg-type]
            )
        )

    async def _run_endpoint(self, rt: _Runtime, call: _Call, *, last: bool) -> LLMResponse | None:
        """在一个端点上尝试到成功或耗尽。成功返回响应；失败记入 ``call.failures`` 返回 None。"""
        retry_cfg = self._cfg.retry
        min_attempt = self._cfg.deadline.min_attempt_sec
        prefer_schema = self._cfg.structured.prefer_schema
        kwargs = build_request_kwargs(
            rt.endpoint,
            self._tiers[rt.tier].temperature,
            call.request,
            prefer_schema=prefer_schema,
        )
        malformed_retried = False
        local = 0

        while local < retry_cfg.max_retries:
            remaining = self._remaining(call.request)
            if remaining is not None and remaining < min_attempt:
                raise LLMDeadlineExceeded("截止时间不足以再发起一次尝试", tuple(call.failures))
            timeout = (
                rt.endpoint.timeout_sec
                if remaining is None
                else min(rt.endpoint.timeout_sec, remaining)
            )

            local += 1
            call.attempts += 1
            began = self._clock()
            try:
                async with rt.limiter.slot():
                    if (
                        call.request.stream
                        and call.request.on_delta is not None
                        and rt.endpoint.capabilities.streaming
                    ):
                        parsed = await self._stream_once(
                            rt, kwargs, call.request, call_timeout=timeout
                        )
                    else:
                        completion = await rt.client.chat.completions.create(
                            **kwargs, timeout=timeout
                        )
                        parsed = parse_completion(
                            rt.endpoint,
                            completion,
                            call.request,
                            safety_factor=self._cfg.usage.estimate_safety_factor,
                            estimate_when_missing=self._cfg.usage.estimate_when_missing,
                        )
            except LLMInvalidResponse as exc:
                kind, error_class, message, retry_after, rate_limited = (
                    "transient",
                    "LLMInvalidResponse",
                    str(exc),
                    None,
                    False,
                )
                # 响应体畸形只重试一次
                retryable = not malformed_retried
                malformed_retried = True
            except openai.APIStatusError as exc:
                kind, retryable, rate_limited = _classify_status(exc.status_code)
                error_class, message = type(exc).__name__, exc.message or str(exc)
                retry_after = parse_retry_after(exc.response.headers.get("retry-after"))
            except openai.APIConnectionError as exc:
                kind, retryable, rate_limited = "transient", True, False
                error_class, message, retry_after = type(exc).__name__, str(exc), None
            else:
                rt.breaker.record_success()
                rt.limiter.on_success()
                self._emit(
                    call,
                    rt,
                    local,
                    "ok",
                    began,
                    usage=parsed.usage,
                    finish_reason=parsed.finish_reason,
                )
                return self._response(rt, call, parsed)

            message = self._redact(message)
            if rate_limited:
                rt.limiter.on_rate_limited()

            if kind == "transient" and retryable and local < retry_cfg.max_retries:
                delay = backoff_delay(
                    local,
                    factor=retry_cfg.backoff_factor,
                    max_sec=retry_cfg.backoff_max_sec,
                    jitter=retry_cfg.jitter,
                    rng=self._rng,
                    retry_after=retry_after,
                    honor_retry_after=retry_cfg.honor_retry_after,
                )
                if not (
                    retry_after is not None
                    and retry_cfg.honor_retry_after
                    and retry_after > retry_cfg.backoff_max_sec
                ):
                    left = self._remaining(call.request)
                    if left is not None and left - delay < min_attempt:
                        call.failures.append(
                            EndpointFailure(rt.endpoint.name, local, error_class, message, kind)
                        )
                        raise LLMDeadlineExceeded(
                            "等待退避后剩余时间不足以再试一次", tuple(call.failures)
                        )
                    self._emit(call, rt, local, "retry", began, error_class=error_class)
                    await self._sleep(delay)
                    continue
                # Retry-After 超过退避上限：不为一个等不到的窗口干等，放弃这个端点

            if kind == "auth":
                rt.blocked = True
            if kind == "transient":
                rt.breaker.record_failure()
            call.failures.append(
                EndpointFailure(
                    rt.endpoint.name,
                    local,
                    error_class,
                    message,
                    kind,
                    context_overflow=kind == "bad_request"
                    and bool(_CONTEXT_OVERFLOW.search(message)),
                )
            )
            self._emit(
                call, rt, local, "error" if last else "fallback", began, error_class=error_class
            )
            logger.warning(
                "端点 {} 失败（{}），{}",
                rt.endpoint.name,
                error_class,
                "已无更多端点" if last else "降级",
            )
            return None
        return None  # max_retries 为 0 不会出现（配置要求至少 1）

    def _response(self, rt: _Runtime, call: _Call, parsed: ParsedCompletion) -> LLMResponse:
        return LLMResponse(
            message=parsed.message,
            usage=parsed.usage,
            endpoint=rt.endpoint.name,
            model=rt.endpoint.model,
            finish_reason=parsed.finish_reason,
            attempts=call.attempts,
            duration_ms=round((self._clock() - call.start) * 1000),
            malformed_calls=parsed.malformed_calls,
            structured_path=path_for(
                rt.endpoint.capabilities,
                call.request,
                prefer_schema=self._cfg.structured.prefer_schema,
            ),
        )

    # ------------------------------------------------------------------ 结构化输出

    async def invoke_structured[T: BaseModel](
        self, request: LLMRequest, model: type[T]
    ) -> Parsed[T] | Degraded:
        """要求模型输出符合 ``model`` 的 JSON。校验失败带着错误重试 ``repair_retries`` 次；
        仍失败返回 ``Degraded``，由调用方按场景契约降级。网关错误（不可用、超时）照常抛出。
        """
        cfg = self._cfg.structured
        current = replace(request, structured=StructuredSpec.for_model(model))
        history: list[Message] = []
        usage: TokenUsage | None = None
        rounds = 0

        while True:
            rounds += 1
            response = await self.invoke(current)
            usage = add_usage(usage, response.usage)
            verdict = judge(response, model)
            if not isinstance(verdict, Rejected):
                self._count(request, response, "ok" if rounds == 1 else "repaired", rounds)
                return Parsed(value=verdict, response=response, usage=usage, rounds=rounds)
            if verdict.reason == "content_filter" or rounds > cfg.repair_retries:
                self._count(request, response, "degraded", rounds, verdict.reason)
                return Degraded(
                    reason=verdict.reason,
                    raw_text=verdict.raw_text,
                    problems=verdict.problems,
                    response=response,
                    usage=usage,
                    rounds=rounds,
                )

            history += [
                Message(role="assistant", content=echo(verdict.raw_text, verdict.reason)),
                Message(role="user", content=repair_prompt(verdict.reason, verdict.problems)),
            ]
            raised = current.max_output_tokens
            if verdict.reason == "truncated" and cfg.on_truncate_raise_max_tokens and raised:
                raised *= 2  # 上限由端点的 max_output_tokens 在构造请求时封顶
            current = replace(
                current,
                messages=(*request.messages, *history),
                # 格式问题不是可用性问题：回到产生坏输出的端点，不降级
                prefer_endpoint=response.endpoint,
                max_output_tokens=raised,
            )

    def _count(
        self,
        request: LLMRequest,
        response: LLMResponse,
        outcome: StructuredOutcome,
        rounds: int,
        reason: str | None = None,
    ) -> None:
        self._structured_counts[(request.purpose, outcome)] += 1
        if outcome == "degraded":
            logger.warning("结构化输出降级：{}（{}）", request.purpose, reason)
        if self._structured_observer is None:
            return
        record = StructuredRecord(
            purpose=request.purpose,
            path=response.structured_path,
            outcome=outcome,
            rounds=rounds,
            reason=reason,  # type: ignore[arg-type]
        )
        try:
            self._structured_observer(record)
        except Exception:
            logger.exception("结构化输出观测回调失败（已忽略）")


def _capable(endpoint: Endpoint, request: LLMRequest) -> bool:
    return endpoint.capabilities.tools or not request.tools


def _classify_status(code: int) -> tuple[FailureKind, bool, bool]:
    """(失败类别, 是否可重试, 是否限流)。"""
    if code == 429:
        return "transient", True, True
    if code in (408, 425) or code >= 500:
        return "transient", True, False
    if code in (401, 402, 403):
        return "auth", False, False
    return "bad_request", False, False

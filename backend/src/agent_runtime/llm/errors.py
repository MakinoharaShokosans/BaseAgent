"""网关的错误类型。设计文档：docs/07_llm_gateway/01_gateway.md §13。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agent_runtime.core import CoreError

FailureKind = Literal["transient", "bad_request", "auth", "blocked", "incapable"]


@dataclass(frozen=True, slots=True)
class EndpointFailure:
    """一个端点在本次调用里的失败摘要。``message`` 已脱敏。"""

    endpoint: str
    attempts: int
    error_class: str
    message: str
    kind: FailureKind
    context_overflow: bool = False


def _summary(failures: tuple[EndpointFailure, ...]) -> str:
    return "；".join(
        f"{f.endpoint}（{f.attempts} 次，{f.error_class}：{f.message}）" for f in failures
    )


class LLMError(CoreError):
    def __init__(self, message: str, failures: tuple[EndpointFailure, ...] = ()) -> None:
        super().__init__(f"{message}：{_summary(failures)}" if failures else message)
        self.failures = failures


class LLMUnavailableError(LLMError):
    """所有端点耗尽。任务以“模型不可用”失败，可续跑。"""


class LLMDeadlineExceeded(LLMError):
    """截止时间不足以再发起一次尝试。任务按“预算耗尽”终止。"""


class LLMBadRequest(LLMError):
    """请求本身被拒，降级也无效。``context_overflow`` 为真时是上下文超长，
    是触发任务内折叠的被动信号。"""

    def __init__(
        self,
        message: str,
        failures: tuple[EndpointFailure, ...] = (),
        context_overflow: bool = False,
    ) -> None:
        super().__init__(message, failures)
        self.context_overflow = context_overflow


class LLMInvalidResponse(LLMError):
    """响应无法解析（无 choices、空内容且无工具调用）。网关内部会重试一次。"""

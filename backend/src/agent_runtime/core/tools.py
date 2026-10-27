"""工具协议：对模型暴露的描述、工具基类、调用结果。

设计文档：docs/04_tools/01_tool_system.md §3-4。注册表与派发器在 ``tools.core``。
"""

from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

Trust = Literal["trusted", "untrusted"]


@dataclass(frozen=True, slots=True)
class ToolSchema:
    name: str
    description: str
    #: JSON Schema（对象）
    parameters: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ToolSchema.name 不能为空")
        object.__setattr__(self, "parameters", copy.deepcopy(dict(self.parameters)))


class ToolResult(BaseModel):
    """一次调用的结构化结果。编排层只读这些字段做判定，不解析 ``content`` 的自由文本。"""

    model_config = ConfigDict(frozen=True)

    ok: bool
    #: 供模型阅读的观察值
    content: str = ""
    exit_code: int | None = None
    artifact_id: str | None = None
    artifact_path: str | None = None
    is_truncated: bool = False
    duration_ms: int = 0
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict[str, Any])
    #: 调用期信任级覆盖：有些工具的输出信任级取决于本次入参（如动态子智能体）
    trust: Trust | None = None

    @classmethod
    def failure(cls, message: str, **extra: Any) -> Self:
        # 失败原因同时放进 content：模型读的是 content
        return cls(ok=False, content=message, error=message, **extra)

    @property
    def failed(self) -> bool:
        """是否计为失败（连续错误计数的依据）：``ok`` 为假，或进程类工具退出码非零。"""
        return not self.ok or (self.exit_code is not None and self.exit_code != 0)


class AgentTool(ABC):
    """所有工具（内置、MCP 转译、子智能体包装）的基类。

    业务失败返回 ``ToolResult.failure``，不抛异常；派发器会兜底，但不应依赖它。
    """

    name: str
    description: str
    #: JSON Schema（对象）
    parameters: Mapping[str, Any]
    timeout_sec: float = 60.0
    trust: Trust = "trusted"
    #: 重复执行是否安全且结果等价；崩溃恢复据此决定重试还是合成“结果未知”
    idempotent: bool = False

    @abstractmethod
    async def invoke(self, args: Mapping[str, Any]) -> ToolResult: ...

    def schema(self) -> ToolSchema:
        return ToolSchema(self.name, self.description, self.parameters)

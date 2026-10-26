"""工具层的装配错误（框架级，区别于工具的业务失败）。"""

from __future__ import annotations

from agent_runtime.core import CoreError


class ToolRegistryError(CoreError):
    """注册表构造期校验失败：空名、重名、不可信工具未获授权。"""


class DependencyUnavailableError(CoreError):
    """sidecar 等外部依赖不可用：网络错误、超时、非预期状态码、返回体不是合法 JSON 对象。"""

"""核心层的错误类型。"""

from __future__ import annotations


class CoreError(Exception):
    """核心层所有错误的基类。"""


class InvalidMessageError(CoreError, ValueError):
    """消息或工具调用的字段不合法。"""


class AtomicPairError(CoreError):
    """违反原子对不变量：带 tool_calls 的助手消息与 tool 消息没有一一配对。

    出现即视为实现缺陷，循环据此终止任务且不写检查点。
    """


class InvalidStateError(CoreError, ValueError):
    """状态不满足不变量。"""


class InvalidUpdateError(InvalidStateError):
    """阶段返回的增量不合法（未知字段、类型错误、改了初始化后不可变的字段）。"""


class StateDecodeError(CoreError, ValueError):
    """检查点内容无法解码。严格解码：宁可失败，不做猜测性修复。"""

"""循环对外的异常。设计文档：docs/02_runtime/02_main_loop.md §4。"""

from __future__ import annotations


class LoopAborted(Exception):
    """工具包装层的重复拦截连续达到上限：模型在原地打转，任务以 ``failed`` 结束，可续跑。"""


class CanaryLeak(Exception):
    """模型输出里出现了金丝雀：安全终止，状态 ``terminated``，不可续跑。"""

    def __init__(self, reason: str = "[SECURITY] Prompt leak detected via canary token") -> None:
        super().__init__(reason)
        self.reason = reason


class LLMCallTimeout(Exception):
    """单次模型调用超过 ``call_timeout_sec``。与其他模型错误同一路径：任务 ``failed``，可续跑。"""

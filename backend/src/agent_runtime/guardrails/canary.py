"""金丝雀探测（纯函数）。设计文档：docs/05_security/03_trust_and_injection.md §6.4。

只做检测：它是探测不是防御，命中时泄露已经发生。命中后的处置（终止任务）由调用的阶段负责。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def detect_canary_leak(value: Any, token: str) -> bool:
    """递归扫描字符串、字典（键与值）、列表里是否含有 ``token`` 子串。"""
    if not token:
        return False  # 空串是任何字符串的子串，不能当成泄露
    if isinstance(value, str):
        return token in value
    if isinstance(value, Mapping):
        return any(
            detect_canary_leak(k, token) or detect_canary_leak(v, token)
            for k, v in value.items()  # type: ignore[union-attr]
        )
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return any(detect_canary_leak(item, token) for item in value)  # type: ignore[union-attr]
    return False

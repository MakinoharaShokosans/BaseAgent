"""重复指纹与连续错误（纯函数）。设计文档：docs/02_runtime/06_tool_execution.md §1。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


def compute_fingerprint(tool_name: str, args: Mapping[str, Any]) -> str:
    """一次调用的确定性指纹：参数键序不影响结果，不可序列化的值也不会抛错。"""
    payload = json.dumps(
        {"tool": tool_name, "args": args}, sort_keys=True, ensure_ascii=False, default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def register_fingerprints(
    history: Sequence[str], calls: Sequence[tuple[str, Mapping[str, Any]]], keep_last: int
) -> tuple[str, ...]:
    """把本批调用按顺序追加进指纹队列，裁成定长的滑动窗口。"""
    merged = [*history, *(compute_fingerprint(name, args) for name, args in calls)]
    return tuple(merged[-keep_last:]) if keep_last > 0 else ()


def is_fingerprint_loop(history: Sequence[str], limit: int) -> bool:
    """最近 ``limit`` 次指纹完全一致。``limit <= 0`` 表示不检查。"""
    if limit <= 0 or len(history) < limit:
        return False
    return len(set(history[-limit:])) == 1


def is_alternating(history: Sequence[str], cycles: int = 3) -> bool:
    """最近的指纹在两个不同取值之间来回交替（ABABAB…），共 ``cycles`` 个周期。

    只抓“连续完全相同”抓不到的摆动：改一下、跑一下、再改回去。``cycles <= 0`` 表示不检查。
    """
    need = cycles * 2
    if cycles <= 0 or len(history) < need:
        return False
    tail = history[-need:]
    first, second = tail[0], tail[1]
    if first == second:
        return False
    return all(tail[i] == (first if i % 2 == 0 else second) for i in range(need))


def update_consecutive_errors(current: int, failures_in_batch: int) -> int:
    """成功清零、失败累加。"""
    return current + failures_in_batch if failures_in_batch > 0 else 0


def build_error_notice(consecutive_errors: int) -> str:
    """连续失败的通知。由工具包装层作为 ``user`` 消息追加在工具消息之后，只是通知，不改变控制流。"""
    return (
        f"[系统护栏] 已连续 {consecutive_errors} 次工具调用失败，请换一种做法；"
        "如果确实无法完成，说明受阻原因并结束。"
    )


@dataclass(frozen=True, slots=True)
class LoopLimits:
    #: 连续失败达到此数追加提醒通知
    consecutive_errors_limit: int
    #: 最近这么多次指纹完全一致即拦截整批
    identical_fingerprint_limit: int
    #: 指纹队列的最短保留长度；不会小于上面的阈值
    fingerprint_window: int = 6
    #: 摆动检测的周期数；0 关闭
    alternation_cycles: int = 3
    #: 连续被拦截的批次数达到它，任务以 failed 结束（LoopAborted）
    max_consecutive_blocks: int = 5

    @property
    def window(self) -> int:
        return max(
            self.fingerprint_window, self.identical_fingerprint_limit, self.alternation_cycles * 2
        )

"""重复指纹与连续错误（纯函数）。设计文档：docs/02_runtime/02_main_loop.md §5。"""

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


def update_consecutive_errors(current: int, failures_in_batch: int) -> int:
    """成功清零、失败累加。"""
    return current + failures_in_batch if failures_in_batch > 0 else 0


def build_replan_notice(consecutive_errors: int, loop_detected: bool, limit: int) -> str:
    """强制重规划的提示。由 tool_runner 作为 ``user`` 消息追加在工具消息之后。"""
    lines = ["[系统护栏] 检测到执行受阻，请停止当前策略并重新规划："]
    if loop_detected:
        lines.append("- 你连续多次提交了完全相同的工具与参数，已被拦截。请更换工具或改变思路。")
    if consecutive_errors >= limit:
        lines.append(
            f"- 工具已连续 {consecutive_errors} 次返回失败（阈值 {limit}）。"
            "请分析根因（前置条件、路径、依赖、参数），而不是继续在同一处微调。"
        )
    lines.append("- 请在下一轮给出不同的行动方案，并说明你从失败中学到了什么。")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class LoopLimits:
    #: 连续失败达到此数触发重规划通知
    consecutive_errors_limit: int
    #: 最近这么多次指纹完全一致即拦截整批
    identical_fingerprint_limit: int
    #: 指纹队列的最短保留长度；不会小于上面的阈值
    fingerprint_window: int = 5

    @property
    def window(self) -> int:
        return max(self.fingerprint_window, self.identical_fingerprint_limit)

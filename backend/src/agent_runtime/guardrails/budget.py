"""物理预算：步数、Token、挂钟三项硬熔断，达到比例时软告警。

判定是纯函数（:func:`evaluate_budget`）；:class:`PhysicalBudgetGuard` 在其上加挂钟与“每项只告警一次”
这两样随任务实例存在的状态。挂钟不入 ``AgentState``：进入状态会让回放不可复现，且续跑时
计时重新开始（否则进程重启本身就会耗尽时间预算）。

设计文档：docs/02_runtime/02_main_loop.md §5、docs/02_runtime/01_state_model.md §2。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

Dimension = Literal["steps", "tokens", "wall_time"]
DIMENSIONS: Final[tuple[Dimension, ...]] = ("steps", "tokens", "wall_time")


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    max_steps: int
    max_total_tokens: int
    max_wall_time_sec: int
    warning_ratio: float

    def __post_init__(self) -> None:
        for name in ("max_steps", "max_total_tokens", "max_wall_time_sec"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须为正，收到 {getattr(self, name)}")
        if not 0 < self.warning_ratio < 1:
            raise ValueError(f"warning_ratio 必须在 (0, 1) 内，收到 {self.warning_ratio}")


@dataclass(frozen=True, slots=True)
class Usage:
    steps: int
    tokens: int
    elapsed_sec: float


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    #: 已触达上限的维度（硬熔断）
    exhausted: tuple[Dimension, ...]
    #: 达到告警比例但未触达上限的维度
    warnings: tuple[Dimension, ...]

    def reason(self, limits: BudgetLimits, usage: Usage) -> str:
        """硬终止原因，写进 ``termination_reason``，面向用户与模型。"""
        parts = {
            "steps": f"步数达到上限（{usage.steps}/{limits.max_steps}）",
            "tokens": f"Token 达到上限（{usage.tokens}/{limits.max_total_tokens}）",
            "wall_time": (
                f"挂钟时间达到上限（{usage.elapsed_sec:.0f}/{limits.max_wall_time_sec} 秒）"
            ),
        }
        return "；".join(parts[d] for d in self.exhausted)

    def warning_text(self, limits: BudgetLimits, usage: Usage) -> str:
        """软告警正文：告诉模型已用多少、还剩多少，请它收敛。"""
        parts = {
            "steps": f"步数 {usage.steps}/{limits.max_steps}",
            "tokens": f"Token {usage.tokens}/{limits.max_total_tokens}",
            "wall_time": f"挂钟 {usage.elapsed_sec:.0f}/{limits.max_wall_time_sec} 秒",
        }
        used = "，".join(parts[d] for d in self.warnings)
        return (
            f"[预算提示] 任务预算已接近上限：{used}。"
            "请尽快收敛：优先完成最关键的目标并给出交付结论，不要开启新的探索。"
        )


def _level(value: float, limit: float, ratio: float) -> Literal["ok", "warn", "fuse"]:
    if value >= limit:
        return "fuse"
    return "warn" if value >= limit * ratio else "ok"


def evaluate_budget(limits: BudgetLimits, usage: Usage) -> BudgetVerdict:
    """纯判定。触达上限即熔断（``>=``）；已熔断的维度不再同时算告警。"""
    levels = {
        "steps": _level(usage.steps, limits.max_steps, limits.warning_ratio),
        "tokens": _level(usage.tokens, limits.max_total_tokens, limits.warning_ratio),
        "wall_time": _level(usage.elapsed_sec, limits.max_wall_time_sec, limits.warning_ratio),
    }
    return BudgetVerdict(
        exhausted=tuple(d for d in DIMENSIONS if levels[d] == "fuse"),
        warnings=tuple(d for d in DIMENSIONS if levels[d] == "warn"),
    )


class PhysicalBudgetGuard:
    """任务级实例：持有挂钟起点与已告警的维度。续跑时重建，两者都重置。"""

    def __init__(self, limits: BudgetLimits, clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits
        self._clock = clock
        self._started = clock()
        self._warned: set[Dimension] = set()

    def elapsed_sec(self) -> float:
        return self._clock() - self._started

    def remaining_wall_sec(self) -> float:
        """剩余挂钟时间，供网关计算调用截止时间；不会为负。"""
        return max(0.0, self.limits.max_wall_time_sec - self.elapsed_sec())

    def assess(self, steps: int, tokens: int) -> tuple[BudgetVerdict, Usage]:
        """判定并登记告警：每个维度的告警只给一次，熔断则每次都报告。"""
        usage = Usage(steps=steps, tokens=tokens, elapsed_sec=self.elapsed_sec())
        verdict = evaluate_budget(self.limits, usage)
        fresh: tuple[Dimension, ...] = tuple(d for d in verdict.warnings if d not in self._warned)
        self._warned.update(fresh)
        return BudgetVerdict(verdict.exhausted, fresh), usage

    def check(self, steps: int, tokens: int) -> BudgetVerdict:
        return self.assess(steps, tokens)[0]

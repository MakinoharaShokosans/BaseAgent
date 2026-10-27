"""预算账本：子智能体的准入、记账与对账。设计文档：docs/06_subagents/03_budget_ledger.md。

纯算术，没有 I/O，``reserve`` 内部没有 ``await``（单线程事件循环里天然原子）。
账本归父级持有；子级的用量只能由框架从模型响应里读出，绝不采信子级自述。

守恒式：``父任务已用（不含子级） + 子级已结算 + 在途预留 ≤ 上限``。
``sync_parent_usage`` 收到的是**不含已并入子级**的父任务用量，所以同一笔子级消耗不会被减两次
（文档 §7.1 第 2 项）；``mark_merged`` 记录已由 tool_runner 并入状态的部分。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(slots=True)
class Reservation:
    label: str
    amount: int
    settled: bool = False


class ChildBudget:
    """子智能体的自我约束：Token 与挂钟。额度来自账本**实际授予**的数，不是名义上限。"""

    def __init__(
        self, max_tokens: int, max_wall_time_sec: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.max_tokens = max_tokens
        self.max_wall_time_sec = max_wall_time_sec
        self._clock = clock
        self._started = clock()
        self.used = 0

    def add_tokens(self, n: int) -> None:
        self.used += max(int(n), 0)  # 负数按 0 计：异常值不能反过来“充值”

    @property
    def remaining(self) -> int:
        return max(self.max_tokens - self.used, 0)

    @property
    def elapsed_sec(self) -> float:
        return self._clock() - self._started

    @property
    def remaining_wall_sec(self) -> float:
        return max(self.max_wall_time_sec - self.elapsed_sec, 0.0)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0 or self.remaining_wall_sec <= 0

    def reason(self) -> str:
        if self.remaining <= 0:
            return f"Token 额度用尽（{self.used}/{self.max_tokens}）"
        if self.remaining_wall_sec <= 0:
            return f"挂钟时间用尽（{self.max_wall_time_sec:g} 秒）"
        return ""


@dataclass(slots=True)
class BudgetLedger:
    max_total_tokens: int
    max_spawns: int = 4
    min_grant_tokens: int = 1000
    parent_used: int = 0
    consumed: int = 0
    in_flight: int = 0
    spawn_count: int = 0
    merged: int = 0
    warnings: list[str] = field(default_factory=list[str])

    @property
    def available(self) -> int:
        return max(self.max_total_tokens - self.parent_used - self.consumed - self.in_flight, 0)

    def sync_parent_usage(self, state_total_tokens: int) -> None:
        """推送父任务已用量。传入的是状态里的 ``total_tokens``，已并入的子级消耗在这里扣掉。"""
        self.parent_used = max(int(state_total_tokens) - self.merged, 0)

    def mark_merged(self, child_tokens: int) -> None:
        """tool_runner 把子级消耗并入状态之后调用；此后这部分由 ``state.total_tokens`` 体现。"""
        self.merged += max(int(child_tokens), 0)

    def reserve(self, amount: int, label: str) -> tuple[Reservation | None, str]:
        if self.spawn_count >= self.max_spawns:
            return None, f"派发次数已达上限 {self.max_spawns}"
        if amount <= 0:
            return None, "申请额度必须为正"
        if self.available < self.min_grant_tokens:
            return None, f"剩余额度 {self.available} 不足最小授予额 {self.min_grant_tokens}"
        granted = min(amount, self.available)  # 收窄而不是拒绝
        if granted < self.min_grant_tokens:
            return None, f"可授予额度 {granted} 低于最小授予额 {self.min_grant_tokens}"
        self.in_flight += granted
        self.spawn_count += 1
        return Reservation(label, granted), ""

    def settle(self, reservation: Reservation, actual_tokens: int) -> int:
        """结算并返回退还额。幂等；实际超出预留时仍按实际记账（宁可多记，不可少记）。"""
        if reservation.settled:
            return 0
        reservation.settled = True
        actual = max(int(actual_tokens), 0)
        self.in_flight = max(self.in_flight - reservation.amount, 0)
        self.consumed += actual
        if actual > reservation.amount:
            self.warnings.append(
                f"{reservation.label} 实际消耗 {actual} 超出预留 {reservation.amount}，按实际记账"
            )
        return max(reservation.amount - actual, 0)

    def snapshot(self) -> dict[str, int]:
        return {
            "max_total_tokens": self.max_total_tokens,
            "parent_used": self.parent_used,
            "consumed": self.consumed,
            "in_flight": self.in_flight,
            "available": self.available,
            "spawn_count": self.spawn_count,
            "merged": self.merged,
        }

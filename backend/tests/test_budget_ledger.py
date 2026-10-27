"""预算账本。出处：docs/06_subagents/03_budget_ledger.md §3-5、§10。"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agent_runtime.guardrails import BudgetLedger, ChildBudget


def ledger(total: int = 100_000, **kw: Any) -> BudgetLedger:
    return BudgetLedger(total, **kw)


@pytest.mark.unit
def test_reserve_grants_what_was_asked_when_there_is_room() -> None:
    led = ledger()
    reservation, reason = led.reserve(20_000, "research")
    assert reservation is not None and reservation.amount == 20_000 and reason == ""
    assert (led.in_flight, led.spawn_count, led.available) == (20_000, 1, 80_000)


@pytest.mark.unit
def test_a_request_larger_than_the_balance_is_narrowed_not_refused() -> None:
    led = ledger(30_000)
    led.sync_parent_usage(20_000)
    reservation, _ = led.reserve(25_000, "x")
    assert reservation is not None and reservation.amount == 10_000


@pytest.mark.unit
def test_below_the_minimum_grant_is_refused_with_a_reason() -> None:
    led = ledger(10_000, min_grant_tokens=2_000)
    led.sync_parent_usage(9_000)
    reservation, reason = led.reserve(5_000, "x")
    assert reservation is None and "不足最小授予额" in reason
    assert (led.in_flight, led.spawn_count) == (0, 0)  # 被拒绝不留痕迹


@pytest.mark.unit
def test_spawn_count_is_capped() -> None:
    led = ledger(max_spawns=2)
    for _ in range(2):
        assert led.reserve(1_000, "x")[0] is not None
    reservation, reason = led.reserve(1_000, "x")
    assert reservation is None and "派发次数已达上限 2" in reason


@pytest.mark.unit
@pytest.mark.parametrize("amount", [0, -5])
def test_non_positive_requests_are_refused(amount: int) -> None:
    assert ledger().reserve(amount, "x")[0] is None


@pytest.mark.unit
def test_settle_refunds_the_difference_and_is_idempotent() -> None:
    led = ledger()
    reservation, _ = led.reserve(10_000, "x")
    assert reservation is not None
    assert led.settle(reservation, 4_000) == 6_000
    assert (led.in_flight, led.consumed) == (0, 4_000)
    assert led.settle(reservation, 9_999) == 0  # 重复结算无副作用
    assert (led.in_flight, led.consumed) == (0, 4_000)


@pytest.mark.unit
def test_overspend_is_booked_in_full_and_warned() -> None:
    led = ledger()
    reservation, _ = led.reserve(5_000, "research")
    assert reservation is not None
    assert led.settle(reservation, 7_000) == 0
    assert led.consumed == 7_000 and "超出预留" in led.warnings[0]


@pytest.mark.unit
def test_negative_usage_cannot_recharge_the_budget() -> None:
    led = ledger()
    reservation, _ = led.reserve(5_000, "x")
    assert reservation is not None
    led.settle(reservation, -500)
    assert led.consumed == 0
    child = ChildBudget(1000, 10)
    child.add_tokens(-100)
    assert child.used == 0


@pytest.mark.unit
def test_concurrent_reservations_never_exceed_the_balance() -> None:
    led = ledger(10_000, min_grant_tokens=1_000)
    granted = [led.reserve(4_000, f"c{i}")[0] for i in range(5)]
    assert sum(r.amount for r in granted if r) <= 10_000
    assert [r.amount for r in granted if r] == [4_000, 4_000, 2_000]


@pytest.mark.unit
def test_child_usage_is_not_charged_twice_after_it_is_merged_into_the_state() -> None:
    """文档 §7.1 第 2 项：第二次派发前，已并入状态的子级消耗不能再从余额里扣一次。"""
    led = ledger(100_000)
    led.sync_parent_usage(10_000)
    first, _ = led.reserve(20_000, "a")
    assert first is not None
    led.settle(first, 15_000)
    state_total = 10_000 + 15_000  # tool_runner 把增量并入状态
    led.mark_merged(15_000)
    led.sync_parent_usage(state_total)
    assert led.parent_used == 10_000
    assert led.available == 100_000 - 10_000 - 15_000  # 子级的 15_000 只算一次


@pytest.mark.unit
def test_a_rebuilt_ledger_starts_clean_and_aligned_with_the_state() -> None:
    led = ledger(100_000)
    led.sync_parent_usage(40_000)  # 续跑：状态里已含此前并入的子级消耗，账本从零重建
    assert (led.parent_used, led.consumed, led.available) == (40_000, 0, 60_000)


@given(
    st.lists(st.tuples(st.integers(1, 30_000), st.integers(0, 40_000)), min_size=1, max_size=12),
    st.integers(5_000, 200_000),
)
@settings(max_examples=80, deadline=None)
def test_the_conservation_law_holds_for_any_sequence(
    plan: list[tuple[int, int]], total: int
) -> None:
    led = BudgetLedger(total, max_spawns=50, min_grant_tokens=500)
    parent = 0
    for ask, spend in plan:
        led.sync_parent_usage(parent + led.merged)
        reservation, _ = led.reserve(ask, "x")
        assert led.parent_used + led.consumed + led.in_flight <= total or led.available == 0
        if reservation is None:
            continue
        assert reservation.amount <= ask
        used = min(spend, reservation.amount)  # 受自我约束的子级不会超过授予额
        led.settle(reservation, used)
        led.mark_merged(used)
        assert led.in_flight == 0
        assert led.available >= 0


@pytest.mark.unit
def test_child_budget_exhaustion_by_tokens_and_by_wall_clock() -> None:
    now = {"t": 0.0}
    child = ChildBudget(1000, 30, clock=lambda: now["t"])
    child.add_tokens(400)
    assert child.remaining == 600 and not child.exhausted and child.reason() == ""
    child.add_tokens(700)
    assert child.exhausted and "Token" in child.reason() and child.remaining == 0
    slow = ChildBudget(1000, 30, clock=lambda: now["t"])
    now["t"] = 31.0
    assert slow.exhausted and "挂钟" in slow.reason()

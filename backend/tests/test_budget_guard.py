"""物理预算守卫：步数、Token、挂钟三项硬熔断与 90% 软告警。

出处：docs/02_runtime/02_main_loop.md §5、docs/02_runtime/01_state_model.md §2。
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_runtime.guardrails import (
    BudgetLimits,
    BudgetVerdict,
    PhysicalBudgetGuard,
    Usage,
    evaluate_budget,
)

LIMITS = BudgetLimits(
    max_steps=25, max_total_tokens=200_000, max_wall_time_sec=900, warning_ratio=0.9
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def usage(steps: int = 0, tokens: int = 0, elapsed: float = 0.0) -> Usage:
    return Usage(steps=steps, tokens=tokens, elapsed_sec=elapsed)


# ==============================================================================
# 限额本身
# ==============================================================================


@pytest.mark.unit
@pytest.mark.parametrize("field", ["max_steps", "max_total_tokens", "max_wall_time_sec"])
@pytest.mark.parametrize("bad", [0, -1])
def test_limits_must_be_positive(field: str, bad: int) -> None:
    values = {
        "max_steps": 25,
        "max_total_tokens": 1000,
        "max_wall_time_sec": 60,
        "warning_ratio": 0.9,
    }
    with pytest.raises(ValueError, match=field):
        BudgetLimits(**{**values, field: bad})  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize("ratio", [0, 1, 1.5, -0.1])
def test_warning_ratio_must_be_inside_zero_and_one(ratio: float) -> None:
    with pytest.raises(ValueError, match="warning_ratio"):
        BudgetLimits(max_steps=1, max_total_tokens=1, max_wall_time_sec=1, warning_ratio=ratio)


# ==============================================================================
# 纯判定
# ==============================================================================


@pytest.mark.unit
def test_plenty_of_budget_is_quiet() -> None:
    assert evaluate_budget(LIMITS, usage(2, 1000, 10)) == BudgetVerdict(exhausted=(), warnings=())


@pytest.mark.unit
def test_zero_usage_is_quiet() -> None:
    verdict = evaluate_budget(LIMITS, usage())
    assert not verdict.exhausted and not verdict.warnings


@pytest.mark.unit
@pytest.mark.parametrize(
    ("dimension", "just_below", "at_warning", "at_limit"),
    [
        ("steps", usage(steps=22), usage(steps=23), usage(steps=25)),  # 25 × 0.9 = 22.5
        ("tokens", usage(tokens=179_999), usage(tokens=180_000), usage(tokens=200_000)),
        ("wall_time", usage(elapsed=809.9), usage(elapsed=810.0), usage(elapsed=900.0)),
    ],
)
def test_each_dimension_warns_at_the_ratio_and_fuses_at_the_limit(
    dimension: str, just_below: Usage, at_warning: Usage, at_limit: Usage
) -> None:
    assert evaluate_budget(LIMITS, just_below) == BudgetVerdict((), ())
    assert evaluate_budget(LIMITS, at_warning) == BudgetVerdict((), (dimension,))  # type: ignore[arg-type]
    assert evaluate_budget(LIMITS, at_limit) == BudgetVerdict((dimension,), ())  # type: ignore[arg-type]


@pytest.mark.unit
def test_going_past_the_limit_still_fuses() -> None:
    verdict = evaluate_budget(LIMITS, usage(steps=40, tokens=999_999, elapsed=5000))
    assert set(verdict.exhausted) == {"steps", "tokens", "wall_time"}


@pytest.mark.unit
def test_dimensions_are_independent() -> None:
    verdict = evaluate_budget(LIMITS, usage(steps=25, tokens=185_000))
    assert verdict.exhausted == ("steps",)
    assert verdict.warnings == ("tokens",)


@pytest.mark.unit
def test_an_exhausted_dimension_is_not_also_warned() -> None:
    verdict = evaluate_budget(LIMITS, usage(steps=25))
    assert "steps" not in verdict.warnings


@pytest.mark.unit
@given(
    st.integers(0, 60),
    st.integers(0, 500_000),
    st.floats(0, 2000, allow_nan=False),
    st.integers(0, 10),
    st.integers(0, 100_000),
    st.floats(0, 500, allow_nan=False),
)
def test_more_usage_never_makes_a_dimension_look_better(
    steps: int, tokens: int, elapsed: float, d_steps: int, d_tokens: int, d_elapsed: float
) -> None:
    rank = {"ok": 0, "warn": 1, "fuse": 2}

    def level(verdict: BudgetVerdict, dimension: str) -> int:
        if dimension in verdict.exhausted:
            return rank["fuse"]
        return rank["warn"] if dimension in verdict.warnings else rank["ok"]

    before = evaluate_budget(LIMITS, usage(steps, tokens, elapsed))
    after = evaluate_budget(LIMITS, usage(steps + d_steps, tokens + d_tokens, elapsed + d_elapsed))
    for dimension in ("steps", "tokens", "wall_time"):
        assert level(after, dimension) >= level(before, dimension)


@pytest.mark.unit
def test_termination_reason_names_the_dimension_and_the_numbers() -> None:
    reason = evaluate_budget(LIMITS, usage(steps=25, tokens=201_000)).reason(
        LIMITS, usage(steps=25, tokens=201_000)
    )
    assert "步数" in reason and "25/25" in reason
    assert "Token" in reason and "201000/200000" in reason
    assert "挂钟" not in reason


@pytest.mark.unit
def test_wall_time_reason_is_in_seconds() -> None:
    u = usage(elapsed=901.4)
    assert "挂钟" in evaluate_budget(LIMITS, u).reason(LIMITS, u)
    assert "900" in evaluate_budget(LIMITS, u).reason(LIMITS, u)


# ==============================================================================
# 守卫对象：挂钟与“只告警一次”
# ==============================================================================


def make_guard(clock: FakeClock, limits: BudgetLimits = LIMITS) -> PhysicalBudgetGuard:
    return PhysicalBudgetGuard(limits, clock=clock)


@pytest.mark.unit
def test_wall_clock_starts_when_the_guard_is_created() -> None:
    clock = FakeClock()
    guard = make_guard(clock)
    clock.advance(100)
    assert guard.elapsed_sec() == 100
    assert guard.remaining_wall_sec() == 800


@pytest.mark.unit
def test_remaining_wall_time_never_goes_negative() -> None:
    clock = FakeClock()
    guard = make_guard(clock)
    clock.advance(5000)
    assert guard.remaining_wall_sec() == 0


@pytest.mark.unit
def test_check_fuses_on_wall_time_without_any_other_usage() -> None:
    clock = FakeClock()
    guard = make_guard(clock)
    clock.advance(900)
    assert guard.check(steps=0, tokens=0).exhausted == ("wall_time",)


@pytest.mark.unit
def test_each_warning_is_given_once() -> None:
    clock = FakeClock()
    guard = make_guard(clock)
    assert guard.check(steps=23, tokens=0).warnings == ("steps",)
    assert guard.check(steps=23, tokens=0).warnings == ()
    assert guard.check(steps=24, tokens=0).warnings == ()
    # 另一项第一次越线时仍会告警
    assert guard.check(steps=24, tokens=185_000).warnings == ("tokens",)


@pytest.mark.unit
def test_exhaustion_is_reported_every_time() -> None:
    guard = make_guard(FakeClock())
    assert guard.check(steps=25, tokens=0).exhausted == ("steps",)
    assert guard.check(steps=25, tokens=0).exhausted == ("steps",)


@pytest.mark.unit
def test_a_new_guard_starts_fresh() -> None:
    """续跑：守卫随任务重建，挂钟重新计时，告警状态也重置。"""
    clock = FakeClock()
    first = make_guard(clock)
    assert first.check(steps=23, tokens=0).warnings == ("steps",)
    clock.advance(5000)
    assert first.check(steps=0, tokens=0).exhausted == ("wall_time",)

    second = make_guard(clock)
    assert second.check(steps=0, tokens=0).exhausted == ()
    assert second.check(steps=23, tokens=0).warnings == ("steps",)


@pytest.mark.unit
def test_default_clock_is_monotonic_and_real() -> None:
    guard = PhysicalBudgetGuard(LIMITS)
    assert 0 <= guard.elapsed_sec() < 5

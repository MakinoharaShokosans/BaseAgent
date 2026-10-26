"""退避、熔断器、自适应并发。出处：docs/07_llm_gateway/01_gateway.md §5.2、§5.4、§7。"""

from __future__ import annotations

import asyncio

import pytest

from agent_runtime.llm.resilience import (
    AdaptiveLimiter,
    CircuitBreaker,
    backoff_delay,
    parse_retry_after,
)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def delay(attempt: int, **kw: object) -> float:
    base: dict[str, object] = {
        "factor": 2.0,
        "max_sec": 30.0,
        "jitter": 0.0,
        "rng": lambda: 0.0,
        "retry_after": None,
        "honor_retry_after": True,
    }
    return backoff_delay(attempt, **{**base, **kw})  # type: ignore[arg-type]


# ==============================================================================
# 退避
# ==============================================================================


@pytest.mark.unit
def test_backoff_grows_exponentially_from_the_factor() -> None:
    assert [delay(n) for n in (1, 2, 3, 4)] == [2.0, 4.0, 8.0, 16.0]


@pytest.mark.unit
def test_backoff_is_capped() -> None:
    assert delay(5) == 30.0
    assert delay(50) == 30.0


@pytest.mark.unit
def test_jitter_adds_a_random_fraction() -> None:
    assert delay(1, jitter=0.2, rng=lambda: 0.5) == pytest.approx(2.2)
    assert delay(1, jitter=0.2, rng=lambda: 1.0) == pytest.approx(2.4)
    assert delay(1, jitter=0.2, rng=lambda: 0.0) == 2.0


@pytest.mark.unit
def test_the_cap_applies_after_jitter() -> None:
    assert delay(5, jitter=0.5, rng=lambda: 1.0) == 30.0


@pytest.mark.unit
def test_retry_after_wins_when_larger_and_is_ignored_when_smaller() -> None:
    assert delay(1, retry_after=10.0) == 10.0
    assert delay(3, retry_after=1.0) == 8.0


@pytest.mark.unit
def test_retry_after_can_be_switched_off() -> None:
    assert delay(1, retry_after=10.0, honor_retry_after=False) == 2.0


@pytest.mark.unit
def test_retry_after_is_not_capped_so_the_caller_can_decide_to_give_up() -> None:
    assert delay(1, retry_after=120.0) == 120.0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("7", 7.0), ("0", 0.0), ("2.5", 2.5), (" 3 ", 3.0), (None, None), ("", None),
     ("soon", None), ("-5", None), ("Wed, 21 Oct 2026 07:28:00 GMT", None)],
)  # fmt: skip
def test_retry_after_header_parsing(raw: str | None, expected: float | None) -> None:
    assert parse_retry_after(raw) == expected


# ==============================================================================
# 熔断器
# ==============================================================================


def breaker(clock: Clock, open_after: int = 2, open_sec: float = 60) -> CircuitBreaker:
    return CircuitBreaker(open_after=open_after, open_sec=open_sec, clock=clock)


@pytest.mark.unit
def test_closed_breaker_lets_everything_through() -> None:
    b = breaker(Clock())
    assert b.state == "closed" and b.allow() and b.allow()


@pytest.mark.unit
def test_it_opens_after_n_consecutive_failures() -> None:
    b = breaker(Clock())
    b.record_failure()
    assert b.state == "closed"
    b.record_failure()
    assert b.state == "open"
    assert b.allow() is False


@pytest.mark.unit
def test_a_success_resets_the_failure_count() -> None:
    b = breaker(Clock())
    b.record_failure()
    b.record_success()
    b.record_failure()
    assert b.state == "closed"


@pytest.mark.unit
def test_after_the_window_exactly_one_probe_is_let_through() -> None:
    clock = Clock()
    b = breaker(clock)
    b.record_failure()
    b.record_failure()
    clock.now += 59.9
    assert b.allow() is False
    clock.now += 0.2
    assert b.allow() is True  # 探测
    assert b.state == "half_open"
    assert b.allow() is False  # 探测在途，其余仍被挡住


@pytest.mark.unit
def test_a_successful_probe_closes_it() -> None:
    clock = Clock()
    b = breaker(clock)
    b.record_failure()
    b.record_failure()
    clock.now += 61
    assert b.allow()
    b.record_success()
    assert b.state == "closed" and b.allow() and b.allow()


@pytest.mark.unit
def test_a_failed_probe_reopens_for_another_full_window() -> None:
    clock = Clock()
    b = breaker(clock)
    b.record_failure()
    b.record_failure()
    clock.now += 61
    assert b.allow()
    b.record_failure()
    assert b.state == "open"
    clock.now += 30
    assert b.allow() is False
    clock.now += 31
    assert b.allow() is True


@pytest.mark.unit
def test_opened_at_is_recorded_for_the_all_open_fallback() -> None:
    clock = Clock()
    b = breaker(clock)
    assert b.opened_at is None
    b.record_failure()
    b.record_failure()
    assert b.opened_at == 100.0


@pytest.mark.unit
def test_n_of_one_opens_on_the_first_failure() -> None:
    b = breaker(Clock(), open_after=1)
    b.record_failure()
    assert b.state == "open"


# ==============================================================================
# 自适应并发
# ==============================================================================


@pytest.mark.unit
async def test_limiter_bounds_concurrency() -> None:
    limiter = AdaptiveLimiter(2)
    running = peak = 0

    async def work() -> None:
        nonlocal running, peak
        async with limiter.slot():
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

    await asyncio.gather(*(work() for _ in range(8)))
    assert peak == 2
    assert limiter.in_flight == 0


@pytest.mark.unit
async def test_rate_limiting_halves_the_limit_down_to_one() -> None:
    limiter = AdaptiveLimiter(4)
    limiter.on_rate_limited()
    assert limiter.limit == 2
    limiter.on_rate_limited()
    assert limiter.limit == 1
    limiter.on_rate_limited()
    assert limiter.limit == 1


@pytest.mark.unit
async def test_successes_give_the_limit_back_gradually() -> None:
    limiter = AdaptiveLimiter(4)
    limiter.on_rate_limited()  # → 2
    limiter.on_success()
    assert limiter.limit == 2  # 需要连续成功“当前并发数”次才放回一格
    limiter.on_success()
    assert limiter.limit == 3
    for _ in range(10):
        limiter.on_success()
    assert limiter.limit == 4  # 不超过配置值


@pytest.mark.unit
async def test_a_rate_limit_resets_the_recovery_streak() -> None:
    limiter = AdaptiveLimiter(4)
    limiter.on_rate_limited()
    limiter.on_success()
    limiter.on_rate_limited()  # → 1，连胜清零
    assert limiter.limit == 1
    limiter.on_success()
    assert limiter.limit == 2


@pytest.mark.unit
async def test_a_reduced_limit_takes_effect_for_new_work() -> None:
    limiter = AdaptiveLimiter(3)
    limiter.on_rate_limited()  # → 1
    running = peak = 0

    async def work() -> None:
        nonlocal running, peak
        async with limiter.slot():
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

    await asyncio.gather(*(work() for _ in range(5)))
    assert peak == 1


@pytest.mark.unit
async def test_slot_is_released_when_the_work_fails() -> None:
    limiter = AdaptiveLimiter(1)
    with pytest.raises(RuntimeError):
        async with limiter.slot():
            raise RuntimeError("boom")
    assert limiter.in_flight == 0
    async with limiter.slot():
        pass


@pytest.mark.unit
def test_limiter_needs_a_positive_limit() -> None:
    with pytest.raises(ValueError, match="limit"):
        AdaptiveLimiter(0)

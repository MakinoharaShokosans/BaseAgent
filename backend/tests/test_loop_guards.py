"""重复指纹与连续错误护栏（纯函数）。出处：docs/02_runtime/02_main_loop.md §5。"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_runtime.guardrails import (
    build_replan_notice,
    compute_fingerprint,
    is_alternating,
    is_fingerprint_loop,
    register_fingerprints,
    update_consecutive_errors,
)

# ==============================================================================
# 指纹
# ==============================================================================


@pytest.mark.unit
def test_the_fingerprint_ignores_key_order_and_separates_tools() -> None:
    a = compute_fingerprint("bash", {"command": "ls", "timeout": 5})
    assert a == compute_fingerprint("bash", {"timeout": 5, "command": "ls"})
    assert a != compute_fingerprint("sh", {"command": "ls", "timeout": 5})
    assert a != compute_fingerprint("bash", {"command": "ls", "timeout": 6})


@pytest.mark.unit
def test_the_tool_name_and_arguments_cannot_be_confused() -> None:
    assert compute_fingerprint("a", {"b": 1}) != compute_fingerprint("a", {"b": "1"})
    assert compute_fingerprint("ab", {}) != compute_fingerprint("a", {"b": None})


@pytest.mark.unit
def test_odd_values_do_not_break_the_fingerprint() -> None:
    from pathlib import Path

    assert compute_fingerprint("t", {"p": Path("/x"), "s": {1}})


@pytest.mark.unit
@given(st.dictionaries(st.text(max_size=5), st.integers() | st.text(max_size=5), max_size=5))
def test_the_fingerprint_is_stable_under_reinsertion(args: dict[str, object]) -> None:
    reordered = dict(reversed(list(args.items())))
    assert compute_fingerprint("t", args) == compute_fingerprint("t", reordered)


@pytest.mark.unit
def test_registering_appends_in_order_and_keeps_a_sliding_window() -> None:
    history: tuple[str, ...] = ()
    for i in range(8):
        history = register_fingerprints(history, [("t", {"i": i})], keep_last=5)
    assert len(history) == 5
    assert history[-1] == compute_fingerprint("t", {"i": 7})
    assert history[0] == compute_fingerprint("t", {"i": 3})


@pytest.mark.unit
def test_a_batch_registers_every_call_in_order() -> None:
    history = register_fingerprints((), [("a", {}), ("b", {})], keep_last=5)
    assert history == (compute_fingerprint("a", {}), compute_fingerprint("b", {}))


@pytest.mark.unit
def test_registering_does_not_mutate_its_input() -> None:
    base = ("x",)
    register_fingerprints(base, [("a", {})], keep_last=5)
    assert base == ("x",)


@pytest.mark.unit
def test_the_loop_needs_limit_identical_entries_at_the_tail() -> None:
    same = compute_fingerprint("t", {})
    other = compute_fingerprint("u", {})
    assert is_fingerprint_loop((same, same, same), 3) is True
    assert is_fingerprint_loop((same, same), 3) is False
    assert is_fingerprint_loop((other, same, same), 3) is False
    assert is_fingerprint_loop((other, same, same, same), 3) is True
    assert is_fingerprint_loop((same, same, other), 3) is False


@pytest.mark.unit
def test_a_non_positive_limit_disables_the_loop_check() -> None:
    same = compute_fingerprint("t", {})
    assert is_fingerprint_loop((same,) * 10, 0) is False


# ==============================================================================
# 连续错误
# ==============================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    ("current", "failures", "expected"),
    [(0, 0, 0), (2, 0, 0), (0, 1, 1), (2, 1, 3), (1, 3, 4)],
)
def test_success_resets_and_failure_accumulates(current: int, failures: int, expected: int) -> None:
    assert update_consecutive_errors(current, failures) == expected


@pytest.mark.unit
def test_the_notice_explains_what_triggered_it() -> None:
    loop = build_replan_notice(0, True, 3)
    assert "完全相同" in loop
    errors = build_replan_notice(3, False, 3)
    assert "连续 3 次" in errors
    both = build_replan_notice(4, True, 3)
    assert "完全相同" in both and "连续 4 次" in both
    assert "完全相同" not in errors
    assert "阈值" not in loop


# ==============================================================================
# 摆动（ABAB…）
# ==============================================================================


@pytest.mark.unit
def test_an_alternating_pair_is_a_loop_after_three_cycles() -> None:
    assert is_alternating(list("ABABAB")) is True
    assert is_alternating(list("xxABABAB")) is True  # 只看最近的 6 条
    assert is_alternating(list("BABABA"), cycles=3) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "history",
    ["ABABA", "ABABAC", "AAAAAA", "ABCABC", "AABBAA", "ABABAB"[:4]],
)
def test_other_shapes_are_not_alternation(history: str) -> None:
    assert is_alternating(list(history)) is False


@pytest.mark.unit
def test_alternation_can_be_switched_off_or_tightened() -> None:
    assert is_alternating(list("ABABAB"), cycles=0) is False
    assert is_alternating(list("ABAB"), cycles=2) is True


@pytest.mark.unit
@given(st.lists(st.text(min_size=1, max_size=3), max_size=20), st.integers(0, 5))
def test_alternation_never_raises(history: list[str], cycles: int) -> None:
    assert isinstance(is_alternating(history, cycles), bool)

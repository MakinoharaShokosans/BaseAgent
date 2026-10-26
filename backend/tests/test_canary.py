"""金丝雀探测。出处：docs/05_security/03_trust_and_injection.md §6.4。"""

from __future__ import annotations

import pytest

from agent_runtime.guardrails import detect_canary_leak

TOKEN = "canary_0123456789ab"


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        f"前面 {TOKEN} 后面",
        {"query": TOKEN},
        {TOKEN: 1},  # 键也要扫
        [1, ["x", {"deep": [f"a{TOKEN}b"]}]],
        {"a": {"b": {"c": [TOKEN]}}},
    ],
)
def test_a_token_anywhere_in_the_structure_is_found(value: object) -> None:
    assert detect_canary_leak(value, TOKEN) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "value", ["", "canary_0123456789a", {"a": [1, None, True]}, [], 12, None, "CANARY_0123456789AB"]
)
def test_clean_values_are_clean(value: object) -> None:
    assert detect_canary_leak(value, TOKEN) is False


@pytest.mark.unit
def test_an_empty_token_never_matches() -> None:
    """没有金丝雀时不能把“任何字符串都包含空串”当成泄露。"""
    assert detect_canary_leak("anything", "") is False

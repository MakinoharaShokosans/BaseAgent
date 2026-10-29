"""任务内旧观察值折叠（装配视图）。出处：02_window_and_compaction.md §3.2、§3.2.1。"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agent_runtime.context import FoldPolicy, fold_stream
from agent_runtime.core import Message, ToolCall, assert_atomic_pairs


def step(
    i: int, name: str = "bash", size: int = 100, handle: bool = True, args: Any = None
) -> list[Message]:
    body = "\n".join(f"line {n}" for n in range(3)) + "x" * size
    if handle:
        body += f"\n[完整输出已离线保存: artifact://obs_step_{i}_{name}]"
    return [
        Message(
            role="assistant",
            content="",
            tool_calls=(ToolCall(id=f"c{i}", name=name, args=args or {"command": f"cmd{i}"}),),
        ),
        Message(role="tool", content=body, tool_call_id=f"c{i}", name=name),
    ]


def stream(n: int, **kw: Any) -> list[Message]:
    return [m for i in range(1, n + 1) for m in step(i, **kw)]


def policy(
    trigger: int = 300, keep: int = 2, exempt: tuple[str, ...] = ("load_skill",)
) -> FoldPolicy:
    return FoldPolicy(
        trigger_tokens=trigger, keep_recent_steps=keep, exempt_tools=frozenset(exempt)
    )


def run(msgs: list[Message], base: int = 0, p: FoldPolicy | None = None):  # type: ignore[no-untyped-def]
    return fold_stream(msgs, base_tokens=base, policy=p or policy(), count=len)


def is_stub(m: Message) -> bool:
    return m.content.startswith("[已折叠]")


# ==============================================================================
# 触发
# ==============================================================================


@pytest.mark.unit
def test_below_the_trigger_nothing_changes() -> None:
    msgs = stream(2, size=10)
    out = run(msgs, p=policy(trigger=10_000))
    assert out.messages == msgs and out.folded == 0


@pytest.mark.unit
def test_the_base_size_counts_toward_the_trigger() -> None:
    msgs = stream(4, size=50)
    assert run(msgs, base=0, p=policy(trigger=2_000)).folded == 0
    assert run(msgs, base=2_000, p=policy(trigger=2_000)).folded > 0


@pytest.mark.unit
def test_old_observations_become_stubs_and_the_recent_steps_stay_whole() -> None:
    msgs = stream(6)
    out = run(msgs, p=policy(trigger=400, keep=2)).messages
    tools = [m for m in out if m.role == "tool"]
    assert all(is_stub(m) for m in tools[:4]) and not any(is_stub(m) for m in tools[4:])
    assert out[-2:] == msgs[-2:] and out[-4:-2] == msgs[-4:-2]


@pytest.mark.unit
def test_structure_is_untouched_so_pairs_stay_atomic() -> None:
    msgs = stream(6)
    out = run(msgs, p=policy(trigger=400)).messages
    assert len(out) == len(msgs)
    for a, b in zip(msgs, out, strict=True):
        assert (a.role, a.tool_calls, a.tool_call_id, a.name) == (
            b.role,
            b.tool_calls,
            b.tool_call_id,
            b.name,
        )
    assert_atomic_pairs(out)


@pytest.mark.unit
def test_non_tool_messages_are_never_touched() -> None:
    msgs = [
        Message(role="assistant", content="先做这个"),
        *stream(5),
        Message(role="user", content="【系统护栏】换个思路"),
    ]
    out = run(msgs, p=policy(trigger=300, keep=1)).messages
    assert out[0] == msgs[0] and out[-1] == msgs[-1]


# ==============================================================================
# 存根
# ==============================================================================


@pytest.mark.unit
def test_the_stub_names_the_tool_the_call_the_size_and_the_handle() -> None:
    out = run(stream(5), p=policy(trigger=300, keep=1)).messages
    stub = next(m for m in out if m.role == "tool")
    assert stub.content == (
        "[已折叠] bash: cmd1（4 行，"
        f"{len(stream(5)[1].content)} 字符）→ 完整输出见产物 artifact://obs_step_1_bash，"
        "可用 read_artifact 取回"
    )


@pytest.mark.unit
def test_the_stub_is_deterministic_and_has_no_clock() -> None:
    a = run(stream(5), p=policy(trigger=300)).messages
    b = run(stream(5), p=policy(trigger=300)).messages
    assert a == b


@pytest.mark.unit
@pytest.mark.parametrize(
    ("args", "shown"),
    [
        ({"command": "pytest -x\n-q"}, "pytest -x -q"),
        ({"path": "src/a.py", "start_line": 3}, '"path": "src/a.py"'),
        ({"command": "x" * 500}, "x" * 80 + "…"),
    ],
)
def test_the_call_is_summarised_on_one_short_line(args: dict[str, Any], shown: str) -> None:
    msgs = step(1, size=1000, args=args) + stream(4, size=1000)[2:]
    stub = next(m for m in run(msgs, p=policy(trigger=300, keep=1)).messages if m.role == "tool")
    assert shown in stub.content and "\n" not in stub.content


# ==============================================================================
# 什么不折叠
# ==============================================================================


@pytest.mark.unit
def test_an_observation_without_an_offline_copy_is_kept_because_folding_would_lose_it() -> None:
    msgs = stream(6, handle=False)
    out = run(msgs, p=policy(trigger=300, keep=1))
    assert out.messages == msgs and out.folded == 0


@pytest.mark.unit
def test_exempt_tools_are_never_folded() -> None:
    msgs = stream(2, name="load_skill") + stream(6)[2:]
    # load_skill 的 SOP 带着句柄也不折叠：截断流程文档会让做法残缺
    out = run(msgs, p=policy(trigger=300, keep=1)).messages
    assert not is_stub(out[1])


@pytest.mark.unit
def test_a_stub_that_would_not_be_shorter_is_not_used() -> None:
    msgs = stream(6, size=0)  # 原文本来就比存根短
    out = run(msgs, p=policy(trigger=1, keep=1))
    assert out.messages == msgs


@pytest.mark.unit
def test_fewer_steps_than_the_keep_window_means_nothing_to_fold() -> None:
    msgs = stream(2)
    assert run(msgs, p=policy(trigger=1, keep=3)).messages == msgs


# ==============================================================================
# 成批与滞回
# ==============================================================================


def folded_flags(msgs: list[Message], p: FoldPolicy, base: int = 0) -> list[bool]:
    return [is_stub(m) for m in run(msgs, base, p).messages if m.role == "tool"]


@pytest.mark.unit
def test_folding_happens_in_batches_not_one_message_per_step() -> None:
    full = stream(40, size=1000)
    p = policy(trigger=12_000, keep=2)
    counts = []
    for n in range(1, 41):
        counts.append(sum(folded_flags(full[: 2 * n], p)))
    changes = sum(1 for a, b in pairwise(counts) if b != a)
    assert counts == sorted(counts) and counts[-1] > 10
    assert changes < 10  # 40 步里只发生少数几次折叠，而不是每步一次


@pytest.mark.unit
def test_once_folded_it_stays_folded_as_the_task_grows() -> None:
    full = stream(30, size=1000)
    p = policy(trigger=8_000, keep=2)
    previous: list[bool] = []
    for n in range(1, 31):
        now = folded_flags(full[: 2 * n], p)
        assert now[: len(previous)] >= previous  # 逐位不回退
        previous = now


@pytest.mark.unit
def test_after_a_fold_the_view_is_back_under_the_trigger() -> None:
    full = stream(10, size=1000)
    p = policy(trigger=6_000, keep=2)
    view = run(full, p=p).messages
    assert sum(len(m.content) for m in view) < 6_000  # 回落到触发线以下，留出了增长的空间


@given(
    st.lists(st.integers(0, 400), min_size=1, max_size=25),
    st.integers(50, 3000),
    st.integers(1, 4),
)
@settings(max_examples=60, deadline=None)
def test_properties_of_any_stream(sizes: list[int], trigger: int, keep: int) -> None:
    msgs = [m for i, n in enumerate(sizes, 1) for m in step(i, size=n)]
    p = policy(trigger=trigger, keep=keep)
    out = run(msgs, p=p).messages
    assert len(out) == len(msgs) and out == run(msgs, p=p).messages
    assert_atomic_pairs(out)
    assert [m.tool_call_id for m in out] == [m.tool_call_id for m in msgs]
    tool_idx = [i for i, m in enumerate(out) if m.role == "tool"]
    for i in tool_idx[-keep:]:
        assert out[i] == msgs[i]  # 最近 K 步永远完整
    # 单调：更长的流里，已折叠的位置仍是折叠的
    shorter = run(msgs[: 2 * max(1, len(sizes) // 2)], p=p).messages
    for a, b in zip(shorter, out, strict=False):
        if is_stub(a):
            assert b == a


@pytest.mark.unit
def test_the_trigger_is_inclusive() -> None:
    msgs = stream(4, size=1000)
    total = sum(len(m.content) for m in msgs)
    assert run(msgs, p=policy(trigger=total, keep=1)).folded > 0
    assert run(msgs, p=policy(trigger=total + 1, keep=1)).folded == 0


@pytest.mark.unit
def test_the_stub_points_at_the_handle_the_governor_appended_not_one_quoted_in_the_body() -> None:
    first = step(1, size=1000)
    first[1] = Message(
        role="tool",
        content="见 artifact://obs_step_0_old 的内容\n" + first[1].content,
        tool_call_id="c1",
        name="bash",
    )
    msgs = first + stream(4, size=1000)[2:]
    stub = next(m for m in run(msgs, p=policy(trigger=300, keep=1)).messages if m.role == "tool")
    assert "obs_step_1_bash" in stub.content and "obs_step_0_old" not in stub.content


@pytest.mark.unit
def test_only_steps_with_tool_calls_count_toward_the_keep_window() -> None:
    """规划者的纯文本指令（没有工具调用）不算一步，否则最近的证据会被提前折叠。"""
    msgs: list[Message] = []
    for i in range(1, 6):
        msgs += [Message(role="assistant", content=f"指令{i}"), *step(i, size=1000)]
    out = run(msgs, p=policy(trigger=300, keep=2)).messages
    tools = [m for m in out if m.role == "tool"]
    assert [is_stub(m) for m in tools] == [True, True, True, False, False]


# ==============================================================================
# 可重现的读类结果：没有离线副本也能折叠
# ==============================================================================


def reproducible_policy(trigger: int = 300, keep: int = 2) -> FoldPolicy:
    return FoldPolicy(
        trigger_tokens=trigger,
        keep_recent_steps=keep,
        exempt_tools=frozenset(),
        reproducible_tools=frozenset({"view_file", "grep"}),
    )


@pytest.mark.unit
def test_old_read_results_fold_to_a_stub_that_says_how_to_get_them_back() -> None:
    msgs = stream(5, name="view_file", size=200, handle=False, args={"path": "a.py"})
    out = run(msgs, p=reproducible_policy())
    stubs = [m for m in out.messages if is_stub(m)]
    assert out.folded == 3 and len(stubs) == 3  # 最近 2 步保持完整
    assert "view_file" in stubs[0].content and '"path": "a.py"' in stubs[0].content
    assert "重新调用 view_file" in stubs[0].content and "artifact://" not in stubs[0].content
    assert not is_stub(out.messages[-1]) and not is_stub(out.messages[-3])


@pytest.mark.unit
def test_results_that_cannot_be_reproduced_stay_even_without_a_copy() -> None:
    msgs = stream(5, name="bash", size=200, handle=False)  # bash 的输出没有离线副本，重跑可能不同
    assert run(msgs, p=reproducible_policy()).folded == 0


@pytest.mark.unit
def test_a_copy_is_preferred_over_re_running_and_pairs_stay_intact() -> None:
    msgs = stream(5, name="grep", size=200, handle=True)
    out = run(msgs, p=reproducible_policy())
    first = next(m for m in out.messages if is_stub(m))
    assert "artifact://obs_step_1_grep" in first.content
    assert_atomic_pairs(out.messages)

"""任务内旧观察值折叠：对装配视图的操作，不改 ``state.messages``。

设计文档：docs/03_context/02_window_and_compaction.md §3.2（级别 2）、§3.2.1（成批、低频）。

折叠决定是**消息序列的纯函数**：从头重放，每当“基础部分 + 可见的任务内消息”达到触发线，
就把除最近 K 步之外的旧观察值一次性折叠；之后只有可见规模再次触到触发线才会再折叠。
所以折叠是成批的、单调的（折叠过的永远折叠），且不需要把折叠水位写进状态。
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from agent_runtime.core import Message

_HANDLE: Final = re.compile(r"artifact://([A-Za-z0-9_.-]+)")
_SUMMARY_CHARS: Final = 80


@dataclass(frozen=True, slots=True)
class FoldPolicy:
    #: 触发线（Token）：``min(窗口 × 比例, 绝对上限)``，见 ``TaskContextConfig.soft_trigger``
    trigger_tokens: int
    keep_recent_steps: int
    #: 永不折叠的工具（如 ``load_skill``：截断流程文档会让做法残缺）
    exempt_tools: frozenset[str]
    #: 结果能靠“用同样的参数再调一次”取回的工具（读文件、搜索）：折叠不需要离线副本，只留一行存根
    reproducible_tools: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class FoldResult:
    messages: list[Message]
    #: 被换成存根的 tool 消息数
    folded: int


def fold_stream(
    stream: Sequence[Message],
    *,
    base_tokens: int,
    policy: FoldPolicy,
    count: Callable[[str], int],
) -> FoldResult:
    """``stream``：任务内往返（最新需求之后的消息）。``base_tokens``：流之外各段的规模。"""
    items = list(stream)
    stubs = _stubs(items, policy, count)
    if not stubs:
        return FoldResult(items, 0)

    step_of = _step_index(items)
    saved = {i: count(items[i].content) - count(stub.content) for i, stub in stubs.items()}
    keep = max(policy.keep_recent_steps, 1)
    folded_through = 0  # 步序号 < folded_through 的步已折叠
    visible = base_tokens
    steps_seen = 0
    for i, message in enumerate(items):
        if message.role == "assistant" and message.tool_calls:
            steps_seen += 1
        visible += count(message.content)
        if visible >= policy.trigger_tokens:
            target = steps_seen - keep
            for j in stubs:
                if j <= i and folded_through <= step_of[j] < target:
                    visible -= saved[j]
            folded_through = max(folded_through, target)

    out = list(items)
    folded = 0
    for i, stub in stubs.items():
        if step_of[i] < folded_through:
            out[i] = stub
            folded += 1
    return FoldResult(out, folded)


def _step_index(items: Sequence[Message]) -> dict[int, int]:
    """每条 tool 消息所属的步序号（从 0 起）：一步 = 带 ``tool_calls`` 的 assistant 及其响应。"""
    out: dict[int, int] = {}
    step = -1
    for i, m in enumerate(items):
        if m.role == "assistant" and m.tool_calls:
            step += 1
        if m.role == "tool":
            out[i] = max(step, 0)
    return out


def _stubs(
    items: Sequence[Message], policy: FoldPolicy, count: Callable[[str], int]
) -> dict[int, Message]:
    """每条可折叠的 tool 消息对应的存根。

    能取回才折叠：有离线副本的，存根指向产物；读类工具（``reproducible_tools``）没有副本也行，
    存根写明调用参数，需要时用同样的参数再调一次。其余没有副本的不折叠：折叠会让它永远取不回。
    """
    calls = {c.id: c for m in items for c in m.tool_calls}
    out: dict[int, Message] = {}
    for i, m in enumerate(items):
        if m.role != "tool" or (m.name or "") in policy.exempt_tools:
            continue
        call = calls.get(m.tool_call_id or "")
        shown = _summarise(call.args) if call is not None else ""
        lines = m.content.count("\n") + 1
        handles = _HANDLE.findall(m.content)
        if handles:
            text = (
                f"[已折叠] {m.name}: {shown}（{lines} 行，{len(m.content)} 字符）"
                f"→ 完整输出见产物 artifact://{handles[-1]}，可用 read_artifact 取回"
            )
        elif (m.name or "") in policy.reproducible_tools:
            text = (
                f"[已折叠] {m.name}: {shown}（{lines} 行，{len(m.content)} 字符）"
                f"→ 需要时用同样的参数重新调用 {m.name}"
            )
        else:
            continue
        if count(text) < count(m.content):
            out[i] = dataclasses.replace(m, content=text)
    return out


def _summarise(args: object) -> str:
    if isinstance(args, dict) and isinstance(args.get("command"), str):  # type: ignore[reportUnknownMemberType]
        raw = args["command"]  # type: ignore[reportUnknownVariableType]
    else:
        raw = json.dumps(args, ensure_ascii=False, sort_keys=True)
    flat = " ".join(str(raw).split())
    return flat if len(flat) <= _SUMMARY_CHARS else flat[:_SUMMARY_CHARS] + "…"

"""会话流水的切片：只在完整的“问加答”边界截断。设计文档：02_window_and_compaction.md §1.4。"""

from __future__ import annotations

from collections.abc import Sequence

from agent_runtime.memory.models import Turn


def split_for_compaction(
    turns: Sequence[Turn], target_tokens: int, keep_recent_turns: int
) -> tuple[list[Turn], list[Turn]]:
    """返回 (待压缩, 保留)。

    在累计量达到 ``target_tokens`` 的**第一个 assistant 回复之后**截断；够不到目标时退而求其次，
    取允许范围内最后一个 assistant 节点。至少留下最近 ``keep_recent_turns`` 条（且不少于一条）。
    """
    items = list(turns)
    keep = max(keep_recent_turns, 1)
    total = 0
    fallback: int | None = None
    for i, turn in enumerate(items):
        total += turn.token_count
        if turn.role != "assistant" or len(items) - (i + 1) < keep:
            continue
        fallback = i + 1
        if total >= target_tokens:
            return items[: i + 1], items[i + 1 :]
    if fallback is None:
        return [], items
    return items[:fallback], items[fallback:]

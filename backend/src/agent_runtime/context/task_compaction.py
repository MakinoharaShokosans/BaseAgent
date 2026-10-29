"""任务内压缩（级别 3）：折叠之后仍然超过 hard 线时，把较早的步骤总结成一份摘要。

设计文档：docs/03_context/02_window_and_compaction.md §3.3。

只改装配视图，不动 ``state.messages``：压缩的结果是两个状态字段，``stream_summary``（摘要）
与 ``stream_cut``（目标消息之后有多少条往返已并入摘要）。
检查点、轨迹、续跑看到的仍是完整的 transcript。

压缩不会让任务失败：模型总结失败时退回一份机械的调用清单，摘要仍然生成，窗口照样变小。
摘要出自对不可信观察值的总结，装配时仍标不可信。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from loguru import logger
from pydantic import BaseModel, ConfigDict

from agent_runtime.context.prompt import goal_index
from agent_runtime.core import AgentState, Message
from agent_runtime.guardrails import wrap

_CALL_TIMEOUT_SEC: Final = 120.0
_MIN_NEW_STEPS: Final = 2  # 自动压缩至少要并入这么多步，免得每个回合都压一次
_ARGS_CHARS: Final = 160
_MECHANICAL_NOTE: Final = "（模型总结不可用，以下只是调用清单，没有各步的要点）"


class TaskSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = ""


#: （系统提示词，用户消息）→ 摘要文本；模型不可用或输出不可用时返回 None。
#: 由运行时层注入：这一层不依赖 llm（与记忆压缩器同一做法）
Summariser = Callable[[str, str], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class TaskCompactionConfig:
    enabled: bool
    #: 折叠之后仍达到它就压缩（``min(窗口 × hard_ratio, hard_tokens)``）
    trigger_tokens: int
    keep_recent_steps: int
    summary_max_chars: int
    #: 送去总结的旧步骤的字符上限；超出的每条会被截短
    input_max_chars: int


@dataclass(frozen=True, slots=True)
class Compaction:
    """一次压缩：状态增量加上给事件用的数字。"""

    update: Mapping[str, object]
    #: 本次新并入摘要的往返步数
    steps: int
    before_tokens: int
    summary_chars: int
    #: ``llm``：模型总结；``mechanical``：模型不可用时的调用清单
    via: str


CompactStep = Callable[[AgentState, bool], Awaitable[Compaction | None]]


class TaskCompactor:
    def __init__(
        self,
        summarise: Summariser,
        assemble: Callable[[AgentState], Sequence[Message]],
        count: Callable[[str], int],
        config: TaskCompactionConfig,
        *,
        prompt: str,
    ) -> None:
        self._summarise_with = summarise
        self._assemble = assemble
        self._count = count
        self._cfg = config
        self._prompt = prompt

    async def __call__(self, state: AgentState, force: bool = False) -> Compaction | None:
        """需要压缩就返回压缩结果，否则 None。

        ``force``：窗口已经溢出，不看触发线，近期步骤只保留一步。
        """
        cfg = self._cfg
        if not cfg.enabled:
            return None
        start = goal_index(state)
        if start is None:
            return None
        stream = state.messages[start + 1 :]
        before = sum(self._count(m.content) for m in self._assemble(state))
        if not force and before < cfg.trigger_tokens:
            return None
        keep = 1 if force else max(cfg.keep_recent_steps, 1)
        boundaries = [i for i, m in enumerate(stream) if m.role == "assistant" and m.tool_calls]
        if len(boundaries) <= keep:
            return None
        cut = boundaries[-keep]
        fresh = [b for b in boundaries if state.stream_cut <= b < cut]
        if not fresh or (not force and len(fresh) < _MIN_NEW_STEPS):
            return None
        chunk = stream[state.stream_cut : cut]
        summary, via = await self._summarise(state, chunk)
        update = {"stream_summary": summary, "stream_cut": cut}
        return Compaction(update, len(fresh), before, len(summary), via)

    async def _summarise(self, state: AgentState, chunk: Sequence[Message]) -> tuple[str, str]:
        cap = self._cfg.summary_max_chars
        try:
            text = await asyncio.wait_for(
                self._summarise_with(self._prompt, self._user_message(state, chunk)),
                _CALL_TIMEOUT_SEC,
            )
            if text and text.strip():
                return _clip(text.strip(), cap), "llm"
            logger.warning("任务内压缩：模型没有给出可用的摘要，改用调用清单")
        except Exception as exc:  # 压缩不能让任务失败
            logger.warning("任务内压缩：模型总结失败（{}），改用调用清单", type(exc).__name__)
        return _clip(_mechanical(state.stream_summary, chunk, cap), cap), "mechanical"

    def _user_message(self, state: AgentState, chunk: Sequence[Message]) -> str:
        previous = state.stream_summary or "（还没有）"
        plan = "\n".join(f"- [{p.status}] {p.title}" for p in state.plan) or "（无）"
        notes = "\n".join(f"- {f}" for f in state.facts) or "（无）"
        log = wrap(
            "task_log", _render(chunk, self._cfg.input_max_chars), source="agent", trust="untrusted"
        )
        return (
            f"已有摘要：\n{previous}\n\n要并入的步骤记录（按时间顺序）：\n{log}\n\n"
            f"智能体当前的计划（已另外保存，不必重复）：\n{plan}\n\n笔记（同上）：\n{notes}\n\n"
            f"摘要不超过 {self._cfg.summary_max_chars} 字符。"
        )


def _render(chunk: Sequence[Message], total_chars: int) -> str:
    """把往返渲染成文本：每条结果按总预算均摊后截短，保证输入有界。"""
    per = max(300, min(3000, total_chars // max(len(chunk), 1)))
    lines: list[str] = []
    for m in chunk:
        if m.role == "assistant":
            if m.content.strip():
                lines.append(f"[助手] {_clip(m.content.strip(), per)}")
            lines += [f"[调用] {c.name} {_args(c.args)}" for c in m.tool_calls]
        elif m.role == "tool":
            lines.append(f"[结果 {m.name}] {_clip(m.content, per)}")
        else:
            lines.append(f"[通知] {_clip(m.content, per)}")
    return _clip("\n".join(lines), total_chars)


def _mechanical(previous: str, chunk: Sequence[Message], cap: int) -> str:
    """模型不可用时的兜底：旧摘要原样保留，再按时间列出调用与结果的头一行。"""
    results = {m.tool_call_id: m for m in chunk if m.role == "tool"}
    lines: list[str] = []
    for m in chunk:
        for c in m.tool_calls:
            result = results.get(c.id)
            head = (result.content.strip().splitlines() or [""])[0] if result else ""
            lines.append(f"- {c.name} {_args(c.args)} → {_clip(head, 80)}")
    body = "\n".join(lines)
    old = previous.removeprefix(_MECHANICAL_NOTE).strip()
    text = f"{_MECHANICAL_NOTE}\n{old}\n{body}".strip() if old else f"{_MECHANICAL_NOTE}\n{body}"
    if len(text) <= cap:
        return text
    # 超长时丢最早的行：近期的调用比远期的更有用
    return _MECHANICAL_NOTE + "\n…" + text[-(cap - len(_MECHANICAL_NOTE) - 2) :]


def _args(args: Any) -> str:
    return _clip(
        " ".join(json.dumps(args, ensure_ascii=False, sort_keys=True).split()), _ARGS_CHARS
    )


def _clip(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[: max(cap - 1, 0)] + "…"
